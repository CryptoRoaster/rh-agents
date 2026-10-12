"""PAPER job runs and the exit job's liveness: the vocabulary and the two judgements.

Entries and exits may run as two jobs (`--entries-once`, `--exits-once`). Split
like that, a BUY is only as safe as the exit job behind it: a position opened
while nothing is watching it can fall without anybody selling it. So in split
operation every BUY asks first whether the exit job has *recently shown that it
works* — and answers `EXIT_JOB_STALE` when it has not.

Two judgements, both pure:

- `assess_exit_run` — what one finished exit-job run is evidence of. Exit code
  0 alone is not enough: the run must have held its lock, acquired its
  positions' markets completely, judged every open position under a configured
  policy, and left no exit unknown, failed, cut off or deferred.
- `judge_exit_liveness` — whether the latest such evidence still covers a BUY
  at one instant: recent enough, from a run that finished, and for the policy
  class the BUY would open a position in.

Neither ever treats something unknown as healthy: a missing, unreadable,
unfinished or malformed record is a reason, not permission.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

# The refusal every BUY path answers with; the detail names the reason.
EXIT_JOB_STALE = "EXIT_JOB_STALE"
# How long past its own runtime bound an unfinished run still counts as running.
RUNNING_GRACE_SECONDS = 30
# How many recent exit-job runs the gate reads. Only one can be running at a
# time (its lock), so the latest finished one is always within the first two.
EVIDENCE_DEPTH = 3
# What a result-code list and the metrics may hold. Bounded so a row stays small.
MAX_CODES = 32


class JobName(StrEnum):
    EXIT = "PAPER_EXIT_JOB"
    ENTRY = "PAPER_ENTRY_JOB"


class JobOutcome(StrEnum):
    """What one run ended as. `RUNNING` until it finishes; left so, it crashed."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    # A stop it was told to honour: a pause, a kill switch, a halt, a refusal.
    REFUSED = "REFUSED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    TIMED_OUT = "TIMED_OUT"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    # Open positions no configured exit policy may close.
    EXIT_COVERAGE_MISSING = "EXIT_COVERAGE_MISSING"


class PolicyClass(StrEnum):
    """Which exit contract closes a position: PAPER_EXIT_V1 or EARLY_PAPER_EXIT_V1."""

    NORMAL = "NORMAL"
    EARLY = "EARLY"


class Liveness(StrEnum):
    """Why the exit job is not evidence a BUY may rest on (the `EXIT_JOB_STALE` detail)."""

    MISSING = "EXIT_JOB_MISSING"
    UNREADABLE = "EXIT_JOB_UNREADABLE"
    INCOMPLETE = "EXIT_JOB_INCOMPLETE"
    UNHEALTHY = "EXIT_JOB_UNHEALTHY"
    TOO_OLD = "EXIT_JOB_TOO_OLD"
    CLOCK_AHEAD = "EXIT_JOB_CLOCK_AHEAD"
    POLICY_NOT_COVERED = "EXIT_POLICY_NOT_COVERED"


class ExitHealth(StrEnum):
    """Why one finished exit-job run is not evidence that exits work."""

    ALREADY_RUNNING = "EXIT_JOB_ALREADY_RUNNING"
    LOCK_NOT_HELD = "EXIT_JOB_LOCK_NOT_HELD"
    TECHNICAL_FAILURE = "EXIT_JOB_TECHNICAL_FAILURE"
    SYSTEM_STOPPED = "EXIT_JOB_SYSTEM_STOPPED"
    TIMED_OUT = "EXIT_JOB_TIMED_OUT"
    NO_POLICY = "EXIT_POLICY_NOT_CONFIGURED"
    COVERAGE_MISSING = "EXIT_POLICY_COVERAGE_MISSING"
    COVERAGE_UNKNOWN = "EXIT_POLICY_COVERAGE_UNKNOWN"
    ACQUISITION_DISABLED = "EXIT_ACQUISITION_DISABLED"
    ACQUISITION_INCOMPLETE = "EXIT_ACQUISITION_INCOMPLETE"
    PLAN_INCOMPLETE = "EXIT_PLAN_INCOMPLETE"
    OUTCOME_UNKNOWN = "EXIT_OUTCOME_UNKNOWN"
    ATTEMPT_FAILED = "EXIT_ATTEMPT_FAILED"
    # A triggered exit was refused for a reason other than the trigger being
    # gone: a position the exit job could not sell.
    BLOCKED = "EXIT_BLOCKED"
    ATTEMPT_TIMED_OUT = "EXIT_ATTEMPT_TIMED_OUT"
    BACKLOG = "EXIT_BACKLOG"
    MARK_UNKNOWN = "EXIT_MARK_UNKNOWN"


# Acquisition stops after which every held market was asked about and answered.
ACQUIRED = frozenset({"COMPLETED", "NOTHING_TO_ACQUIRE"})


@dataclass(frozen=True)
class Coverage:
    """Open positions by the exit contract that closes them, as one exit run found them."""

    normal: int = 0
    early: int = 0
    # No case-bound entry: no exit policy may close it.
    unknown: int = 0

    @property
    def total(self) -> int:
        return self.normal + self.early + self.unknown

    def uncovered(self, policies: frozenset[PolicyClass]) -> int:
        missing = self.unknown
        if PolicyClass.NORMAL not in policies:
            missing += self.normal
        if PolicyClass.EARLY not in policies:
            missing += self.early
        return missing


@dataclass(frozen=True)
class SweepFacts:
    """What one exit policy's sweep did, as far as health is concerned."""

    unevaluated: int = 0
    plan_stopped: bool = False
    unknown: int = 0
    failed: int = 0
    blocked: int = 0
    timed_out: int = 0
    deferred: int = 0
    mark_unknown: int = 0


@dataclass(frozen=True)
class ExitRunFacts:
    """Everything `assess_exit_run` judges, gathered from one finished exit run."""

    lock: str | None
    stop: str
    errors: tuple[str, ...]
    policies: frozenset[PolicyClass]
    coverage: Coverage | None
    acquisition_enabled: bool
    acquisition_stop: str | None
    acquisition_shortfall: int
    sweeps: tuple[SweepFacts, ...]


@dataclass(frozen=True)
class ExitAssessment:
    outcome: JobOutcome
    ready: bool
    reason: ExitHealth | None


def assess_exit_run(facts: ExitRunFacts) -> ExitAssessment:
    """What one finished exit-job run ended as, and whether exits demonstrably work.

    The first reason found is the one reported; a run is ready only when there
    is none. The order puts what the run *was* (locked out, broken, stopped, cut
    off, blind to a position) before what it *found* (a market not acquired, an
    exit unknown, failed, cut off or left for later).
    """

    def not_ready(outcome: JobOutcome, reason: ExitHealth) -> ExitAssessment:
        return ExitAssessment(outcome=outcome, ready=False, reason=reason)

    if facts.lock == "ALREADY_RUNNING":
        return not_ready(JobOutcome.ALREADY_RUNNING, ExitHealth.ALREADY_RUNNING)
    if facts.errors:
        return not_ready(JobOutcome.TECHNICAL_FAILURE, ExitHealth.TECHNICAL_FAILURE)
    if facts.lock not in ("ACQUIRED", "NOT_SUPPORTED"):
        return not_ready(JobOutcome.TECHNICAL_FAILURE, ExitHealth.LOCK_NOT_HELD)
    if facts.stop == "SYSTEM_STOPPED":
        return not_ready(JobOutcome.REFUSED, ExitHealth.SYSTEM_STOPPED)
    if facts.stop == "TIME_BUDGET_REACHED":
        return not_ready(JobOutcome.TIMED_OUT, ExitHealth.TIMED_OUT)
    if facts.stop not in ("NOTHING_LEFT_TO_DO",):
        return not_ready(JobOutcome.TECHNICAL_FAILURE, ExitHealth.TECHNICAL_FAILURE)
    if not facts.policies:
        return not_ready(JobOutcome.EXIT_COVERAGE_MISSING, ExitHealth.NO_POLICY)
    if facts.coverage is None:
        return not_ready(JobOutcome.TECHNICAL_FAILURE, ExitHealth.COVERAGE_UNKNOWN)
    if facts.coverage.uncovered(facts.policies):
        return not_ready(JobOutcome.EXIT_COVERAGE_MISSING, ExitHealth.COVERAGE_MISSING)
    succeeded = JobOutcome.SUCCEEDED
    if not facts.acquisition_enabled:
        return not_ready(succeeded, ExitHealth.ACQUISITION_DISABLED)
    if facts.acquisition_stop not in ACQUIRED or facts.acquisition_shortfall:
        return not_ready(succeeded, ExitHealth.ACQUISITION_INCOMPLETE)
    sweeps = facts.sweeps
    for found, reason in (
        (any(item.unevaluated or item.plan_stopped for item in sweeps), ExitHealth.PLAN_INCOMPLETE),
        (any(item.unknown for item in sweeps), ExitHealth.OUTCOME_UNKNOWN),
        (any(item.failed for item in sweeps), ExitHealth.ATTEMPT_FAILED),
        (any(item.blocked for item in sweeps), ExitHealth.BLOCKED),
        (any(item.timed_out for item in sweeps), ExitHealth.ATTEMPT_TIMED_OUT),
        (any(item.deferred for item in sweeps), ExitHealth.BACKLOG),
        (any(item.mark_unknown for item in sweeps), ExitHealth.MARK_UNKNOWN),
    ):
        if found:
            return not_ready(succeeded, reason)
    return ExitAssessment(outcome=succeeded, ready=True, reason=None)


@dataclass(frozen=True)
class ExitLivenessGate:
    """How recent the exit job's evidence must be for a BUY to rest on it.

    Present only where an operator chose split operation
    (`PAPER_ENTRY_REQUIRES_EXIT_JOB`); absent, no BUY path consults it.
    """

    max_age_seconds: int
    # The exit job's own runtime bound: an unfinished run younger than this
    # (plus a grace) is still running; older, it is a run that never finished.
    run_max_seconds: int


@dataclass(frozen=True)
class EvidenceRow:
    """One exit-job run as the gate reads it. Values are as stored, unvalidated."""

    outcome: Any
    started_at: Any
    finished_at: Any
    exit_ready: Any
    exit_ready_reason: Any
    policy_classes: Any


def judge_exit_liveness(
    rows: Sequence[EvidenceRow],
    *,
    at: datetime,
    gate: ExitLivenessGate,
    policy: PolicyClass | None = None,
) -> str | None:
    """`None` when the exit job's latest evidence covers a BUY at `at`, else why not.

    `rows` are the latest exit-job runs, newest first, `ALREADY_RUNNING` ones
    left out — a start that found the lock held proves nothing either way. The
    one run that may still be going (younger than its bound plus a grace) is
    passed over to the run before it; an older unfinished one never finished.
    The first finished run decides: it must be ready, finished no later than
    `at`, no longer ago than the gate allows, and — when the BUY's policy class
    is named — under a configuration that closes positions of that class.
    """
    try:
        running = timedelta(seconds=gate.run_max_seconds + RUNNING_GRACE_SECONDS)
        for index, row in enumerate(rows):
            if row.outcome == JobOutcome.RUNNING.value or row.finished_at is None:
                if index == 0 and at - row.started_at <= running:
                    continue
                return Liveness.INCOMPLETE.value
            if row.exit_ready is not True:
                return Liveness.UNHEALTHY.value
            if row.finished_at > at:
                return Liveness.CLOCK_AHEAD.value
            if at - row.finished_at > timedelta(seconds=gate.max_age_seconds):
                return Liveness.TOO_OLD.value
            classes = row.policy_classes
            if not isinstance(classes, list) or not all(isinstance(item, str) for item in classes):
                return Liveness.UNREADABLE.value
            if policy is not None and policy.value not in classes:
                return Liveness.POLICY_NOT_COVERED.value
            return None
    except (TypeError, AttributeError, ValueError, OverflowError):
        # A stored value this code cannot read as evidence is not evidence.
        return Liveness.UNREADABLE.value
    return Liveness.MISSING.value


def bounded_codes(codes: Sequence[str]) -> list[str]:
    """Distinct codes in first-seen order, at most `MAX_CODES` of them."""
    seen: list[str] = []
    for code in codes:
        if code not in seen:
            seen.append(code)
    return seen[:MAX_CODES]


__all__ = [
    "EXIT_JOB_STALE",
    "Coverage",
    "EvidenceRow",
    "ExitAssessment",
    "ExitHealth",
    "ExitLivenessGate",
    "ExitRunFacts",
    "JobName",
    "JobOutcome",
    "Liveness",
    "PolicyClass",
    "SweepFacts",
    "assess_exit_run",
    "bounded_codes",
    "judge_exit_liveness",
]
