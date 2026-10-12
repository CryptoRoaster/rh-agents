"""One exit phase: every triggered position of every exit policy, under one budget.

The policies decide *whether* a position is to be sold (`plan`). This module
decides *which one is asked next*, *whether there is still room to ask*, and
*what one attempt turned out to be* — for the normal and the early policy
together, so neither can quietly spend a budget of its own beside the other's.

**One attempt.** A call into `PaperExitService.execute_position_exit`: its own
on-chain read (ATLAS), the pre-exit refresh, the second judgement and the
booking under the account lock. Every started attempt counts against
`max_attempts`, whatever it ends as — so a run of refusals cannot become an
unbounded run of chain reads. Booked sales count against `max_exits` apart.

**Admission.** A new attempt starts only while the exit budget, the attempt
budget and the remaining time all allow it; the time check reserves one whole
attempt plus the time to settle its outcome. What cannot be started any more is
reported per position as *deferred*, by reason, never silently skipped.

**One attempt's time.** Bounded on its own (`attempt_seconds`), and never past
the run's deadline less the settle reserve. There is no total bound inside an
attempt to rely on instead: the chain read is a series of RPC and holder-source
calls, each with its own timeout, and the refresh has its own ceiling. An
attempt that outlives its bound is cancelled — before the lock nothing was
written; inside the transaction the cancellation rolls it back — and then
*settled*: the exit row under the attempt's key is looked up. Present, it was
booked and is counted as booked. Absent, the outcome is `EXIT_OUTCOME_UNKNOWN`,
never "failed": a commit cut off in flight may still land, and the database's
one-exit-per-cycle contract is what keeps the next run from selling twice.

**Order.** Deterministic per UTC minute and otherwise unrelated to identity:
each policy's triggered positions are permuted by a digest of the minute and
the position, and the two policies alternate, starting with the one the same
digest picks. A stable identity order would hand the same position the last
slot in every tight run. This is *not* a fairness guarantee across runs or
restarts — nothing here is persisted — only the absence of a systematic loser.
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import EXIT_SETTLE_SECONDS
from src.data.tables import TradeCaseExitRow
from src.markets.geckoterminal.networks import VerifiedNetworkRegistry
from src.orchestration.exitpolicy.refresh import (
    PreExitMarketRefresh,
    RefreshBudget,
    RefreshContext,
    RefreshDeadline,
    Unbounded,
)
from src.orchestration.paperexit.models import ExitReading, ExitRefusal, PaperExitRecorded
from src.orchestration.paperexit.service import PaperExitUnavailable

# What a position that was not attempted is reported as, by reason.
DEFERRED_RUN_BUDGET = "EXIT_DEFERRED_RUN_BUDGET"
DEFERRED_ATTEMPT_LIMIT = "EXIT_ATTEMPT_LIMIT_REACHED"
DEFERRED_EXIT_BUDGET = "EXIT_BUDGET_REACHED"
# An attempt cut off by its bound whose booking could not be confirmed either way.
OUTCOME_UNKNOWN = "EXIT_OUTCOME_UNKNOWN"
# Why a policy's judgement stopped short: its share of the run's time ran out
# between two positions, or a read inside it outlived the share and was cut off.
PLAN_TIME_BUDGET = "EXIT_PLAN_TIME_BUDGET_REACHED"
PLAN_TIMED_OUT = "EXIT_PLAN_TIMED_OUT"
# Refusals that leave nothing to sell: the trigger has gone on fresh evidence,
# or the holding was closed or removed meanwhile. Every other refusal of a
# triggered exit is an exit that did not happen.
SETTLED = frozenset(
    {
        ExitRefusal.EXIT_TRIGGER_CLEARED,
        ExitRefusal.POSITION_ALREADY_CLOSED,
        ExitRefusal.POSITION_NOT_FOUND,
    }
)
# Looking up a cut-off attempt's exit row: one indexed read by its unique key.
SETTLE_SECONDS = float(EXIT_SETTLE_SECONDS)


@dataclass
class ExitSweep:
    """What one policy's exits did in one phase. Counts and codes only."""

    evaluated: int = 0
    triggered: int = 0
    # Sales this phase booked. A replayed sale is counted under `replayed`.
    executed: int = 0
    held: int = 0
    triggers: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    # Attempts started (each one a chain read), sales that already existed under
    # their key, attempts cut off by their bound, and of those the ones whose
    # booking could not be confirmed.
    attempts: int = 0
    replayed: int = 0
    timed_out: int = 0
    unknown: int = 0
    # Attempts that could not be carried out at all (`PaperExitUnavailable`),
    # and judged positions held only because their mark was unknown.
    failed: int = 0
    mark_unknown: int = 0
    # Attempts answered with a refusal that leaves the triggered position held
    # for a reason other than the trigger being gone or the position being
    # closed already: an exit that should have happened and did not.
    blocked: int = 0
    # Triggered positions not attempted, by reason; positions of this policy not
    # judged at all, and why the judgement stopped short (`PLAN_*`), if it did.
    deferred: dict[str, int] = field(default_factory=dict)
    unevaluated: int = 0
    plan_stop: str | None = None
    # The pre-exit refresh and the second judgement of each trigger.
    refresh_attempts: int = 0
    refresh_failures: int = 0
    refresh_provider_requests: int = 0
    triggers_cleared: int = 0
    triggers_changed: int = 0
    reevaluated: dict[str, int] = field(default_factory=dict)
    max_refresh_seconds: float | None = None
    max_atlas_read_seconds: float | None = None
    max_mark_age_at_trigger_seconds: float | None = None
    max_mark_age_at_final_seconds: float | None = None

    def refused(self, code: str) -> None:
        self.refusals[code] = self.refusals.get(code, 0) + 1

    def defer(self, code: str) -> None:
        self.deferred[code] = self.deferred.get(code, 0) + 1

    def triggered_at(self, mark_age: float | None) -> None:
        self.max_mark_age_at_trigger_seconds = _larger(
            self.max_mark_age_at_trigger_seconds, mark_age
        )

    def measured(self, reading: ExitReading) -> None:
        """Fold one exit's freshness into the sweep's. Numbers and codes only."""
        freshness = reading.freshness
        if freshness is None:
            return
        self.refresh_attempts += freshness.refresh_attempts
        self.refresh_provider_requests += freshness.refresh_provider_requests
        if freshness.refresh_attempts and freshness.refresh_reason is not None:
            self.refresh_failures += 1
        if freshness.trigger_cleared:
            self.triggers_cleared += 1
        reevaluated = freshness.reevaluated_trigger
        if reevaluated is not None:
            self.reevaluated[reevaluated] = self.reevaluated.get(reevaluated, 0) + 1
            if reevaluated != freshness.original_trigger:
                self.triggers_changed += 1
        self.max_refresh_seconds = _larger(self.max_refresh_seconds, freshness.refresh_seconds)
        self.max_atlas_read_seconds = _larger(
            self.max_atlas_read_seconds, freshness.atlas_read_seconds
        )
        self.max_mark_age_at_final_seconds = _larger(
            self.max_mark_age_at_final_seconds, freshness.mark_age_at_final_seconds
        )


def _larger(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


@dataclass(frozen=True)
class ExitCandidate:
    """One triggered position, ready to be asked for its sale."""

    position_id: UUID
    request_key: str
    # The policy's own tally this attempt is counted in.
    result: ExitSweep
    port: PreExitMarketRefresh | None
    start: Callable[[RefreshContext], Awaitable[ExitReading]]


@dataclass(frozen=True)
class ExitLimits:
    """The whole exit phase's bounds, shared by every exit policy in it."""

    max_attempts: int
    max_exits: int
    max_refreshes: int
    max_provider_requests: int | None = None
    per_refresh_requests: int = 0
    # One attempt's ceiling; `None` bounds an attempt by the deadline alone.
    attempt_seconds: float | None = None
    settle_seconds: float = SETTLE_SECONDS

    @property
    def reserve_seconds(self) -> float:
        """What must remain for a new attempt to start: one attempt and its settling."""
        return (self.attempt_seconds or 0.0) + self.settle_seconds


@dataclass
class ExitBudget:
    """The exit phase's spending so far, against its limits."""

    limits: ExitLimits
    refresh: RefreshBudget = field(init=False)
    attempts: int = 0
    exits: int = 0
    deferred: dict[str, int] = field(default_factory=dict)
    unknown: int = 0

    def __post_init__(self) -> None:
        self.refresh = RefreshBudget(
            self.limits.max_refreshes,
            max_provider_requests=self.limits.max_provider_requests,
            per_refresh_requests=self.limits.per_refresh_requests,
        )

    def admit(self, deadline: RefreshDeadline) -> str | None:
        """Why no new attempt may start now, or `None` when one may."""
        if self.exits >= self.limits.max_exits:
            return DEFERRED_EXIT_BUDGET
        if self.attempts >= self.limits.max_attempts:
            return DEFERRED_ATTEMPT_LIMIT
        if deadline.expired or (
            self.limits.attempt_seconds is not None
            and deadline.remaining < self.limits.reserve_seconds
        ):
            return DEFERRED_RUN_BUDGET
        return None

    def ceiling(self, deadline: RefreshDeadline) -> float | None:
        """How long the attempt about to start may take, if anything bounds it."""
        bounds = [deadline.remaining - self.limits.settle_seconds]
        if self.limits.attempt_seconds is not None:
            bounds.append(self.limits.attempt_seconds)
        bound = min(bounds)
        return None if bound == float("inf") else max(0.001, bound)

    def defer(self, candidate: ExitCandidate, code: str) -> None:
        candidate.result.defer(code)
        self.deferred[code] = self.deferred.get(code, 0) + 1


class _Window:
    """A monotonic deadline of its own, never later than the one it was cut from."""

    def __init__(self, seconds: float) -> None:
        self._expires = time.monotonic() + max(0.0, seconds)

    @property
    def remaining(self) -> float:
        return self._expires - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining <= 0


class ExitPlanner(Protocol):
    async def plan(
        self,
        result: ExitSweep,
        *,
        deadline: RefreshDeadline | None = None,
        candidates: list[ExitCandidate] | None = None,
    ) -> list[ExitCandidate]: ...


async def plan_exits(
    stages: Sequence[tuple[ExitPlanner, ExitSweep]],
    budget: ExitBudget,
    deadline: RefreshDeadline,
) -> list[list[ExitCandidate]]:
    """Every policy's judgement, each on its own share of the time planning may take.

    Planning may take what the run has left less one attempt's reserve and one
    more settling margin, so a phase that can still sell keeps the time to;
    with less than that left it may take what remains, to be judged and
    reported, and admission then defers every attempt. Each policy is given an
    equal, fixed share of that: a policy whose judgement hangs is cut off at its
    own share and can never take the other's, and time a fast one leaves unused
    stays with the attempts.

    A cut-off judgement keeps every count it made and every candidate it named
    (they are written as they are made), and says it stopped: `PLAN_TIMED_OUT`
    when a read inside it was cut, `PLAN_TIME_BUDGET` when it stopped between
    positions. Database reads only: nothing here calls a provider or sells.
    """
    limits = budget.limits
    pool = deadline.remaining - limits.reserve_seconds - limits.settle_seconds
    seconds = (pool if pool > 0 else max(0.0, deadline.remaining)) / max(1, len(stages))
    plans: list[list[ExitCandidate]] = []
    for stage, result in stages:
        share = _Window(min(seconds, deadline.remaining))
        found: list[ExitCandidate] = []
        plans.append(found)
        if share.expired:
            result.plan_stop = PLAN_TIME_BUDGET
            continue
        try:
            await asyncio.wait_for(
                stage.plan(result, deadline=share, candidates=found),
                timeout=max(0.001, share.remaining),
            )
        except TimeoutError:
            result.plan_stop = PLAN_TIMED_OUT
    return plans


def exit_order(plans: Sequence[Sequence[ExitCandidate]], at: datetime) -> list[ExitCandidate]:
    """Every policy's candidates, permuted per minute and interleaved.

    Deterministic for one UTC minute, so a test and an operator can say what
    the order was; unrelated to identity order, so no position is always last.
    """
    minute = at.strftime("%Y%m%dT%H%M")
    queues = [
        sorted(plan, key=lambda item: _digest(f"{minute}:{item.position_id}"))
        for plan in plans
        if plan
    ]
    if not queues:
        return []
    first = int(_digest(minute), 16) % len(queues)
    queues = queues[first:] + queues[:first]
    ordered: list[ExitCandidate] = []
    for index in range(max(len(queue) for queue in queues)):
        ordered.extend(queue[index] for queue in queues if index < len(queue))
    return ordered


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def attempt_exits(
    candidates: Sequence[ExitCandidate],
    budget: ExitBudget,
    *,
    sessions: async_sessionmaker[AsyncSession],
    deadline: RefreshDeadline | None = None,
    networks: VerifiedNetworkRegistry | None = None,
) -> None:
    """Ask for each candidate's sale in order, while the budget admits one.

    Everything is counted into the candidates' own tallies as it happens, so an
    exception that ends the phase takes nothing already recorded with it.
    """
    bound = deadline if deadline is not None else Unbounded()
    for index, candidate in enumerate(candidates):
        code = budget.admit(bound)
        if code is not None:
            for rest in candidates[index:]:
                budget.defer(rest, code)
            return
        await _attempt(candidate, budget, sessions, bound, networks)


async def _attempt(
    candidate: ExitCandidate,
    budget: ExitBudget,
    sessions: async_sessionmaker[AsyncSession],
    deadline: RefreshDeadline,
    networks: VerifiedNetworkRegistry | None,
) -> None:
    result = candidate.result
    budget.attempts += 1
    result.attempts += 1
    context = RefreshContext(
        port=candidate.port, budget=budget.refresh, deadline=deadline, networks=networks
    )
    ceiling = budget.ceiling(deadline)
    try:
        work = candidate.start(context)
        reading = await (work if ceiling is None else asyncio.wait_for(work, timeout=ceiling))
    except PaperExitUnavailable as failure:
        # Unknown is not permission: nothing was sold, the next run asks again.
        result.failed += 1
        result.refused(str(failure))
        return
    except TimeoutError:
        result.timed_out += 1
        if await _booked(sessions, candidate.request_key, budget.limits.settle_seconds):
            # Cut off after its commit: the sale exists, and this attempt made it.
            result.executed += 1
            budget.exits += 1
        else:
            result.unknown += 1
            budget.unknown += 1
            result.refused(OUTCOME_UNKNOWN)
        return
    result.measured(reading)
    if isinstance(reading, PaperExitRecorded):
        if reading.replayed:
            # The sale already existed under this key: recorded once, not by this attempt.
            result.replayed += 1
        else:
            result.executed += 1
            budget.exits += 1
    else:
        result.refused(reading.reason.value)
        if reading.reason not in SETTLED:
            result.blocked += 1


async def _booked(
    sessions: async_sessionmaker[AsyncSession], request_key: str, seconds: float
) -> bool:
    """Whether an exit row exists under this key. A read that fails confirms nothing."""

    async def read() -> Any:
        async with sessions() as session:
            return await session.scalar(
                select(TradeCaseExitRow.exit_id).where(TradeCaseExitRow.request_key == request_key)
            )

    try:
        return await asyncio.wait_for(read(), timeout=seconds) is not None
    except (TimeoutError, SQLAlchemyError, OSError):
        return False


def alone(
    *,
    max_exits: int,
    max_attempts: int | None,
    max_refreshes: int | None,
) -> ExitBudget:
    """The budget of one policy's sweep run on its own, outside a run's exit phase."""
    return ExitBudget(
        ExitLimits(
            max_attempts=max_attempts if max_attempts is not None else max_exits,
            max_exits=max_exits,
            max_refreshes=max_refreshes if max_refreshes is not None else max_exits,
        )
    )


__all__ = [
    "DEFERRED_ATTEMPT_LIMIT",
    "DEFERRED_EXIT_BUDGET",
    "DEFERRED_RUN_BUDGET",
    "OUTCOME_UNKNOWN",
    "PLAN_TIMED_OUT",
    "PLAN_TIME_BUDGET",
    "SETTLE_SECONDS",
    "ExitBudget",
    "ExitCandidate",
    "ExitLimits",
    "ExitPlanner",
    "ExitSweep",
    "alone",
    "attempt_exits",
    "exit_order",
    "plan_exits",
]
