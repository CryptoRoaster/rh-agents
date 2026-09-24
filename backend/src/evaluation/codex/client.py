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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from src.evaluation.codex import sandbox
from src.evaluation.codex.catalog import (
    RuntimeCatalog,
    judge_snapshot,
    materialise_runtime_catalog,
    snapshot_payload,
)
from src.evaluation.codex.command import (
    CommandBuildError,
    build_arguments,
    build_preflight_arguments,
    build_version_arguments,
    child_environment,
)
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.diagnostics import (
    REDACTION_FAILED,
    PathAliases,
    ProcessDiagnostic,
    diagnose,
)
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
    StreamDiagnostic,
)
from src.evaluation.codex.preflight import check_chatgpt_login, check_cli_version
from src.evaluation.codex.process import AbortedByConsumer, ProcessError, run_bounded
from src.evaluation.codex.release import ReleasePermit, RunBinding
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
    expected_catalog_sha256: str
    model: str
    # The named, digest-bound copy of the judged catalog bytes, held open for
    # the whole prepared run. Absent means "materialise one for this attempt
    # and discard it afterwards", which is what the offline tests do; a real
    # build additionally requires one, because the binding names its path and
    # its digest and neither can be checked against nothing.
    runtime_catalog: RuntimeCatalog | None = None
    effort: str | None = None
    forbidden_roots: tuple[Path, ...] = ()
    process_limits: ProcessLimits = ProcessLimits()
    run_preflight: bool = True
    # When set, every attempt runs behind the outer Seatbelt profile. A real
    # turn additionally requires it -- `release.evaluate_release` fails
    # `OUTER_READ_SANDBOX` when it is absent, and that gate has no degraded
    # mode.
    outer_sandbox: sandbox.SandboxRoots | None = None
    # A profile that already exists, with the digest of the bytes it was created
    # from. When set, the client loads *this* policy and re-checks the bytes
    # rather than composing a fresh one from the two source files: between a
    # preflight that measured a profile and an exec that loads one, "the same
    # two file names" is not "the same policy".
    outer_profile: sandbox.WrittenProfile | None = None
    preflight_budget_seconds: float = PREFLIGHT_BUDGET_SECONDS
    version_budget_seconds: float = VERSION_BUDGET_SECONDS

    def __post_init__(self) -> None:
        """Refuse a configuration that could reach `exec` without a pinned catalog.

        The digest is not a mode, a flag or a caller's promise. There is no
        combination of this dataclass that carries a missing or malformed digest
        any further than construction, so nothing downstream has to remember to
        check for one.
        """
        digest = self.expected_catalog_sha256
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("expected_catalog_sha256 must be a sha256 hex digest")
        if any(character not in "0123456789abcdef" for character in digest.lower()):
            raise ValueError("expected_catalog_sha256 must be a sha256 hex digest")


class OuterSandboxMissing(Exception):
    """Raised when the outer profile is configured but not on disk."""


def _remove_empty_directory(directory: Path) -> None:
    """Clear a runtime-catalog directory whose catalog was never written."""
    try:
        directory.rmdir()
    except OSError:
        pass


def binding_for(config: CodexClientConfig, profile_sha256: str) -> RunBinding:
    """Reduce a configuration to the identity a release is granted for.

    Every path is resolved, because the question is which real location the run
    may reach, not how the caller spelled it. Widening a root, swapping the
    isolated home, pointing at a different launcher or a different pinned
    catalog all change one of these fields, and the comparison in
    `_permit_problem` then refuses before any process starts.

    The runtime catalog contributes three fields rather than one: the path the
    sandbox grants read access to, the path `model_catalog_json` is pointed at,
    and the digest of the bytes in it. A path alone would authorise whatever
    that path happens to hold at exec time, and a digest alone would authorise
    the same bytes reached through some other file.
    """
    sandbox_roots = config.outer_sandbox
    parameters = sandbox_roots.parameters() if sandbox_roots is not None else {}
    runtime_catalog = config.runtime_catalog
    return RunBinding(
        model=config.model,
        catalog_digest=config.expected_catalog_sha256,
        catalog_path=str(config.model_catalog_path.resolve()),
        runtime_catalog_path=(
            str(runtime_catalog.path.resolve()) if runtime_catalog is not None else ""
        ),
        runtime_catalog_digest=runtime_catalog.digest if runtime_catalog is not None else "",
        launcher_kind=str(config.launcher.kind),
        launcher_path=str(config.launcher.executable.resolve()),
        supported_cli_version=SUPPORTED_CLI_VERSION,
        workspace=str(config.workspace.resolve()),
        codex_home=str(config.codex_home.resolve()),
        home=str(config.home.resolve()),
        tmpdir=str(config.tmpdir.resolve()),
        scratch=str(config.scratch.resolve()),
        sandbox_vendor=parameters.get("CODEX_VENDOR", ""),
        sandbox_workspace=parameters.get("WORKSPACE", ""),
        sandbox_codex_home=parameters.get("CODEX_HOME", ""),
        sandbox_auth_file=parameters.get("AUTH_FILE", ""),
        sandbox_installation_id_file=parameters.get("INSTALLATION_ID_FILE", ""),
        sandbox_catalog_file=parameters.get("CATALOG_FILE", ""),
        forbidden_roots=tuple(sorted(str(root.resolve()) for root in config.forbidden_roots)),
        run_preflight=config.run_preflight,
        max_exec_starts=config.process_limits.max_exec_starts,
        profile_sha256=profile_sha256,
    )


@dataclass
class CodexEvaluationClient:
    """Runs at most one attempt. A second call is refused, not retried.

    Usable without a permit, with a `FAKE_EXECUTABLE` launcher: that is the
    offline engine every test in this suite drives. What it will not do without
    one is start a real Codex build -- see `_permit_problem`.
    """

    config: CodexClientConfig
    # Held by a `ReleaseAuthorization`, which only a cleared preflight produces.
    # Absent is the normal case; it is required only for a real build.
    permit: ReleasePermit | None = None
    exec_starts: int = field(default=0, init=False)
    _profile: Path | None = field(default=None, init=False)
    _owns_profile: bool = field(default=False, init=False)
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
        problem = self._permit_problem()
        if problem is not None:
            # Before the effort check, before the budget check, before any
            # process: nothing about a real build may start on the strength of
            # a configuration that was never cleared.
            return self._reject(EvaluationFailure.LAUNCHER_UNSUPPORTED, problem, deadline)
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

        # Cheapest gate first: one bounded read, no process at all.
        #
        # A prepared run hands its own runtime catalog in and keeps it for the
        # whole context, which is what lets the binding name a path that still
        # exists at exec time. Without one -- the offline engine -- the attempt
        # materialises its own into a private directory under the scratch root
        # and removes it again afterwards. There is deliberately no third mode:
        # the descriptor transport cannot serve a catalog the CLI reloads.
        held = self.config.runtime_catalog
        if held is not None:
            return await self._attempt_with_catalog(request, deadline, held, environment, schema)

        try:
            directory = Path(tempfile.mkdtemp(dir=self.config.scratch, prefix="catalog-runtime-"))
        except OSError as error:
            return self._reject(
                EvaluationFailure.TOOL_SURFACE_UNSUPPORTED,
                type(error).__name__.upper(),
                deadline,
            )
        catalog = materialise_runtime_catalog(self.config.model_catalog_path, directory)
        try:
            return await self._attempt_with_catalog(request, deadline, catalog, environment, schema)
        finally:
            if catalog is not None:
                catalog.discard()
            else:
                _remove_empty_directory(directory)

    async def _attempt_with_catalog[Output: BaseModel](
        self,
        request: EvaluationRequest[Output],
        deadline: Deadline,
        catalog: RuntimeCatalog | None,
        environment: dict[str, str],
        schema: dict[str, object],
    ) -> EvaluationOutcome[Output]:
        if catalog is None:
            return self._reject(
                EvaluationFailure.TOOL_SURFACE_UNSUPPORTED, "CATALOG_UNREADABLE", deadline
            )
        rejection = self._verify_tool_surface(catalog, deadline)
        if rejection is not None:
            return rejection

        # One profile for the whole attempt. Every Codex invocation goes behind
        # it -- the version probe and the login probe as much as the turn --
        # so no phase of the path that may be released ever starts Codex
        # unsandboxed.
        if self.config.outer_sandbox is not None:
            bound = self.config.outer_profile
            if bound is not None:
                # Prepared elsewhere and already measured. Loading it is only
                # sound if it is still the policy that was measured.
                if not bound.still_matches():
                    return self._reject(
                        EvaluationFailure.PROCESS_START_FAILED, "PROFILE_DIGEST_MISMATCH", deadline
                    )
                self._profile = bound.path
                self._owns_profile = False
            else:
                try:
                    self._profile = sandbox.write_profile(self.config.scratch)
                    self._owns_profile = True
                except OSError as error:
                    return self._reject(
                        EvaluationFailure.PROCESS_START_FAILED,
                        type(error).__name__.upper(),
                        deadline,
                    )
        try:
            return await self._attempt_sandboxed(request, deadline, catalog, environment, schema)
        finally:
            # A profile handed in belongs to whoever prepared it and outlives
            # this attempt; one written here does not.
            if self._profile is not None and self._owns_profile:
                self._profile.unlink(missing_ok=True)
            self._profile = None
            self._owns_profile = False

    async def _attempt_sandboxed[Output: BaseModel](
        self,
        request: EvaluationRequest[Output],
        deadline: Deadline,
        catalog: RuntimeCatalog,
        environment: dict[str, str],
        schema: dict[str, object],
    ) -> EvaluationOutcome[Output]:

        rejection = await self._verify_version(environment, request.limits, deadline)
        if rejection is not None:
            return rejection

        if self.config.run_preflight:
            rejection = await self._preflight(environment, request.limits, deadline)
            if rejection is not None:
                return rejection

        # The schema travels as a descriptor too. Passing it by path would mean
        # opening the scratch directory to the sandboxed process, and the
        # profile deliberately has no scratch root.
        schema_snapshot = snapshot_payload(
            json.dumps(schema, sort_keys=True).encode("utf-8"), self.config.scratch
        )
        if schema_snapshot is None:
            return self._reject(
                EvaluationFailure.SCHEMA_UNSUPPORTED, "SCHEMA_SNAPSHOT_FAILED", deadline
            )
        try:
            arguments = build_arguments(
                launcher=self.config.launcher,
                working_directory=self.config.workspace,
                schema_path=Path(schema_snapshot.reference),
                model=self.config.model,
                effort=self.config.effort,
                instructions=request.instructions,
                model_catalog_reference=catalog.reference,
                forbidden_roots=self.config.forbidden_roots,
            )
            # The same profile the probes ran behind, written once for the
            # whole attempt and removed in the caller's `finally`. Applied in
            # front of Codex itself, not in front of the commands it might run.
            arguments = self._sandboxed(arguments)
        except CommandBuildError as error:
            return self._reject(error.failure, error.reason_code, deadline)
        except OuterSandboxMissing:
            return self._reject(
                EvaluationFailure.PROCESS_START_FAILED, "OUTER_SANDBOX_MISSING", deadline
            )

        accumulator = EventAccumulator(
            max_final_message_bytes=request.limits.max_final_message_bytes,
            # So a Codex `error` event is redacted against the same bound paths
            # the stderr channel uses, rather than reported verbatim.
            aliases=self._aliases(),
        )
        payload = json.dumps(
            request.data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

        # Last thing before the exec, and after everything that could have
        # taken time: the version probe, the login probe and the schema
        # snapshot all ran since the bytes were judged. Re-hashing the file
        # here is what makes the judgement about the bytes the child will read
        # rather than about the bytes that were there a few seconds ago. A
        # mismatch refuses with `exec_starts` still at zero.
        if not catalog.still_matches():
            schema_snapshot.close()
            return self._reject(
                EvaluationFailure.PROCESS_START_FAILED, "RUNTIME_CATALOG_DIGEST_MISMATCH", deadline
            )

        self.exec_starts += 1
        try:
            return await self._run_attempt(
                request,
                deadline,
                arguments,
                environment,
                payload,
                accumulator,
                # Only the schema travels as a descriptor now. The catalog is a
                # named file the child opens for itself, once per config build.
                (schema_snapshot.fd,),
            )
        finally:
            schema_snapshot.close()

    async def _run_attempt[Output: BaseModel](
        self,
        request: EvaluationRequest[Output],
        deadline: Deadline,
        arguments: list[str],
        environment: dict[str, str],
        payload: bytes,
        accumulator: EventAccumulator,
        extra_fds: tuple[int, ...],
    ) -> EvaluationOutcome[Output]:
        try:
            result = await run_bounded(
                arguments=arguments,
                environment=environment,
                working_directory=self.config.workspace,
                stdin_payload=payload,
                limits=request.limits,
                deadline=deadline,
                on_stdout_line=accumulator.feed,
                extra_fds=extra_fds,
            )
        except ProcessError as error:
            return self._reject(error.failure, error.reason_code, deadline, error.cleanup)
        except AbortedByConsumer as error:
            # The consumer stopped reading and the process layer then signalled
            # the group, so there is no child exit status and no stderr tail --
            # nothing a `ProcessDiagnostic` could honestly describe. What the
            # stream had already established is reported instead.
            cause = error.cause
            if isinstance(cause, StreamError):
                return self._reject(
                    cause.failure,
                    cause.reason_code,
                    deadline,
                    error.cleanup,
                    stream_diagnostic=self._stream_diagnostic(accumulator, cause.safe_lines),
                )
            return self._reject(
                EvaluationFailure.EVENT_STREAM_INVALID,
                type(cause).__name__.upper(),
                deadline,
                error.cleanup,
                stream_diagnostic=self._stream_diagnostic(accumulator, ()),
            )

        # The tail is not dropped any more and is not logged either: it goes
        # straight into the redactor and the raw bytes end there.
        return self._finish(
            request,
            accumulator,
            result.exit_code,
            result.cleanup,
            deadline,
            result.stderr_tail,
            request.limits.max_stderr_bytes,
        )

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

        try:
            wrapped = self._sandboxed(arguments)
        except OuterSandboxMissing:
            return self._reject(
                EvaluationFailure.PROCESS_START_FAILED, "OUTER_SANDBOX_MISSING", deadline
            )

        self.version_starts += 1
        try:
            outcome = await check_cli_version(
                arguments=wrapped,
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

    def _verify_tool_surface(
        self, catalog: RuntimeCatalog, deadline: Deadline
    ) -> EvaluationRejected | None:
        """Refuse a catalog whose entry would widen the tool surface.

        Judged from the bytes that were written into the runtime file, not by
        re-reading the operator's path. Those bytes are what the child is
        pointed at, and `_runtime_catalog_drifted` re-hashes the file
        immediately before the exec, so the window between this judgement and
        the run is closed by a check rather than by an assumption.
        """
        verdict = judge_snapshot(catalog, self.config.model, self.config.expected_catalog_sha256)
        if verdict.reason is not None:
            return self._reject(
                EvaluationFailure.TOOL_SURFACE_UNSUPPORTED, verdict.reason, deadline
            )
        return None

    def _permit_problem(self) -> str | None:
        """Why this client may not start the launcher it was given, if it may not.

        A real Codex build is reachable only with a permit, and the permit has
        to describe *this* configuration -- otherwise a cleared preflight for
        one run would authorise a different one. A fake launcher needs neither,
        because starting it is not starting Codex.
        """
        if not self.config.launcher.kind.is_real_codex:
            return None
        if self.permit is None:
            return "RELEASE_PERMIT_REQUIRED"
        if self.config.outer_profile is None:
            # The binding names a profile digest, so there has to be a bound
            # profile to compare it against.
            return "PERMIT_WITHOUT_BOUND_PROFILE"
        if self.config.runtime_catalog is None:
            # Same reasoning for the catalog: the binding names a runtime path
            # and a runtime digest, and an attempt that would materialise its
            # own copy has nothing for those fields to be about. A real build
            # runs against the catalog the prepared run holds open, or not at
            # all.
            return "PERMIT_WITHOUT_RUNTIME_CATALOG"
        expected = self.permit.binding
        actual = binding_for(self.config, self.config.outer_profile.digest)
        drifted = expected.differences(actual)
        if drifted:
            return f"PERMIT_MISMATCH_{drifted[0].upper()}"
        return None

    def _sandboxed(self, arguments: list[str]) -> list[str]:
        """Put the outer profile in front of any Codex invocation.

        `sandbox-exec` execs in place, so the gate's pid, its process group and
        every inherited descriptor carry through unchanged.

        A configured outer sandbox with no profile on disk raises instead of
        returning the bare command. Falling back to an unwrapped Codex would be
        the exact outcome the configuration exists to prevent, and it would be
        invisible in the result.
        """
        if self.config.outer_sandbox is None:
            return arguments
        if self._profile is None:
            raise OuterSandboxMissing
        return sandbox.wrap(arguments, self._profile, self.config.outer_sandbox)

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

        try:
            wrapped = self._sandboxed(arguments)
        except OuterSandboxMissing:
            return self._reject(
                EvaluationFailure.PREFLIGHT_FAILED, "OUTER_SANDBOX_MISSING", deadline
            )

        self.preflight_starts += 1
        try:
            outcome = await check_chatgpt_login(
                arguments=wrapped,
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
        stderr_tail: bytes = b"",
        stderr_limit: int = 0,
    ) -> EvaluationOutcome[Output]:
        diagnostic = diagnose(
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            captured_limit=stderr_limit,
            aliases=self._aliases(),
        )
        if deadline.expired:
            # The budget was already gone when the child finished. Parsing an
            # answer now could only produce a result that arrived too late.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED,
                "RESULT_AFTER_DEADLINE",
                deadline,
                cleanup,
                diagnostic,
            )
        if exit_code != 0 and not accumulator.turn_started:
            # A CLI that failed before emitting a single event never got as far
            # as a turn, so the interesting fact is the failed start, not the
            # missing completion. Reported first for that reason: the previous
            # ordering answered "the turn did not complete", which is true and
            # useless, and it is what made the first real failure undiagnosable.
            return self._reject(
                EvaluationFailure.PROCESS_FAILED,
                f"EXIT_{exit_code}_BEFORE_TURN",
                deadline,
                cleanup,
                diagnostic,
                # Retryable stream trouble is reported through `error` events
                # and no longer ends the run, so a CLI that gave up after
                # several attempts exits with those messages behind it. They
                # are what explains the exit, and they would otherwise be lost.
                self._stream_diagnostic(accumulator, accumulator.safe_error_lines()),
            )
        try:
            answer = accumulator.require_consistent_completion()
        except StreamError as error:
            # A turn did start here, so the event semantics decide and are left
            # exactly as they were.
            return self._reject(
                error.failure,
                error.reason_code,
                deadline,
                cleanup,
                diagnostic,
                self._stream_diagnostic(
                    accumulator, error.safe_lines or accumulator.safe_error_lines()
                ),
            )
        if exit_code != 0:
            # A completed turn and a failing exit contradict each other; the
            # attempt is not treated as successful on the strength of one of them.
            return self._reject(
                EvaluationFailure.INCONSISTENT_COMPLETION,
                f"EXIT_{exit_code}",
                deadline,
                cleanup,
                diagnostic,
            )

        try:
            payload = json.loads(answer)
        except json.JSONDecodeError:
            return self._reject(
                EvaluationFailure.OUTPUT_NOT_JSON, "ANSWER_NOT_JSON", deadline, cleanup, diagnostic
            )
        try:
            output = request.output_model.model_validate(payload)
        except ValidationError:
            return self._reject(
                EvaluationFailure.OUTPUT_SCHEMA_MISMATCH,
                "LOCAL_SCHEMA_MISMATCH",
                deadline,
                cleanup,
                diagnostic,
            )
        if deadline.expired:
            # First point where control is back. Starting the domain validator
            # now would add work that could not change the outcome, so the
            # overrun stops here rather than after a second blocking call.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED,
                "SCHEMA_VALIDATION_OVERRAN",
                deadline,
                cleanup,
                diagnostic,
            )
        try:
            request.domain_validator(output)
        except DomainValidationError as error:
            return self._reject(
                EvaluationFailure.OUTPUT_DOMAIN_INVALID,
                error.reason_code,
                deadline,
                cleanup,
                diagnostic,
            )

        if deadline.expired:
            # Schema parsing and the injected validator are synchronous calls the
            # event loop cannot preempt, so an overrun can only be noticed once
            # they return. It is noticed here, and it is never completed anyway.
            return self._reject(
                EvaluationFailure.DEADLINE_EXCEEDED,
                "VALIDATION_OVERRAN",
                deadline,
                cleanup,
                diagnostic,
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
        diagnostic: ProcessDiagnostic | None = None,
        stream_diagnostic: StreamDiagnostic | None = None,
    ) -> EvaluationRejected:
        return EvaluationRejected(
            reason=failure,
            detail_code=detail_code,
            wall_clock_ms=deadline.elapsed_ms,
            cleanup=cleanup if cleanup is not None else CleanupReport(),
            diagnostic=diagnostic,
            stream_diagnostic=stream_diagnostic,
        )

    def _stream_diagnostic(
        self, accumulator: EventAccumulator, safe_lines: tuple[str, ...]
    ) -> StreamDiagnostic:
        """What the parser knew when it stopped. Redacted text only.

        `safe_lines` arrives already redacted from `StreamError`; nothing is
        re-read from the stream here. The rest is state the accumulator holds
        anyway, and none of it can carry a prompt, a payload, a model answer or
        a credential.
        """
        try:
            return StreamDiagnostic(
                safe_lines=safe_lines,
                turn_started=accumulator.turn_started,
                thread_id=accumulator.thread_id,
                observed_tool_activity=accumulator.observed_tool_activity(),
            )
        except Exception:  # noqa: BLE001 - a diagnostic may never be the failure
            return StreamDiagnostic(safe_lines=(REDACTION_FAILED,))

    def _aliases(self) -> PathAliases:
        """The bound paths, so a diagnostic names roles rather than locations.

        A failure message is about this harness's behaviour. Publishing where
        the machine keeps the user's home, or which temporary directory held
        the login copy, adds nothing to that and is not ours to publish.
        """
        roots = self.config.outer_sandbox
        parameters = roots.parameters() if roots is not None else {}
        return PathAliases.build(
            codex_home=self.config.codex_home,
            home=self.config.home,
            workspace=self.config.workspace,
            scratch=self.config.scratch,
            tmpdir=self.config.tmpdir,
            catalog=self.config.model_catalog_path,
            launcher=self.config.launcher.executable,
            profile=self.config.outer_profile.path if self.config.outer_profile else None,
            codex_vendor=parameters.get("CODEX_VENDOR"),
        )
