"""Codex adapter for the structured reasoning port: GPT-5.5 on a ChatGPT login.

Every call goes through the same gated path the offline harness uses: a fresh
`prepare_real_run` (isolated `CODEX_HOME` with a copied `auth.json`, pinned
model catalog, outer Seatbelt profile, version and login probes, release
gates) and exactly one `evaluate` in the environment it measured. A refused
release is a refused call; there is no degraded mode that runs without it.

The adapter owns no domain rule. Callers validate their own output after it
returns (ORBIT runs `validate_assessment`), so the harness is handed a validator
that accepts every schema-valid answer. Nothing from `auth.json` is read into
this process, and no rejection carries more than the harness's own codes.

The port's `max_output_tokens` has no Codex counterpart and is not enforced
here; the harness bounds the final message by bytes instead.

The login state is leased, not just copied: `prepare_real_run` holds an
exclusive lease on the source `auth.json` for the whole call and writes a
refreshed generation back at teardown. What became of it is checked after the
context has closed, and an outcome that leaves the source stale or contested
fails the call -- even one whose turn succeeded -- because the next call would
otherwise start from a login state nobody vouches for. A turn that failed
because the login can no longer be refreshed is `CODEX_LOGIN_REQUIRED`: the
provider is unusable until someone signs in again, so it is reported as not
configured rather than as a transient outage to retry.
"""

import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.core.config import Settings
from src.core.numbers import canonical_decimal
from src.evaluation.codex.auth_home import (
    PERSISTENCE_FAILURES,
    SOURCE_CHANGED,
    SOURCE_UNSAFE,
    AuthSourceError,
    Persistence,
)
from src.evaluation.codex.credential_lease import (
    LEASE_TIMEOUT,
    LEASE_UNAVAILABLE,
    CredentialLeaseError,
)
from src.evaluation.codex.final_preflight import default_launcher
from src.evaluation.codex.models import (
    CodexLauncher,
    EvaluationFailure,
    EvaluationRejected,
    EvaluationRequest,
)
from src.evaluation.codex.prepared_run import prepare_real_run
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningRequest,
    ReasoningResult,
    ReasoningUsage,
)

CODEX_PROVIDER = "codex"

# Terminating and reaping the child draws on this slice of the call's budget.
CLEANUP_RESERVE_SECONDS = 5.0
# Below this, what the preflight left over is not worth starting a turn with.
MIN_TURN_SECONDS = 10.0
# A probe is not started with less than its own cleanup reserve left, so a
# preflight that ended within this much of its budget ran out of time.
PREFLIGHT_SLACK_SECONDS = 2.5

_Category = ReasoningErrorCategory

# Every harness rejection, in terms the worker runtime already acts on. A
# misconfigured or unmeasurable environment is not retried; a failed turn is.
FAILURE_CATEGORIES: dict[EvaluationFailure, ReasoningErrorCategory] = {
    EvaluationFailure.CLI_VERSION_UNSUPPORTED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.VERSION_CHECK_FAILED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.TOOL_SURFACE_UNSUPPORTED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.LAUNCHER_UNSUPPORTED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.EFFORT_NOT_SUPPORTED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.PREFLIGHT_FAILED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.SCHEMA_UNSUPPORTED: _Category.PROVIDER_REJECTED_REQUEST,
    EvaluationFailure.PROCESS_START_FAILED: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.PROCESS_FAILED: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.TURN_FAILED: _Category.PROVIDER_UNAVAILABLE,
    # Not transient: no retry helps until someone signs in again, the same
    # standing a rejected API key has for the other providers.
    EvaluationFailure.LOGIN_REQUIRED: _Category.PROVIDER_NOT_CONFIGURED,
    EvaluationFailure.CLEANUP_INCOMPLETE: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.EVENT_STREAM_INVALID: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.STDOUT_OVERFLOW: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.STDERR_OVERFLOW: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.LINE_OVERFLOW: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.PARSER_BUDGET_EXCEEDED: _Category.PROVIDER_UNAVAILABLE,
    EvaluationFailure.DEADLINE_EXCEEDED: _Category.PROVIDER_TIMEOUT,
    EvaluationFailure.NO_FINAL_MESSAGE: _Category.INVALID_MODEL_OUTPUT,
    EvaluationFailure.INCONSISTENT_COMPLETION: _Category.INVALID_MODEL_OUTPUT,
    EvaluationFailure.OUTPUT_NOT_JSON: _Category.INVALID_MODEL_OUTPUT,
    EvaluationFailure.OUTPUT_SCHEMA_MISMATCH: _Category.INVALID_MODEL_OUTPUT,
    EvaluationFailure.OUTPUT_DOMAIN_INVALID: _Category.INVALID_MODEL_OUTPUT,
}

# The credential lease and the source login state, before anything ran. Another
# holder or a source that moved meanwhile is worth another attempt later; an
# unusable lock directory or an unsafe source file is not.
LEASE_CATEGORIES: dict[str, ReasoningErrorCategory] = {
    LEASE_TIMEOUT: _Category.PROVIDER_UNAVAILABLE,
    LEASE_UNAVAILABLE: _Category.PROVIDER_NOT_CONFIGURED,
    SOURCE_UNSAFE: _Category.PROVIDER_NOT_CONFIGURED,
    SOURCE_CHANGED: _Category.PROVIDER_UNAVAILABLE,
}

PrepareRun = Callable[..., AbstractAsyncContextManager[Any]]

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def _canonical(value: object) -> object:
    """Serialize domain values without ever passing money through a float."""
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _accept(_: object) -> None:
    """The caller validates its own domain after the call returns."""


def _refused(blocking: Sequence[Any]) -> str:
    """The first gate that refused the release, as a code. Never its detail text."""
    name = str(getattr(blocking[0], "name", "")) if blocking else ""
    code = f"CODEX_GATE_{name}"
    return code if _SAFE_CODE.fullmatch(code) else "CODEX_RELEASE_NOT_GRANTED"


def _check_persistence(credentials: object) -> None:
    """Fail the call when the login state could not be brought back cleanly."""
    persistence = getattr(credentials, "persistence", None)
    if isinstance(persistence, Persistence) and persistence in PERSISTENCE_FAILURES:
        raise ReasoningFailure(_Category.PROVIDER_UNAVAILABLE, PERSISTENCE_FAILURES[persistence])


def find_launcher(executable: str) -> CodexLauncher | None:
    """The platform binary behind a configured `codex` command, or on PATH."""
    return default_launcher(Path(executable) if executable else None)


@dataclass(frozen=True)
class CodexReasoningProvider:
    launcher: CodexLauncher
    source_codex_home: Path
    effort: str | None = None
    prepare: PrepareRun = prepare_real_run
    monotonic: Callable[[], float] = field(default=time.monotonic)

    @property
    def name(self) -> str:
        return CODEX_PROVIDER

    async def generate_structured[Output: BaseModel](
        self, request: ReasoningRequest[Output]
    ) -> ReasoningResult[Output]:
        started = self.monotonic()
        # One budget for the whole call. The probes before the turn draw on it
        # and must leave enough for a turn and its cleanup; they never get a
        # budget of their own on top of the caller's.
        probe_budget = request.timeout_seconds - CLEANUP_RESERVE_SECONDS - MIN_TURN_SECONDS
        if probe_budget <= 0:
            raise ReasoningFailure(_Category.PROVIDER_REJECTED_REQUEST, "CODEX_TIMEOUT_TOO_SHORT")
        credentials: object = None
        try:
            async with self.prepare(
                launcher=self.launcher,
                source_codex_home=self.source_codex_home,
                effort=self.effort,
                probe_budget_seconds=probe_budget,
            ) as prepared:
                credentials = getattr(prepared, "credentials", None)
                if prepared.runner is None:
                    elapsed = self.monotonic() - started
                    if elapsed >= probe_budget - PREFLIGHT_SLACK_SECONDS:
                        raise ReasoningFailure(
                            _Category.PROVIDER_TIMEOUT, "CODEX_PREFLIGHT_TOO_SLOW"
                        )
                    raise ReasoningFailure(
                        _Category.PROVIDER_NOT_CONFIGURED, _refused(prepared.status.blocking)
                    )
                remaining = request.timeout_seconds - (self.monotonic() - started)
                if remaining < CLEANUP_RESERVE_SECONDS + MIN_TURN_SECONDS:
                    raise ReasoningFailure(_Category.PROVIDER_TIMEOUT, "CODEX_PREFLIGHT_TOO_SLOW")
                outcome = await prepared.runner.evaluate(
                    EvaluationRequest(
                        instructions=request.instructions,
                        data={key: _canonical(item) for key, item in request.data.items()},
                        output_model=request.output_model,
                        domain_validator=_accept,
                        deadline_seconds=remaining,
                        cleanup_reserve_seconds=CLEANUP_RESERVE_SECONDS,
                    )
                )
        except (CredentialLeaseError, AuthSourceError) as error:
            raise ReasoningFailure(
                LEASE_CATEGORIES.get(error.code, _Category.PROVIDER_UNAVAILABLE), error.code
            ) from None
        except OSError:
            # Building or tearing down the isolated environment failed. Nothing
            # from the error is kept: it may name paths under the login home.
            raise ReasoningFailure(
                _Category.PROVIDER_UNAVAILABLE, "CODEX_ENVIRONMENT_FAILED"
            ) from None
        except ReasoningFailure:
            # A refusal inside the context still had a teardown, and a login
            # state that could not be brought back outranks it: it decides
            # whether the next call can work at all.
            _check_persistence(credentials)
            raise
        _check_persistence(credentials)
        if isinstance(outcome, EvaluationRejected):
            raise ReasoningFailure(
                FAILURE_CATEGORIES.get(outcome.reason, _Category.PROVIDER_UNAVAILABLE),
                f"CODEX_{outcome.reason.value}",
            )
        if outcome.observed_tool_activity:
            # The port promises a call without tools. An answer that came with
            # observed tool use is not accepted, however valid it looks.
            raise ReasoningFailure(_Category.INVALID_MODEL_OUTPUT, "CODEX_TOOL_ACTIVITY_OBSERVED")
        configuration = outcome.configuration
        return ReasoningResult(
            output=outcome.output,
            model=ReasoningModel(
                provider=CODEX_PROVIDER,
                model=configuration.reported_model or configuration.configured_model,
                # What was asked for, and separately what the CLI said it ran.
                # Codex 0.153.4 does not report an effort, so the second stays
                # None rather than echoing the first.
                effort=configuration.configured_effort,
                reported_effort=configuration.reported_effort,
            ),
            usage=ReasoningUsage(
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                latency_ms=int((self.monotonic() - started) * 1000),
                # A Codex thread id is a local identifier, not a request id.
                provider_request_id=None,
            ),
        )


def codex_provider_from(settings: Settings) -> CodexReasoningProvider | None:
    """The configured provider, or None when no Codex CLI can be found.

    Resolving the launcher touches the filesystem and nothing else: no process
    starts and no login is read until a call is made.
    """
    launcher = find_launcher(settings.codex_executable)
    if launcher is None:
        return None
    home = Path(settings.codex_home).expanduser() if settings.codex_home else Path.home() / ".codex"
    return CodexReasoningProvider(
        launcher=launcher, source_codex_home=home, effort=settings.reasoning_effort
    )
