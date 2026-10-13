"""The exit supervisor's vocabulary and its pure judgements.

Everything that decides something is here and takes plain values: what one
child run was, how healthy the whole arrangement is, and whether a regular stop
or a deploy may go ahead. The gathering (subprocess, launchd, database, state
file) lives elsewhere and only feeds these.

Unknown is never healthy. A state file that cannot be read, a database that
cannot be reached, a service whose status cannot be established: each is a
reason, and with positions possibly open it is a critical one.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID

from src.orchestration.jobruns.models import (
    Coverage,
    EvidenceRow,
    ExitLivenessGate,
    PolicyClass,
    judge_exit_liveness,
)

# launchd starts one tick this often (`StartInterval`).
INTERVAL_SECONDS = 60
# The longest exit run this supervisor accepts: `PAPER_EXIT_RUN_MAX_SECONDS`
# above it is refused before anything starts.
EXIT_RUN_MAX_SECONDS = 60
# The liveness window the supervisor is built for: a BUY needs evidence no
# older than this, and two missed ticks still fit inside it.
LIVENESS_MAX_AGE_SECONDS = 180
# The shortest liveness window a tick accepts: one interval plus one full run.
# Anything shorter would go stale between two healthy ticks.
MIN_LIVENESS_MAX_AGE_SECONDS = INTERVAL_SECONDS + EXIT_RUN_MAX_SECONDS
# The child's hard wall clock: its own runtime bound, two bounded record writes
# (5 s each) and start-up. Below the gate's running grace (run bound + 30 s), so
# a killed child's unfinished row is read as a run that never finished.
CHILD_HARD_TIMEOUT_SECONDS = 85
# After SIGTERM, how long the child's process group gets before SIGKILL.
CHILD_KILL_GRACE_SECONDS = 5
# A tick that started this long after the previous one means ticks were missed:
# the host slept, launchd was not running the job, or a tick hung.
TICK_GAP_SECONDS = 2 * INTERVAL_SECONDS + 30
# No tick started for this long while the service is loaded: the supervisor is
# not supervising.
HEARTBEAT_STALE_SECONDS = 3 * INTERVAL_SECONDS
# A tick still marked in progress after this long died without finishing.
TICK_ABANDONED_SECONDS = CHILD_HARD_TIMEOUT_SECONDS + CHILD_KILL_GRACE_SECONDS + 30

# The one command a tick ever starts, after the interpreter. A constant, never
# assembled from input: there is no path from configuration to another mode.
EXIT_COMMAND: tuple[str, ...] = ("-m", "src.runner.main", "--exits-once")

LABEL = "com.rh-agents.exit-supervisor"
STATE_VERSION = 1
# Bounded so a state file and a health document stay small.
MAX_WARNINGS = 16


class Category(StrEnum):
    """What kind of trouble one tick ran into. Codes say exactly which."""

    OK = "OK"
    SKIPPED = "SKIPPED"
    CONFIGURATION = "CONFIGURATION"
    DATABASE = "DATABASE"
    PROVIDER = "PROVIDER"
    # An exit attempt's chain read (ATLAS, over RPC), its pre-exit refresh or its
    # booking: the exit job cannot tell an RPC outage from a refused sale.
    EXECUTION_OR_RPC = "EXECUTION_OR_RPC"
    COVERAGE = "COVERAGE"
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    TIMEOUT = "TIMEOUT"
    CRASH = "CRASH"
    TECHNICAL = "TECHNICAL"


class TickCode(StrEnum):
    READY = "EXIT_JOB_READY"
    ALREADY_RUNNING = "EXIT_JOB_ALREADY_RUNNING"
    HARD_TIMEOUT = "EXIT_JOB_HARD_TIMEOUT"
    CRASHED = "EXIT_JOB_CRASHED"
    OUTPUT_UNREADABLE = "EXIT_JOB_OUTPUT_UNREADABLE"
    CONFIGURATION_REFUSED = "EXIT_JOB_CONFIGURATION_REFUSED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    TECHNICAL_FAILURE = "EXIT_JOB_TECHNICAL_FAILURE"
    NOT_READY = "EXIT_JOB_NOT_READY"
    START_FAILED = "EXIT_JOB_START_FAILED"
    TICK_ALREADY_RUNNING = "SUPERVISOR_TICK_ALREADY_RUNNING"
    SETTINGS_INVALID = "SUPERVISOR_SETTINGS_INVALID"
    RUN_BOUND_EXCEEDED = "SUPERVISOR_EXIT_RUN_BOUND_EXCEEDED"
    LIVENESS_WINDOW_TOO_SHORT = "SUPERVISOR_LIVENESS_WINDOW_TOO_SHORT"


# How each `exit_ready_reason` the exit job records reads to an operator.
READY_REASON_CATEGORY: dict[str, Category] = {
    "EXIT_JOB_TECHNICAL_FAILURE": Category.TECHNICAL,
    "EXIT_JOB_LOCK_NOT_HELD": Category.TECHNICAL,
    "EXIT_JOB_ALREADY_RUNNING": Category.SKIPPED,
    "EXIT_JOB_SYSTEM_STOPPED": Category.SYSTEM_STOPPED,
    "EXIT_JOB_TIMED_OUT": Category.TIMEOUT,
    "EXIT_POLICY_NOT_CONFIGURED": Category.CONFIGURATION,
    "EXIT_POLICY_COVERAGE_MISSING": Category.COVERAGE,
    "EXIT_POLICY_COVERAGE_UNKNOWN": Category.COVERAGE,
    "EXIT_ACQUISITION_DISABLED": Category.CONFIGURATION,
    "EXIT_ACQUISITION_INCOMPLETE": Category.PROVIDER,
    "EXIT_PLAN_INCOMPLETE": Category.EXECUTION_OR_RPC,
    "EXIT_OUTCOME_UNKNOWN": Category.EXECUTION_OR_RPC,
    "EXIT_ATTEMPT_FAILED": Category.EXECUTION_OR_RPC,
    "EXIT_BLOCKED": Category.EXECUTION_OR_RPC,
    "EXIT_ATTEMPT_TIMED_OUT": Category.EXECUTION_OR_RPC,
    "EXIT_BACKLOG": Category.EXECUTION_OR_RPC,
    "EXIT_MARK_UNKNOWN": Category.PROVIDER,
}


@dataclass(frozen=True)
class TickResult:
    """What one child run was, as codes only. Nothing the child printed beyond these."""

    code: str
    category: Category
    returncode: int | None
    exit_ready: bool | None = None
    exit_ready_reason: str | None = None
    stop: str | None = None
    lock: str | None = None
    errors: tuple[str, ...] = ()
    run_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.category is Category.OK

    @property
    def skipped(self) -> bool:
        return self.category is Category.SKIPPED

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "ok": self.ok,
            "returncode": self.returncode,
            "exit_ready": self.exit_ready,
            "exit_ready_reason": self.exit_ready_reason,
            "stop": self.stop,
            "lock": self.lock,
            "errors": list(self.errors),
            "run_id": self.run_id,
        }


def _code(value: Any) -> str | None:
    """A value the child printed, kept only if it looks like one of our codes."""
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    if not all(char.isupper() or char.isdigit() or char == "_" for char in value):
        return None
    return value


def _uuid(value: Any) -> str | None:
    """A run id the child printed, kept only if it is one."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def classify_child(returncode: int | None, stdout: str, *, timed_out: bool) -> TickResult:
    """What one `--exits-once` child ended as, from its exit status and its JSON.

    Only an exit-0 run summary that says `exit_ready` is a success. Everything
    else — killed, crashed, unreadable, refused, broken, not ready — is a
    failure with a category, and a start that found the exit lock held is
    neither: another exit run is going and will leave its own evidence.
    """
    if timed_out:
        return TickResult(TickCode.HARD_TIMEOUT.value, Category.TIMEOUT, returncode)
    if returncode is None or returncode < 0:
        return TickResult(TickCode.CRASHED.value, Category.CRASH, returncode)
    document: Any = None
    for line in reversed(stdout.strip().splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        break
    if not isinstance(document, dict):
        return TickResult(TickCode.OUTPUT_UNREADABLE.value, Category.TECHNICAL, returncode)
    kind = document.get("kind")
    if kind == "run_configuration_refused" or returncode == 2:
        reason = _code(document.get("reason"))
        return TickResult(
            TickCode.CONFIGURATION_REFUSED.value,
            Category.CONFIGURATION,
            returncode,
            errors=(reason,) if reason else (),
        )
    if kind == "run_technical_failure":
        reason = _code(document.get("reason"))
        database = reason in ("RUN_STARTUP_FAILED", "DATABASE_UNAVAILABLE")
        return TickResult(
            TickCode.DATABASE_UNAVAILABLE.value if database else TickCode.TECHNICAL_FAILURE.value,
            Category.DATABASE if database else Category.TECHNICAL,
            returncode,
            errors=(reason,) if reason else (),
        )
    if kind != "paper_run_summary" or document.get("mode") != "EXITS_ONLY":
        return TickResult(TickCode.OUTPUT_UNREADABLE.value, Category.TECHNICAL, returncode)
    raw_errors = document.get("errors")
    errors = tuple(
        code
        for code in (_code(item) for item in (raw_errors if isinstance(raw_errors, list) else []))
        if code is not None
    )
    ready = document.get("exit_ready")
    facts: dict[str, Any] = {
        "exit_ready": ready if isinstance(ready, bool) else None,
        "exit_ready_reason": _code(document.get("exit_ready_reason")),
        "stop": _code(document.get("stop")),
        "lock": _code(document.get("lock")),
        "errors": errors,
        "run_id": _uuid(document.get("run_id")),
    }
    if facts["lock"] == "ALREADY_RUNNING":
        return TickResult(TickCode.ALREADY_RUNNING.value, Category.SKIPPED, returncode, **facts)
    if returncode != 0 or errors:
        database = "DATABASE_UNAVAILABLE" in errors
        return TickResult(
            TickCode.DATABASE_UNAVAILABLE.value if database else TickCode.TECHNICAL_FAILURE.value,
            Category.DATABASE if database else Category.TECHNICAL,
            returncode,
            **facts,
        )
    if ready is True:
        return TickResult(TickCode.READY.value, Category.OK, returncode, **facts)
    category = READY_REASON_CATEGORY.get(facts["exit_ready_reason"] or "", Category.TECHNICAL)
    return TickResult(TickCode.NOT_READY.value, category, returncode, **facts)


def settings_refusal(run_max_seconds: int, liveness_max_age_seconds: int) -> TickCode | None:
    """Whether this configuration is one the supervisor's rhythm can keep live."""
    if run_max_seconds > EXIT_RUN_MAX_SECONDS:
        return TickCode.RUN_BOUND_EXCEEDED
    if liveness_max_age_seconds < MIN_LIVENESS_MAX_AGE_SECONDS:
        return TickCode.LIVENESS_WINDOW_TOO_SHORT
    return None


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    STOPPED = "STOPPED"


class HealthExit(IntEnum):
    """The health command's process code: one per status, so a monitor needs no parser."""

    HEALTHY = 0
    DEGRADED = 1
    CRITICAL = 2
    STOPPED = 3


@dataclass(frozen=True)
class ServiceStatus:
    """What launchd says about the supervisor job. `loaded=None`: could not be asked."""

    loaded: bool | None
    running: bool = False
    last_exit_status: int | None = None


@dataclass(frozen=True)
class SupervisorState:
    """The tick's own record, as read back from the state file."""

    last_tick_started_at: datetime | None = None
    last_tick_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_result: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None
    in_progress_since: datetime | None = None
    last_gap_seconds: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatabaseFacts:
    """What the database says, read in one read-only transaction."""

    evidence: Sequence[EvidenceRow]
    coverage: Coverage
    last_exit_run: dict[str, Any] | None = None
    last_ready_finished_at: datetime | None = None
    entry_halt_manual: bool = False
    entry_halt_auto: bool = False
    paused: bool = False
    # Unfinished rows younger than their runtime bound plus the gate's grace.
    exit_run_active: bool = False
    entry_run_active: bool = False

    @property
    def entry_blocked(self) -> bool:
        """Whether every BUY path refuses right now, durably, whatever runs."""
        return self.entry_halt_manual or self.entry_halt_auto or self.paused


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    reasons: tuple[str, ...]
    open_positions: int | None
    liveness: str | None
    liveness_age_seconds: int | None
    service: ServiceStatus
    state: SupervisorState | None
    database: DatabaseFacts | None
    checked_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> HealthExit:
        return HealthExit[self.status.value]


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else int((now - then).total_seconds())


def evaluate_health(
    *,
    now: datetime,
    service: ServiceStatus,
    state: SupervisorState | None,
    state_readable: bool,
    database: DatabaseFacts | None,
    gate: ExitLivenessGate,
) -> HealthReport:
    """One verdict over the service, the tick record and the exit evidence.

    CRITICAL when a position may be held and nothing demonstrably watches it:
    open positions with stale exit evidence, with the service not running, with
    positions no configured exit policy closes — or positions unknown because
    the database cannot be read. STOPPED when the service is not running and
    nothing is held: the intended idle state, in which every BUY already fails
    closed on stale evidence. DEGRADED for everything else that is not right.
    HEALTHY only when every check passes.
    """
    critical: list[str] = []
    degraded: list[str] = []
    running = service.loaded is True
    if service.loaded is None:
        degraded.append("SERVICE_STATUS_UNKNOWN")
    liveness: str | None = None
    open_positions: int | None = None
    if database is None:
        critical.append("DATABASE_UNREADABLE_POSITIONS_UNKNOWN")
    else:
        liveness = judge_exit_liveness(database.evidence, at=now, gate=gate)
        coverage = database.coverage
        open_positions = coverage.total
        if open_positions:
            if not running:
                critical.append("OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING")
            if liveness is not None:
                critical.append(f"OPEN_POSITIONS_EXIT_STALE:{liveness}")
            for count, policy in (
                (coverage.normal, PolicyClass.NORMAL),
                (coverage.early, PolicyClass.EARLY),
            ):
                if not count or liveness is not None:
                    continue
                verdict = judge_exit_liveness(database.evidence, at=now, gate=gate, policy=policy)
                if verdict is not None:
                    critical.append(f"OPEN_POSITIONS_POLICY_NOT_COVERED:{policy.value}")
            if coverage.unknown:
                critical.append("OPEN_POSITIONS_WITHOUT_EXIT_POLICY")
        elif running and liveness is not None:
            degraded.append(f"EXIT_STALE_ENTRIES_BLOCKED:{liveness}")
    if not state_readable:
        degraded.append("SUPERVISOR_STATE_UNREADABLE")
    elif running and state is None:
        degraded.append("SUPERVISOR_STATE_MISSING")
    if state is not None:
        heartbeat = _age(now, state.last_tick_started_at)
        if running and (heartbeat is None or heartbeat > HEARTBEAT_STALE_SECONDS):
            reason = "SUPERVISOR_HEARTBEAT_STALE"
            (critical if open_positions else degraded).append(reason)
        busy = _age(now, state.in_progress_since)
        if busy is not None and busy > TICK_ABANDONED_SECONDS:
            degraded.append("SUPERVISOR_TICK_ABANDONED")
        if state.consecutive_failures:
            degraded.append(f"CONSECUTIVE_FAILURES:{state.consecutive_failures}")
        degraded.extend(state.warnings)
    if running and service.last_exit_status not in (None, 0):
        degraded.append("SERVICE_LAST_EXIT_NONZERO")
    if critical:
        status = HealthStatus.CRITICAL
    elif not running and service.loaded is not None:
        status = HealthStatus.STOPPED
    elif degraded or not running:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    last_ready = None if database is None else database.last_ready_finished_at
    return HealthReport(
        status=status,
        reasons=tuple(dict.fromkeys(critical + degraded)),
        open_positions=open_positions,
        liveness=liveness,
        liveness_age_seconds=_age(now, last_ready),
        service=service,
        state=state,
        database=database,
        checked_at=now,
    )


class StopRefusal(StrEnum):
    POSITIONS_UNKNOWN = "STOP_REFUSED_POSITIONS_UNKNOWN"
    OPEN_POSITIONS = "STOP_REFUSED_OPEN_POSITIONS"
    EXIT_RUN_ACTIVE = "STOP_REFUSED_EXIT_RUN_ACTIVE"
    ENTRY_NOT_BLOCKED = "STOP_REFUSED_ENTRY_NOT_BLOCKED"
    ENTRY_RUN_ACTIVE = "STOP_REFUSED_ENTRY_RUN_ACTIVE"


def stop_refusals(database: DatabaseFacts | None, *, tick_in_progress: bool) -> list[str]:
    """Why a regular stop must not happen now. Empty: it may.

    A regular stop ends exit supervision, so it is allowed only when there is
    nothing to supervise and nothing can create something: no open position, no
    exit run going, no entry run going, and a durable entry block (an entry
    halt or the pause) that every BUY path honours. The emergency stop is a
    separate action and does not ask.
    """
    if database is None:
        return [StopRefusal.POSITIONS_UNKNOWN.value]
    found: list[str] = []
    if database.coverage.total:
        found.append(StopRefusal.OPEN_POSITIONS.value)
    if database.exit_run_active or tick_in_progress:
        found.append(StopRefusal.EXIT_RUN_ACTIVE.value)
    if not database.entry_blocked:
        found.append(StopRefusal.ENTRY_NOT_BLOCKED.value)
    if database.entry_run_active:
        found.append(StopRefusal.ENTRY_RUN_ACTIVE.value)
    return found


class DeployRefusal(StrEnum):
    POSITIONS_UNKNOWN = "DEPLOY_REFUSED_POSITIONS_UNKNOWN"
    SUPERVISOR_LOADED = "DEPLOY_REFUSED_SUPERVISOR_LOADED"
    SERVICE_STATUS_UNKNOWN = "DEPLOY_REFUSED_SERVICE_STATUS_UNKNOWN"
    OPEN_POSITIONS = "DEPLOY_REFUSED_OPEN_POSITIONS"
    EXIT_RUN_ACTIVE = "DEPLOY_REFUSED_EXIT_RUN_ACTIVE"


def deploy_refusals(database: DatabaseFacts | None, service: ServiceStatus) -> list[str]:
    """Why a code deploy into the runtime worktree must not happen now. Empty: it may.

    A deploy changes the code the supervisor's next tick runs, and the schema
    may be behind that code until a migration is applied. So a deploy waits
    until the supervisor has been stopped regularly — which itself needs an
    empty book — and no exit run is going. Nothing here stops anything.
    """
    found: list[str] = []
    if service.loaded is None:
        found.append(DeployRefusal.SERVICE_STATUS_UNKNOWN.value)
    elif service.loaded:
        found.append(DeployRefusal.SUPERVISOR_LOADED.value)
    if database is None:
        found.append(DeployRefusal.POSITIONS_UNKNOWN.value)
        return found
    if database.coverage.total:
        found.append(DeployRefusal.OPEN_POSITIONS.value)
    if database.exit_run_active:
        found.append(DeployRefusal.EXIT_RUN_ACTIVE.value)
    return found


def tick_gap(previous_start: datetime | None, now: datetime) -> int | None:
    """Seconds since the previous tick started, when ticks were evidently missed."""
    if previous_start is None:
        return None
    gap = now - previous_start
    if gap > timedelta(seconds=TICK_GAP_SECONDS):
        return int(gap.total_seconds())
    return None


__all__ = [
    "CHILD_HARD_TIMEOUT_SECONDS",
    "EXIT_COMMAND",
    "EXIT_RUN_MAX_SECONDS",
    "INTERVAL_SECONDS",
    "LABEL",
    "LIVENESS_MAX_AGE_SECONDS",
    "Category",
    "DatabaseFacts",
    "DeployRefusal",
    "HealthExit",
    "HealthReport",
    "HealthStatus",
    "ServiceStatus",
    "StopRefusal",
    "SupervisorState",
    "TickCode",
    "TickResult",
    "classify_child",
    "deploy_refusals",
    "evaluate_health",
    "settings_refusal",
    "stop_refusals",
    "tick_gap",
]
