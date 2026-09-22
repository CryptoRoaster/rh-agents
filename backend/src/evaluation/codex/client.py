"""One Codex evaluation attempt, end to end.

The client promises three things and enforces all three locally: at most one
`codex exec` start -- never a resume, never a restart after any outcome -- one
monotonic deadline covering spawn, reading, parsing and validation, and bounded
reading of every child channel.

It promises nothing about model requests, HTTP attempts or generated tokens.
A turn may issue more than one Responses request, the built-in provider's retry
budget cannot be overridden from config, and no output-token key exists at all.
Those numbers are therefore unknown, and this client never reports a number it
did not observe.

Validation is local and final. The schema handed to the CLI is an aid; the
authority is `output_model.model_validate` followed by the injected domain
validator, which holds the original task input bound in its closure. A schema
conformant answer that contradicts its own input is rejected, and no rejection
path can produce an `EvaluationCompleted`.

The result is a test artifact. It is not workflow evidence, carries no lease or
claim, and nothing here may reach a risk decision or an execution.
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from src.evaluation.codex.catalog import judge_catalog_file
from src.evaluation.codex.command import (
    CommandBuildError,
    build_arguments,
    build_preflight_arguments,
    build_version_arguments,
    child_environment,
)
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    SUPPORTED_CLI_VERSION,
    SUPPORTED_EFFORTS,
    AttemptConfiguration,
    CleanupReport,
    CodexLauncher,
    DomainValidationError,
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationOutcome,
    EvaluationRejected,
    EvaluationRequest,
    OutputLimits,
    ProcessLimits,
    SchemaUnsupportedError,
)
from src.evaluation.codex.preflight import check_chatgpt_login, check_cli_version
from src.evaluation.codex.process import AbortedByConsumer, ProcessError, run_bounded
from src.evaluation.codex.schema import strict_schema
from src.evaluation.codex.stream import EventAccumulator, StreamError

PREFLIGHT_BUDGET_SECONDS = 10.0
VERSION_BUDGET_SECONDS = 10.0


@dataclass(frozen=True)
class CodexClientConfig:
    """Everything the client needs, passed in rather than discovered.

    Nothing is read from `Settings`, so this harness cannot be reached from the
    production composition even by accident.
    """

    launcher: CodexLauncher
    codex_home: Path
    home: Path
    tmpdir: Path
    workspace: Path
    scratch: Path
    model_catalog_path: Path
    model: str
    effort: str | None = None
    forbidden_roots: tuple[Path, ...] = ()
    process_limits: ProcessLimits = ProcessLimits()
    run_preflight: bool = True
    preflight_budget_seconds: float = PREFLIGHT_BUDGET_SECONDS
    version_budget_seconds: float = VERSION_BUDGET_SECONDS


@dataclass
class CodexEvaluationClient:
    """Runs at most one attempt. A second call is refused, not retried."""

    config: CodexClientConfig
    exec_starts: int = field(default=0, init=False)
    version_starts: int = field(default=0, init=False)
    preflight_starts: int = field(default=0, init=False)

    async def evaluate[Output: BaseModel](
        self, request: EvaluationRequest[Output]
    ) -> EvaluationOutcome[Output]:
        deadline = Deadline(
            total_seconds=request.deadline_seconds,
            cleanup_reserve_seconds=request.cleanup_reserve_seconds,
        )
        try:
            return await self._attempt(request, deadline)
        except asyncio.CancelledError:
            # Cleanup already ran inside the process layer. The caller's
            # cancellation is theirs to see, so it is never converted.
            raise

    async def _attempt[Output: BaseModel](
        self, request: EvaluationRequest[Output], deadline: Deadline
    ) -> EvaluationOutcome[Output]:
        if self.config.effort is not None and self.config.effort not in SUPPORTED_EFFORTS:
            # Never remapped onto a neighbouring value: a silently downgraded
            # effort would make the recorded configuration a lie.
            return self._reject(
                EvaluationFailure.EFFORT_NOT_SUPPORTED, self.config.effort, deadline
            )
        if self.exec_starts >= self.config.process_limits.max_exec_starts:
            return self._reject(
                EvaluationFailure.PROCESS_START_FAILED, "EXEC_BUDGET_EXHAUSTED", deadline
            )

        try:
            schema = strict_schema(request.output_model)
        except SchemaUnsupportedError as error:
            return self._reject(EvaluationFailure.SCHEMA_UNSUPPORTED, error.reason_code, deadline)

        environment = child_environment(
            codex_home=self.config.codex_home,
            home=self.config.home,
            tmpdir=self.config.tmpdir,
            path_entries=self.config.launcher.path_entries,
        )

        # Cheapest gate first: a file read, no process at all.
        rejection = self._verify_tool_surface(deadline)
        if rejection is not None:
            return rejection

        rejection = await self._verify_version(environment, request.limits, deadline)
        if rejection is not None:
            return rejection

        if self.config.run_preflight:
            rejection = await self._preflight(environment, request.limits, deadline)
            if rejection is not None:
                return rejection

        schema_path = self.config.scratch / "orbit-output-schema.json"
        try:
            schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
            arguments = build_arguments(
                launcher=self.config.launcher,
                working_directory=self.config.workspace,
                schema_path=schema_path,
                model=self.config.model,
                effort=self.config.effort,
                instructions=request.instructions,
                model_catalog_path=self.config.model_catalog_path,
                forbidden_roots=self.config.forbidden_roots,
            )
        except CommandBuildError as error:
            return self._reject(error.failure, error.reason_code, deadline)
        except OSError as error:
            return self._reject(
                EvaluationFailure.PROCESS_START_FAILED, type(error).__name__.upper(), deadline
            )

        accumulator = EventAccumulator(
            max_final_message_bytes=request.limits.max_final_message_bytes
        )
        payload = json.dumps(
            request.data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

        self.exec_starts += 1
        try:
            result = await run_bounded(
                arguments=arguments,
                environment=environment,
                working_directory=self.config.workspace,
                stdin_payload=payload,
                limits=request.limits,
                deadline=deadline,
                on_stdout_line=accumulator.feed,
            )
        except ProcessError as error:
            return self._reject(error.failure, error.reason_code, deadline, error.cleanup)
        except AbortedByConsumer as error:
            cause = error.cause
            if isinstance(cause, StreamError):
                return self._reject(cause.failure, cause.reason_code, deadline, error.cleanup)
            return self._reject(
                EvaluationFailure.EVENT_STREAM_INVALID,
                type(cause).__name__.upper(),
                deadline,
                error.cleanup,
            )

        return self._finish(request, accumulator, result.exit_code, result.cleanup, deadline)

    async def _verify_version(
        self, environment: dict[str, str], limits: OutputLimits, deadline: Deadline
    ) -> EvaluationRejected | None:
        """Ask the launcher on disk which build it is, and refuse anything else.

        A configured version string would only record an expectation. What
        decides is the answer of the executable that would actually run, since
        a different build may carry a different event contract, different
        default tools or different flag names.
        """
        if self.version_starts >= self.config.process_limits.max_version_starts:
            return self._reject(
                EvaluationFailure.VERSION_CHECK_FAILED, "VERSION_BUDGET_EXHAUSTED", deadline
            )
        try:
            arguments = build_version_arguments(launcher=self.config.launcher)
        except CommandBuildError as error:
            return self._reject(error.failure, error.reason_code, deadline)

        probe = self._probe_deadline(self.config.version_budget_seconds, deadline)
        if probe is None:
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED, "NO_TIME_FOR_VERSION_CHECK", deadline
            )

        self.version_starts += 1
        try:
            outcome = await check_cli_version(
                arguments=arguments,
                environment=environment,
                working_directory=self.config.workspace,
                limits=limits,
                deadline=probe,
            )
        except ProcessError as error:
            return self._reject(error.failure, error.reason_code, deadline, error.cleanup)

        if outcome.version is None:
            return self._reject(
                EvaluationFailure.VERSION_CHECK_FAILED,
                "VERSION_NOT_REPORTED",
                deadline,
                outcome.cleanup,
            )
        if outcome.version != SUPPORTED_CLI_VERSION:
            return self._reject(
                EvaluationFailure.CLI_VERSION_UNSUPPORTED,
                outcome.version,
                deadline,
                outcome.cleanup,
            )
        return None

    def _verify_tool_surface(self, deadline: Deadline) -> EvaluationRejected | None:
        """Refuse a model whose pinned catalog entry would widen the tool surface.

        The file judged here is the file `codex exec` is pinned to, so there is
        no window in which a cache entry or a remote `/models` response could
        substitute a different `tool_mode`. Reading it needs no process, which
        is the point: a probe would only reopen the gap between what was
        inspected and what runs.
        """
        verdict = judge_catalog_file(self.config.model_catalog_path, self.config.model)
        if verdict.reason is not None:
            return self._reject(
                EvaluationFailure.TOOL_SURFACE_UNSUPPORTED, verdict.reason, deadline
            )
        return None

    def _probe_deadline(self, budget_seconds: float, deadline: Deadline) -> Deadline | None:
        budget = min(budget_seconds, deadline.remaining_for_work)
        if budget <= 0:
            return None
        return Deadline(total_seconds=budget, cleanup_reserve_seconds=min(1.0, budget / 4))

    async def _preflight(
        self, environment: dict[str, str], limits: OutputLimits, deadline: Deadline
    ) -> EvaluationRejected | None:
        if self.preflight_starts >= self.config.process_limits.max_preflight_starts:
            return self._reject(
                EvaluationFailure.PREFLIGHT_FAILED, "PREFLIGHT_BUDGET_EXHAUSTED", deadline
            )
        try:
            arguments = build_preflight_arguments(launcher=self.config.launcher)
        except CommandBuildError as error:
            return self._reject(error.failure, error.reason_code, deadline)

        probe = self._probe_deadline(self.config.preflight_budget_seconds, deadline)
        if probe is None:
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED, "NO_TIME_FOR_PREFLIGHT", deadline
            )

        self.preflight_starts += 1
        try:
            outcome = await check_chatgpt_login(
                arguments=arguments,
                environment=environment,
                working_directory=self.config.workspace,
                limits=limits,
                deadline=probe,
            )
        except ProcessError as error:
            return self._reject(error.failure, error.reason_code, deadline, error.cleanup)
        if not outcome.chatgpt_session:
            return self._reject(
                EvaluationFailure.PREFLIGHT_FAILED, "NO_CHATGPT_SESSION", deadline, outcome.cleanup
            )
        return None

    def _finish[Output: BaseModel](
        self,
        request: EvaluationRequest[Output],
        accumulator: EventAccumulator,
        exit_code: int,
        cleanup: CleanupReport,
        deadline: Deadline,
    ) -> EvaluationOutcome[Output]:
        if deadline.expired:
            # The budget was already gone when the child finished. Parsing an
            # answer now could only produce a result that arrived too late.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED, "RESULT_AFTER_DEADLINE", deadline, cleanup
            )
        try:
            answer = accumulator.require_consistent_completion()
        except StreamError as error:
            return self._reject(error.failure, error.reason_code, deadline, cleanup)
        if exit_code != 0:
            # A completed turn and a failing exit contradict each other; the
            # attempt is not treated as successful on the strength of one of them.
            return self._reject(
                EvaluationFailure.INCONSISTENT_COMPLETION, f"EXIT_{exit_code}", deadline, cleanup
            )

        try:
            payload = json.loads(answer)
        except json.JSONDecodeError:
            return self._reject(
                EvaluationFailure.OUTPUT_NOT_JSON, "ANSWER_NOT_JSON", deadline, cleanup
            )
        try:
            output = request.output_model.model_validate(payload)
        except ValidationError:
            return self._reject(
                EvaluationFailure.OUTPUT_SCHEMA_MISMATCH, "LOCAL_SCHEMA_MISMATCH", deadline, cleanup
            )
        if deadline.expired:
            # First point where control is back. Starting the domain validator
            # now would add work that could not change the outcome, so the
            # overrun stops here rather than after a second blocking call.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED, "SCHEMA_VALIDATION_OVERRAN", deadline, cleanup
            )
        try:
            request.domain_validator(output)
        except DomainValidationError as error:
            return self._reject(
                EvaluationFailure.OUTPUT_DOMAIN_INVALID, error.reason_code, deadline, cleanup
            )

        if deadline.expired:
            # Schema parsing and the injected validator are synchronous calls the
            # event loop cannot preempt, so an overrun can only be noticed once
            # they return. It is noticed here, and it is never completed anyway.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED, "VALIDATION_OVERRAN", deadline, cleanup
            )

        return EvaluationCompleted(
            output=output,
            configuration=AttemptConfiguration(
                configured_model=self.config.model,
                configured_effort=self.config.effort,
                reported_model=accumulator.reported_model,
                reported_effort=accumulator.reported_effort,
            ),
            usage=accumulator.usage,
            observed_tool_activity=accumulator.observed_tool_activity(),
            thread_id=accumulator.thread_id,
            wall_clock_ms=deadline.elapsed_ms,
            cleanup=cleanup,
        )

    def _reject(
        self,
        failure: EvaluationFailure,
        detail_code: str,
        deadline: Deadline,
        cleanup: CleanupReport | None = None,
    ) -> EvaluationRejected:
        return EvaluationRejected(
            reason=failure,
            detail_code=detail_code,
            wall_clock_ms=deadline.elapsed_ms,
            cleanup=cleanup if cleanup is not None else CleanupReport(),
        )
