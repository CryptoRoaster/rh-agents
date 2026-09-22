"""Contracts for one Codex evaluation attempt.

This is a test harness. Its successful result is a test artifact: never workflow
evidence, never a worker claim, never bound to a lease, and never an input to a
risk decision or an execution.

The contracts are deliberately separate from `src.reasoning`. The structured
reasoning port promises a generation token limit and a provider-neutral failure
taxonomy; the Codex CLI can honour neither, so this package does not restate
them. `ReasoningRequest.max_output_tokens` has no counterpart here, because a
field that cannot be enforced is worse than a field that does not exist.

What the harness promises is exactly what the process wrapper enforces locally:
at most one `codex exec` start, no resume, no restart, one monotonic deadline,
and bounded reading of every child channel. It promises nothing about the number
of model requests, HTTP attempts or generated tokens; the CLI does not expose
those, and claiming them would be false.

One limit of the deadline is worth stating plainly rather than burying. Schema
parsing and the injected domain validator are ordinary synchronous calls. The
event loop cannot preempt them, so a validator that runs long is not cut short;
it is detected once it returns, and the attempt is then rejected instead of
completed. The deadline bounds when a result may be accepted, not how long every
step is allowed to occupy the thread.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# The only CLI build whose event contract and argument surface this harness was
# written against. A different build may add a default tool or rename an event,
# so the client refuses to run rather than assume compatibility.
SUPPORTED_CLI_VERSION = "0.153.4"

# Reasoning effort is a free string in the CLI schema, validated against what
# the model catalog advertises rather than against a fixed enum. These are the
# documented values for the supported build. `max` -- which the production
# reasoning settings allow -- is not among them, and is rejected rather than
# quietly mapped onto `xhigh`.
SUPPORTED_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})

Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EvaluationFailure(StrEnum):
    """Why an attempt produced no validated output.

    Its own taxonomy on purpose: importing `ReasoningErrorCategory` would couple
    the harness to the production port it is meant to stay clear of.
    """

    CLI_VERSION_UNSUPPORTED = "CLI_VERSION_UNSUPPORTED"
    VERSION_CHECK_FAILED = "VERSION_CHECK_FAILED"
    TOOL_SURFACE_UNSUPPORTED = "TOOL_SURFACE_UNSUPPORTED"
    LAUNCHER_UNSUPPORTED = "LAUNCHER_UNSUPPORTED"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    EFFORT_NOT_SUPPORTED = "EFFORT_NOT_SUPPORTED"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    STDOUT_OVERFLOW = "STDOUT_OVERFLOW"
    STDERR_OVERFLOW = "STDERR_OVERFLOW"
    LINE_OVERFLOW = "LINE_OVERFLOW"
    PARSER_BUDGET_EXCEEDED = "PARSER_BUDGET_EXCEEDED"
    EVENT_STREAM_INVALID = "EVENT_STREAM_INVALID"
    TURN_FAILED = "TURN_FAILED"
    NO_FINAL_MESSAGE = "NO_FINAL_MESSAGE"
    INCONSISTENT_COMPLETION = "INCONSISTENT_COMPLETION"
    OUTPUT_NOT_JSON = "OUTPUT_NOT_JSON"
    OUTPUT_SCHEMA_MISMATCH = "OUTPUT_SCHEMA_MISMATCH"
    OUTPUT_DOMAIN_INVALID = "OUTPUT_DOMAIN_INVALID"
    CLEANUP_INCOMPLETE = "CLEANUP_INCOMPLETE"


class SchemaUnsupportedError(Exception):
    """The output model cannot be expressed under strict structured output."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class DomainValidationError(Exception):
    """Locally detected domain contradiction. Raised by the injected validator."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class LauncherKind(StrEnum):
    """How the CLI is started, because the two forms need different environments.

    `PLATFORM_BINARY` is the self-contained executable and needs no interpreter
    on PATH. `NODE_SHIM` is the `codex.js` entry point with a `#!/usr/bin/env
    node` line, so it cannot start unless the directory holding `node` is on the
    PATH handed to the child. The command builder checks that rather than
    quietly inheriting the parent environment.
    """

    PLATFORM_BINARY = "PLATFORM_BINARY"
    NODE_SHIM = "NODE_SHIM"


class OutputLimits(Immutable):
    """Separate caps for raw child bytes and for retained parser memory.

    `max_stdout_bytes` and `max_stderr_bytes` bound what is read off each pipe.
    `max_line_bytes` bounds a single JSONL line, so one unterminated line cannot
    grow without limit. `max_final_message_bytes` bounds the one payload the
    parser keeps, which is a different budget from the raw stream: a stream cap
    alone would still allow a single huge retained message.
    """

    max_stdout_bytes: int = Field(default=1_048_576, ge=1024)
    max_stderr_bytes: int = Field(default=262_144, ge=1024)
    max_line_bytes: int = Field(default=131_072, ge=512)
    max_final_message_bytes: int = Field(default=65_536, ge=256)


class ProcessLimits(Immutable):
    """Process starts this harness allows, counted per kind.

    The attempt, the version probe and the login probe are three separate
    processes with three separate budgets. Collapsing them would hide one start
    behind another, and "at most one `codex exec`" would stop meaning anything.
    """

    max_exec_starts: Literal[1] = 1
    max_version_starts: Literal[1] = 1
    max_catalog_starts: Literal[1] = 1
    max_preflight_starts: Literal[1] = 1


class EvaluationUsage(Immutable):
    """Only what the CLI actually reported. Absent stays absent.

    No field is derived, estimated or back-filled. `provider_request_id` has no
    counterpart in the documented event contract, so there is no such field: a
    thread id is a local identifier and must not be published as one.
    """

    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AttemptConfiguration(Immutable):
    """What we asked for, kept apart from what the CLI said it did."""

    configured_model: str = Field(min_length=1, max_length=80)
    configured_effort: str | None = Field(default=None, min_length=1, max_length=40)
    reported_model: str | None = Field(default=None, min_length=1, max_length=80)
    reported_effort: str | None = Field(default=None, min_length=1, max_length=40)


class ObservedToolActivity(Immutable):
    """A tool-shaped item the CLI actually emitted, with how often it appeared.

    This records observation, never entitlement. An empty tuple of activity does
    NOT show that the model was offered no tools: the documented event stream
    reports tool *use*, and a tool that was offered but never invoked emits
    nothing at all.

    A real run does not change that. A successful invocation would evidence the
    activity it emitted and nothing further -- not the complete tool offering,
    and not filesystem isolation in general. No number of quiet runs adds up to
    a proof of absence.
    """

    item_type: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1)


class GroupState(StrEnum):
    """What was established about the child's process group after cleanup.

    `UNVERIFIED` is the honest answer when the cleanup budget ran out before the
    group could be checked. It is not a synonym for `EMPTY`: signals are cheap
    and were still delivered, but nothing confirmed they took effect.
    """

    EMPTY = "EMPTY"
    OCCUPIED = "OCCUPIED"
    UNVERIFIED = "UNVERIFIED"


class CleanupReport(Immutable):
    """What actually happened to the child's process group after the attempt.

    A clean report is not an operating-system guarantee that the process tree is
    gone. It states three things that were observed: a signal was delivered to
    the process group recorded at spawn time, the direct child was reaped, and a
    later check found the group empty.

    The direct child exiting on its own is explicitly NOT enough. A child can
    leave descendants behind in its group, so cleanup runs against the group
    even when the leader is already gone, and `group` reports what was found
    afterwards. Descendants that left the group with `setsid` are outside what
    this harness can see, and `complete` never claims otherwise.
    """

    signalled: bool = False
    reaped: bool = False
    exit_code: int | None = None
    group: GroupState = GroupState.UNVERIFIED
    overran_reserve: bool = False
    error_code: Code | None = None

    @property
    def group_cleared(self) -> bool:
        """The child was reaped and its group was checked and found empty.

        Kept apart from `complete` so an overrun does not read as a leak. A
        cleanup can take longer than its reserve and still have removed
        everything, and the two facts deserve separate answers.
        """
        return self.reaped and self.group is GroupState.EMPTY and self.error_code is None

    @property
    def complete(self) -> bool:
        """Cleared, and cleared inside the reserve."""
        return self.group_cleared and not self.overran_reserve


@dataclass(frozen=True)
class CodexLauncher:
    """Where the CLI lives and which PATH entries its form requires."""

    kind: LauncherKind
    executable: Path
    path_entries: tuple[Path, ...] = ()


@dataclass(frozen=True)
class EvaluationRequest[Output: BaseModel]:
    """One attempt: fixed instructions, quoted data, and a local validator.

    `instructions` is the only control channel and travels as Codex developer
    instructions. `data` is untrusted content and travels on stdin as one quoted
    JSON document, so a market string named "IGNORE ALL RULES" stays a string.

    `domain_validator` is injected by the caller and holds the original task
    input bound in its closure. The harness stays free of any ORBIT import while
    still refusing output that contradicts the input it was derived from.

    It is called synchronously and is not interruptible: asyncio cannot preempt
    a running Python call, so a slow validator overruns the deadline rather than
    being cancelled by it. The overrun is caught afterwards and turns the
    attempt into a rejection, never into a late success.
    """

    instructions: str
    data: dict[str, object]
    output_model: type[Output]
    domain_validator: Callable[[Output], None]
    deadline_seconds: float
    cleanup_reserve_seconds: float = 2.0
    limits: OutputLimits = OutputLimits()


@dataclass(frozen=True)
class EvaluationCompleted[Output: BaseModel]:
    """A locally validated result. Still only a test artifact."""

    output: Output
    configuration: AttemptConfiguration
    usage: EvaluationUsage
    observed_tool_activity: tuple[ObservedToolActivity, ...]
    thread_id: str | None
    wall_clock_ms: int
    cleanup: CleanupReport


@dataclass(frozen=True)
class EvaluationRejected:
    """No validated output. Never carries a partial result."""

    reason: EvaluationFailure
    detail_code: str
    wall_clock_ms: int
    cleanup: CleanupReport


type EvaluationOutcome[Output: BaseModel] = EvaluationCompleted[Output] | EvaluationRejected
