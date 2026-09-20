"""One bounded pass: intake, specialist steps, risk request, fill.

The whole run is a sequence of calls into services that already exist, each of
which owns its own transaction, its own idempotency and its own refusals. This
adds no workflow rule, no risk rule, no sizing rule and no accounting. What it
adds is an order, four budgets, and a structured account of what actually
happened.

The budgets, precisely
----------------------

**Candidates.** Enforced *inside* intake, by lowering the control policy's own
per-cycle ceiling before the cycle runs. Nothing is opened and then discarded: a
case that was never allowed is never created.

**Cases.** The maximum number of *distinct trade cases this run works on*, across
all three stages — intake, worker steps and the decision path. A case counts once
however many times it is touched. When the budget is full, claims are narrowed to
the cases already being worked, in the claim query itself, so no task belonging to
somebody else is taken and then dropped.

**Steps.** Service calls that may change something: the intake cycle, each worker
attempt that actually claimed a task, each risk request, each fill. Checked
*before* the next such call, never after. An attempt that began and was cut off
counts — work was started, and pretending otherwise would let a timing-out run
spend an unbounded number of them.

**Runtime.** A monotonic deadline, so a clock adjustment cannot extend or end a
run. Checked before every mutating step, and every wait is bounded by whatever is
left of it. The trusted clock is untouched: it still decides evidence freshness,
the approval window and the fill instant, which are business facts and not
scheduling.

What a run is not
-----------------

**Not a transaction.** Steps that completed are committed and stay committed; an
interrupted step follows the contract of whatever it was doing. Wrapping the pass
in one transaction would mean a crash at the end discarded a fill that really
happened.

**Not a loop.** When no role can claim another task, the pass ends. Work that is
not due yet stays in the task table, which is where the loop lives — durably, not
inside a process somebody has to keep alive.

**Not an identity.** Order keys come from case identity, so a second run after an
interruption replays rather than duplicating. A key derived from the run id would
make every restart a new order.
"""

import asyncio
import time
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.core.models import TradingMode
from src.data.tables import TradeCaseRow
from src.orchestration.commander.context import SystemPauseUnavailable
from src.orchestration.commander.intake import IntakeRefusal
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.riskrequest.service import stale_source
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    SourceRefreshOutcome,
    TradeCaseStatus,
)
from src.runner.acquisition import disabled as no_acquisition
from src.runner.composition import RunnerStack
from src.runner.models import (
    AcquisitionStop,
    CaseProgress,
    ConfigurationRefused,
    MarketAcquisition,
    RunLimits,
    RunReading,
    RunStop,
    RunSummary,
    SourceRefresh,
)

T = TypeVar("T")

# The one key one case's order is addressed by, in both directions. The risk
# request stores it and the fill is refused unless it matches, so the two are
# deliberately the same string derived from the same identity.
ORDER_PREFIX = "paper-run"


def order_key(trade_case_id: UUID) -> str:
    """The business identity of this case's one entry order.

    Derived from the case, which survives a restart, and never from the run id
    or the clock, which do not. A retry addresses the same order and replays; a
    key carrying anything per-run would mint a second one instead.
    """
    return f"{ORDER_PREFIX}:{trade_case_id}"


def refuse(settings: Settings) -> ConfigurationRefused | None:
    """Every reason a run may not start, checked before anything can write."""
    if not settings.paper_runner_enabled:
        return ConfigurationRefused(reason="PAPER_RUNNER_NOT_ENABLED")
    if settings.trading_mode is not TradingMode.PAPER:
        return ConfigurationRefused(
            reason="TRADING_MODE_NOT_PAPER", detail=settings.trading_mode.value
        )
    if settings.commander_kill_switch:
        return ConfigurationRefused(reason="KILL_SWITCH_ENGAGED")
    return None


class Deadline:
    """The run's own clock, monotonic and unrelated to any business time.

    A wall clock can move backwards, and a run whose end depended on one could
    be extended or cut short by an adjustment nobody made for that reason.
    """

    def __init__(self, seconds: float) -> None:
        self._expires = time.monotonic() + seconds

    @property
    def remaining(self) -> float:
        return self._expires - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def within(self, ceiling: float) -> float:
        """The longest a single wait may last: whichever bound is nearer."""
        return max(0.0, min(ceiling, self.remaining))


@dataclass
class Account:
    """What this run has actually done, kept as it happens.

    Accumulated rather than assembled at the end, so a failure in a later stage
    cannot erase work an earlier one already committed. Reporting zero fills
    because the summary was built after something raised would be a false
    account of a real fill.
    """

    limits: RunLimits
    stop: RunStop = RunStop.NOTHING_LEFT_TO_DO
    # What this run asked the market provider for, before it traded anything.
    acquisition: MarketAcquisition | None = None
    candidates_seen: int = 0
    cases_opened: int = 0
    intake_refusals: tuple[str, ...] = ()
    intake_unknown: bool = False
    steps: int = 0
    steps_timed_out: int = 0
    cases: dict[UUID, CaseProgress] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    touched: set[UUID] = field(default_factory=set)
    # Which sources this run has already ordered a new observation of, per case.
    # One order each, for the whole pass: without that a case needing two of
    # them would have this process alternating between the two gaps, which is
    # retry-until-pass wearing a second name.
    refreshed: dict[UUID, set[str]] = field(default_factory=dict)

    def admits(self, trade_case_id: UUID) -> bool:
        """Whether this run may work on that case, counting it if it may."""
        if trade_case_id in self.touched:
            return True
        if len(self.touched) >= self.limits.max_cases:
            self.stop = RunStop.CASE_BUDGET_REACHED
            return False
        self.touched.add(trade_case_id)
        return True

    @property
    def full(self) -> bool:
        return len(self.touched) >= self.limits.max_cases

    def may_step(self, deadline: Deadline) -> bool:
        """Whether another mutating step may begin. Asked before, never after."""
        if deadline.expired:
            self.stop = RunStop.TIME_BUDGET_REACHED
            return False
        if self.steps >= self.limits.max_steps:
            self.stop = RunStop.STEP_BUDGET_REACHED
            return False
        return True

    def record(self, progress: CaseProgress) -> None:
        self.cases[progress.trade_case_id] = progress

    def outstanding(self, trade_case_id: UUID, origin: str) -> bool:
        """Whether this run may still order a new observation of that source."""
        return origin not in self.refreshed.setdefault(trade_case_id, set())

    def fail(self, code: str) -> None:
        if code not in self.errors:
            self.errors.append(code)


class BoundedPaperRun:
    """Coordinates one explicit pass. Owns no rule and no state of its own."""

    def __init__(
        self,
        stack: RunnerStack,
        *,
        run_id: UUID | None = None,
        deadline: Deadline | None = None,
    ) -> None:
        self.stack = stack
        # Observability only. Never an order, case, request or fill identity.
        self.run_id = run_id if run_id is not None else uuid4()
        # The seam a test uses to hand the run a deadline that has already
        # passed, rather than waiting out a real one. Production supplies none
        # and gets the configured runtime, measured monotonically.
        self._deadline = deadline

    async def execute(self) -> RunReading:
        stack = self.stack
        refusal = refuse(stack.settings)
        if refusal is not None:
            return refusal
        started = stack.clock.now()
        account = Account(limits=stack.limits)
        deadline = (
            self._deadline
            if self._deadline is not None
            else Deadline(stack.limits.max_runtime_seconds)
        )
        try:
            if not await self._acquire(account, deadline):
                # Either a stop was in force before anything was asked, or an
                # observation may or may not have been recorded. Both end the
                # pass before a single mutating trading stage runs.
                return self._summary(started, account)
            await self._intake(account, deadline)
            if account.intake_unknown:
                # The cycle committed an unknown number of cases, and this run
                # cannot say which. Carrying on would hand the case budget out a
                # second time — the working set is read from the database, and
                # what intake opened is not in it — so the pass ends here. What
                # committed stays committed and is ordinary work for the next
                # explicit run.
                return self._summary(started, account)
            await self._work(account, deadline)
            await self._decide(account, deadline)
        except SystemPauseUnavailable:
            # An unreadable stop is unknown, and unknown is not permission.
            account.stop = RunStop.SYSTEM_STOPPED
            account.fail("SYSTEM_STOP_UNREADABLE")
        except TimeoutError:
            # A wait outlived the run. Whatever completed before it stands.
            account.stop = RunStop.TIME_BUDGET_REACHED
        except (SQLAlchemyError, OSError):
            account.fail("DATABASE_UNAVAILABLE")
        except asyncio.CancelledError:
            # Nothing half-written survives this: every service committed or
            # rolled back on its own before control came back here.
            raise
        return self._summary(started, account)

    # ------------------------------------------------------------- the pass

    async def _acquire(self, account: Account, deadline: Deadline) -> bool:
        """Observe the markets the open work depends on, and say whether to go on.

        The stage is absent unless an operator switched it on, and an absent one
        is reported as absent rather than omitted: a run that traded on data
        nobody refreshed and a run that refreshed it look identical in a summary
        that says nothing about either.

        Acquisition is never a permission. It can only end the pass early, and
        only for two reasons: a stop was in force, or something may have been
        written and may not have been. Everything else — a provider that failed,
        a budget that ran out, a market that came back unavailable — leaves the
        run to proceed exactly as it would have without this stage, with the
        existing contracts blocking whatever the data does not support.
        """
        stage = self.stack.acquisition
        if stage is None:
            account.acquisition = no_acquisition()
            return True
        account.acquisition = await stage.execute(deadline)
        stop = account.acquisition.stop
        if stop == AcquisitionStop.SYSTEM_STOP_UNREADABLE.value:
            # A safety question this deployment cannot answer. Unknown is not
            # permission, and it is also not a healthy run.
            account.stop = RunStop.SYSTEM_STOPPED
            account.fail("SYSTEM_STOP_UNREADABLE")
            return False
        if stop == AcquisitionStop.SYSTEM_STOPPED.value:
            account.stop = RunStop.SYSTEM_STOPPED
            return False
        if stop == AcquisitionStop.DATABASE_UNAVAILABLE.value:
            account.fail("DATABASE_UNAVAILABLE")
            return False
        if stop == AcquisitionStop.CONFIGURATION_REFUSED.value:
            # Switched on and not performable. Carrying on would trade over
            # whatever happened to be recorded while reporting a stage that
            # never ran, so the pass ends and says which it was.
            account.fail("ACQUISITION_NOT_CONFIGURED")
            return False
        if account.acquisition.outcome_unknown:
            # The conservative stop. What committed stays committed and is
            # ordinary work for the next explicit run, which finds out what
            # really happened by observing the same market again.
            account.stop = RunStop.ACQUISITION_OUTCOME_UNKNOWN
            account.fail("ACQUISITION_OUTCOME_UNKNOWN")
            return False
        if deadline.expired:
            account.stop = RunStop.TIME_BUDGET_REACHED
            return False
        return True

    async def _bounded(self, work: Coroutine[Any, Any, T], deadline: Deadline) -> T:
        """Await something that touches the world, never past the deadline.

        Applied to intake, registration, the risk request and the fill as much
        as to a handler: a database that has stopped answering would otherwise
        make the runtime bound a suggestion.
        """
        return await asyncio.wait_for(work, timeout=max(0.001, deadline.remaining))

    async def _intake(self, account: Account, deadline: Deadline) -> None:
        """One bounded intake cycle, through the existing control plane.

        The candidate budget is already inside the policy this service was built
        with, so nothing is opened beyond it and nothing has to be discarded
        afterwards.
        """
        if not account.may_step(deadline):
            return
        account.steps += 1
        try:
            outcome = await self._bounded(
                self.stack.intake.run_cycle(limit=self.stack.limits.max_candidates), deadline
            )
        except (SystemPauseUnavailable, asyncio.CancelledError):
            raise
        except TimeoutError:
            # Out of time inside the cycle. Both facts are true and both are
            # reported: the run reached its deadline, and what intake had
            # already committed cannot be counted.
            account.intake_unknown = True
            account.stop = RunStop.TIME_BUDGET_REACHED
            account.fail("INTAKE_OUTCOME_UNKNOWN")
            return
        except Exception:
            # The cycle commits one case at a time, so it may have opened some
            # before it stopped. This run cannot say how many without counting
            # rows another run may have written, so it names the outcome as
            # unconfirmed and counts nothing.
            account.intake_unknown = True
            account.fail("INTAKE_OUTCOME_UNKNOWN")
            return
        account.candidates_seen = len(outcome.opened) + len(outcome.refused)
        account.intake_refusals = tuple(sorted({reason.value for _, reason in outcome.refused}))
        if any(reason is IntakeRefusal.SYSTEM_PAUSED for _, reason in outcome.refused):
            account.stop = RunStop.SYSTEM_STOPPED
            return
        for case in outcome.opened:
            if not account.admits(case.id):
                break
            account.cases_opened += 1

    async def _work(self, account: Account, deadline: Deadline) -> None:
        """Let every available role take steps until nobody can claim anything.

        A full sweep with no disposition anywhere is the end of the pass. There
        is no sleep and no retry: a task that is not due yet is not this run's
        to wait for, and a monitor that rescheduled itself is not due again in
        this pass either.
        """
        if account.stop is RunStop.SYSTEM_STOPPED:
            return
        runners = self.stack.runners
        if not runners:
            return
        # The working set, decided before a single claim is made. Every claim is
        # narrowed to it in the query, so a task outside the budget is never
        # taken and then dropped — a dropped claim is a lease nobody is working,
        # held for as long as the lease lasts.
        if deadline.expired:
            account.stop = RunStop.TIME_BUDGET_REACHED
            return
        scope = await self._working_set(account, deadline)
        if not scope:
            return
        if not await self._register(account, deadline):
            return
        await self._drain(scope, account, deadline)

    async def _register(self, account: Account, deadline: Deadline) -> bool:
        """Announce every runner once, and say whether the budget allowed it.

        Registration writes a worker instance rather than touching a case, so it
        is bounded but not counted as a step. A runner that never registered
        cannot claim, which is why a partial registration ends the stage.
        """
        for runner in self.stack.runners:
            if not account.may_step(deadline):
                return False
            await self._bounded(runner.register(), deadline)
        return True

    async def _drain(self, scope: frozenset[UUID], account: Account, deadline: Deadline) -> None:
        """Let every role claim within `scope` until nobody can claim again.

        The same loop whether it is working the whole pass or one case whose
        source was just re-observed: a narrower scope is a narrower claim query,
        not a different kind of work.
        """
        progressed = True
        while progressed:
            progressed = False
            for runner in self.stack.runners:
                if not account.may_step(deadline):
                    return
                account.steps += 1
                claimed, timed_out = await self._step(runner, scope, deadline)
                if timed_out:
                    account.steps_timed_out += 1
                    # Started work that did not finish. It counts, and the run
                    # stops asking this role for more in this pass.
                    return
                if claimed is None:
                    # Nothing to claim is not work: give the step back.
                    account.steps -= 1
                    continue
                progressed = True

    async def _working_set(self, account: Account, deadline: Deadline) -> frozenset[UUID]:
        """The cases this pass may work on, fixed before any claim.

        Whatever intake opened comes first — this run already spent part of its
        allowance on those — and the rest is topped up from the cases that are
        already alive, oldest first. Deciding it up front is what lets the claim
        be narrowed in the query rather than after the fact.
        """
        if account.full:
            return frozenset(account.touched)
        for trade_case_id in await self._open_cases(deadline):
            if not account.admits(trade_case_id):
                break
        return frozenset(account.touched)

    async def _open_cases(self, deadline: Deadline) -> tuple[UUID, ...]:
        async def read() -> tuple[UUID, ...]:
            async with self.stack.sessions() as session:
                rows = (
                    await session.scalars(
                        select(TradeCaseRow.id)
                        .where(
                            TradeCaseRow.status.notin_(
                                [item.value for item in TERMINAL_CASE_STATUSES]
                            )
                        )
                        .order_by(TradeCaseRow.opened_at, TradeCaseRow.id)
                        .limit(self.stack.limits.max_cases)
                    )
                ).all()
            return tuple(rows)

        return await self._bounded(read(), deadline)

    async def _step(
        self, runner: Any, scope: frozenset[UUID], deadline: Deadline
    ) -> tuple[UUID | None, bool]:
        """One claim, bounded by the nearer of the step timeout and the deadline.

        Returns the case that was claimed, if any, and whether the attempt timed
        out. Those are different facts: an empty claim means there was no work, a
        timeout means work began and was cut off. Reporting them as one would
        hide a handler that hangs behind a queue that is simply empty.

        On expiry `wait_for` cancels the handler and awaits that cancellation,
        so nothing continues in the background. The task keeps its lease and is
        reclaimed by recovery rather than being marked failed by a process that
        stopped watching it.
        """
        budget = deadline.within(self.stack.limits.step_timeout_seconds)
        if budget <= 0:
            return None, False
        try:
            disposition = await asyncio.wait_for(
                runner.run_once(trade_case_ids=scope), timeout=budget
            )
        except TimeoutError:
            # The claim happened; the handler did not finish. The lease the
            # runner recorded says which case spent the budget.
            lease = runner.last_lease
            return (None if lease is None else lease.trade_case_id), True
        if disposition is None:
            return None, False
        lease = runner.last_lease
        return (None if lease is None else lease.trade_case_id), False

    async def _decide(self, account: Account, deadline: Deadline) -> None:
        """Ask SENTINEL about what became ready, and fill what it approved."""
        if account.stop is RunStop.SYSTEM_STOPPED:
            return
        if deadline.expired:
            # Out of time before the first read. A query started now would be
            # cancelled mid-flight, which costs a round trip and answers nothing.
            account.stop = RunStop.TIME_BUDGET_REACHED
            return
        for trade_case_id in await self._cases(account, deadline):
            if not account.may_step(deadline):
                return
            if not account.admits(trade_case_id):
                return
            await self._case(trade_case_id, account, deadline)

    async def _cases(self, account: Account, deadline: Deadline) -> tuple[UUID, ...]:
        """Every non-terminal case, oldest first, bounded by the case budget.

        Read rather than remembered: a case opened by an earlier run is exactly
        as eligible as one opened by this one, and a run that only looked at its
        own would leave the others waiting forever. Cases this run already
        worked on come first, because they are already inside the budget.
        """

        async def read() -> tuple[UUID, ...]:
            async with self.stack.sessions() as session:
                rows = (
                    await session.scalars(
                        select(TradeCaseRow.id)
                        .where(
                            TradeCaseRow.status.notin_(
                                [item.value for item in TERMINAL_CASE_STATUSES]
                            )
                        )
                        .order_by(TradeCaseRow.opened_at, TradeCaseRow.id)
                        .limit(self.stack.limits.max_cases * 2)
                    )
                ).all()
            return tuple(rows)

        found = await self._bounded(read(), deadline)
        known = [item for item in found if item in account.touched]
        return tuple(known + [item for item in found if item not in account.touched])

    async def _case(self, trade_case_id: UUID, account: Account, deadline: Deadline) -> None:
        """One case, as far as its own state and the existing contracts allow."""
        stack = self.stack
        case = await self._bounded(stack.cases.get_trade_case(trade_case_id), deadline)
        refreshes: list[SourceRefresh] = []
        if case.status is TradeCaseStatus.BLOCKED:
            # A case can be blocked on evidence that is merely old — an execution
            # assessment outlives its quotes long before the setup it serves runs
            # out. That is the one blocker a new observation answers, and the
            # workflow decides whether this is one of those: what reaches it is
            # the evidence its own published blockers name.
            refreshes += await self._refresh_round(
                trade_case_id,
                self._blocked_sources(case),
                account,
                deadline,
            )
            if any(item.outcome == SourceRefreshOutcome.ORDERED.value for item in refreshes):
                case = await self._bounded(stack.cases.get_trade_case(trade_case_id), deadline)
        if case.status not in (TradeCaseStatus.READY_FOR_RISK, TradeCaseStatus.RISK_APPROVED):
            # Waiting on something this run does not get to hurry along. Not a
            # step: nothing was asked of anything that could change.
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=case.status.value,
                    reason_code=_code(case.reason_code),
                    refreshes=tuple(refreshes),
                )
            )
            return
        # `RISK_APPROVED` means an earlier run already asked and was answered,
        # and was interrupted before it could act on the answer. Asking again
        # under the same key replays the stored verdict rather than producing a
        # second one, so the order this run completes is the order that was
        # authorised — and if its short window has since closed, the fill says
        # so rather than being granted an extension.
        key = order_key(trade_case_id)
        if not account.may_step(deadline):
            # Bringing this case back to where a request is possible was work,
            # and work spends the budget. Asked before the call rather than
            # corrected after it: the refresh above is committed and stays
            # committed, and the one request this case is entitled to belongs to
            # whichever explicit run can still afford it — under the same key.
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=case.status.value,
                    reason_code=_code(case.reason_code),
                    refreshes=tuple(refreshes),
                )
            )
            return
        account.steps += 1
        verdict = await self._attempt(
            stack.risk.request_risk_evaluation(trade_case_id, request_key=key),
            deadline,
            trade_case_id,
            account,
            status=case.status.value,
        )
        if verdict is None:
            return
        if verdict.kind == "risk_request_refused":
            # A precondition that has merely aged out is the one refusal a run
            # can do something about, and the only moment to do it is now —
            # before the case's one request is spent on readings nobody would
            # accept.
            verdict = await self._refresh(
                trade_case_id, verdict, key, account, deadline, refreshes, case.status.value
            )
            if verdict is None:
                return
        if verdict.kind == "risk_request_refused":
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=case.status.value,
                    risk_refusal=verdict.reason.value,
                    refreshes=tuple(refreshes),
                )
            )
            return
        outcome = verdict.outcome.value
        # Written into the account the moment the verdict returns, and before
        # anything is done with it. The risk request is its own transaction; a
        # fill that then fails must not take a committed verdict down with it.
        account.record(
            CaseProgress(
                trade_case_id=trade_case_id,
                status=case.status.value,
                risk_outcome=outcome,
                replayed=verdict.replayed,
                refreshes=tuple(refreshes),
            )
        )
        if verdict.authorization.value != "APPROVED":
            # `LIMITED`, `REJECT` and `PAUSE_SYSTEM` all end here. No second key,
            # no downsize, no retry: one order gets one verdict. Already
            # recorded above.
            return
        if not account.may_step(deadline):
            # Approved and not acted on. The verdict stands and is recorded; the
            # next explicit run may still complete it, or find its window closed.
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=case.status.value,
                    risk_outcome=outcome,
                    replayed=verdict.replayed,
                    refreshes=tuple(refreshes),
                )
            )
            return
        account.steps += 1
        fill = await self._attempt(
            stack.fills.execute_case_fill(trade_case_id, request_key=key),
            deadline,
            trade_case_id,
            account,
            status=case.status.value,
            risk_outcome=outcome,
        )
        if fill is None:
            return
        if fill.kind == "execution_refused":
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=fill.trade_case_status,
                    risk_outcome=outcome,
                    fill_refusal=fill.reason.value,
                    replayed=fill.replayed,
                    refreshes=tuple(refreshes),
                )
            )
            return
        account.record(
            CaseProgress(
                trade_case_id=trade_case_id,
                status=fill.trade_case_status,
                risk_outcome=outcome,
                execution_id=fill.execution_id,
                replayed=fill.replayed,
                refreshes=tuple(refreshes),
            )
        )

    def _blocked_sources(self, case: Any) -> tuple[str, ...]:
        """The refreshable origins a blocked case's own blockers point at.

        Read from what the workflow published about this case, mapped through
        the workflow's own table. The run keeps no idea of its own about who
        observes what, and asking about an origin the workflow will refuse costs
        one typed answer rather than a wrong guess.
        """
        policy = self.stack.cases.policy
        found = []
        for blocker in case.blockers:
            if blocker.evidence_type is None:
                continue
            declared = policy.refreshable_for_evidence(blocker.evidence_type)
            if declared is not None and declared.origin not in found:
                found.append(declared.origin)
        return tuple(found)

    def _refusal_sources(self, refusal: Any) -> tuple[str, ...]:
        """The refreshable origins one refusal points at.

        Two vocabularies, both the services' own. SENTINEL's pre-request bound
        names a source label when it stops on one; the readiness contract names
        an origin for every fact it could not use. Only the gaps whose cause is
        age map to anything — configuration that was never supplied and evidence
        that established nothing are refusals with their own remedies, and
        answering them by asking somebody to look again would turn every one of
        them into work.
        """
        policy = self.stack.cases.policy
        found: list[str] = []
        candidates = []
        if refusal.reason is RiskRequestRefusal.SOURCE_OLDER_THAN_RISK_LIMIT:
            label = stale_source(refusal.detail)
            if label is not None:
                candidates.append(policy.refreshable_for_label(label))
        candidates += [policy.refreshable_for_gap(gap) for gap in refusal.data_gaps]
        for declared in candidates:
            if declared is not None and declared.origin not in found:
                found.append(declared.origin)
        return tuple(found)

    async def _refresh_round(
        self,
        trade_case_id: UUID,
        origins: tuple[str, ...],
        account: Account,
        deadline: Deadline,
    ) -> list[SourceRefresh]:
        """Order every observation this case still needs, then let them be made.

        One round: the orders are placed together and the handlers that owe them
        run in one sweep, so a case needing two observers does not pay for two
        passes and the second observation is not made against a first that has
        meanwhile aged. Nothing is ordered twice — the run remembers what it has
        already asked for, and the workflow refuses a duplicate anyway.
        """
        placed: list[SourceRefresh] = []
        for origin in origins:
            if not account.outstanding(trade_case_id, origin):
                continue
            if not account.may_step(deadline):
                return placed
            account.refreshed[trade_case_id].add(origin)
            account.steps += 1
            order = await self._bounded(
                self.stack.cases.refresh_source(trade_case_id, origin), deadline
            )
            placed.append(
                SourceRefresh(
                    origin=origin,
                    outcome=order.outcome.value,
                    role=None if order.role is None else order.role.value,
                    attempt=order.attempt,
                )
            )
        if not any(item.outcome == SourceRefreshOutcome.ORDERED.value for item in placed):
            return placed
        if not await self._register(account, deadline):
            return placed
        # Scoped to this case alone. The budget already counts it, and widening
        # the claim here would let a refresh spend the pass on somebody else.
        await self._drain(frozenset({trade_case_id}), account, deadline)
        return placed

    async def _refresh(
        self,
        trade_case_id: UUID,
        refusal: Any,
        key: str,
        account: Account,
        deadline: Deadline,
        refreshes: list[SourceRefresh],
        status: str,
    ) -> Any:
        """Establish the preconditions this case is missing, then ask again.

        The refusal names what it stopped on. Where the workflow declares a task
        that observes that source, this orders one new observation of each,
        lets those handlers run under the run's own budgets, and re-asks *under
        the same request key* — so the case still has exactly one canonical
        request, and this is that question asked once what it needs is in place
        rather than a second request invented for a second chance.

        It repeats only while the answer changes to name a source this run has
        not already ordered. Since each is ordered at most once per case per
        pass, the loop is bounded by the number of sources the workflow declares
        refreshable at all — two — and cannot become a run that alternates
        between two gaps until one of them passes.

        Everything it does is an ordinary step against the same step and time
        budgets, and when a new observation does not arrive the refusal that was
        already there is what gets reported, unchanged.
        """
        while refusal is not None and refusal.kind == "risk_request_refused":
            wanted = tuple(
                origin
                for origin in self._refusal_sources(refusal)
                if account.outstanding(trade_case_id, origin)
            )
            if not wanted:
                return refusal
            placed = await self._refresh_round(trade_case_id, wanted, account, deadline)
            refreshes += placed
            if not any(item.outcome == SourceRefreshOutcome.ORDERED.value for item in placed):
                return refusal
            if not account.may_step(deadline):
                return refusal
            account.steps += 1
            answer = await self._attempt(
                self.stack.risk.request_risk_evaluation(trade_case_id, request_key=key),
                deadline,
                trade_case_id,
                account,
                status=status,
            )
            if answer is None:
                return None
            refusal = answer
        return refusal

    async def _attempt(
        self,
        work: Awaitable[Any],
        deadline: Deadline,
        trade_case_id: UUID,
        account: Account,
        *,
        status: str,
        risk_outcome: str | None = None,
    ) -> Any | None:
        """Run one decisive call, and be honest when its outcome is unknown.

        A call cut off by the deadline may have committed or may not have. The
        run does not know and does not guess: the case is recorded with an
        unknown outcome, and the next explicit run finds whatever really
        happened by addressing the same order key.
        """
        try:
            return await asyncio.wait_for(work, timeout=max(0.001, deadline.remaining))
        except TimeoutError:
            account.stop = RunStop.TIME_BUDGET_REACHED
            account.record(
                CaseProgress(
                    trade_case_id=trade_case_id,
                    status=status,
                    risk_outcome=risk_outcome,
                    outcome_unknown=True,
                )
            )
            return None

    # ----------------------------------------------------------- the account

    def _summary(self, started: Any, account: Account) -> RunSummary:
        cases = tuple(account.cases[key] for key in sorted(account.cases, key=str))
        return RunSummary(
            run_id=self.run_id,
            started_at=started.isoformat(),
            finished_at=self.stack.clock.now().isoformat(),
            stop=account.stop,
            limits=self.stack.limits,
            roles=self.stack.roles,
            acquisition=account.acquisition,
            candidates_seen=account.candidates_seen,
            cases_opened=account.cases_opened,
            intake_refusals=account.intake_refusals,
            intake_outcome_unknown=account.intake_unknown,
            steps_taken=account.steps,
            steps_timed_out=account.steps_timed_out,
            cases=cases,
            risk_requests=len([item for item in cases if item.risk_outcome is not None]),
            fills=len([item for item in cases if item.execution_id is not None]),
            replays=len([item for item in cases if item.replayed]),
            errors=tuple(account.errors),
        )


def _code(value: str | None) -> str | None:
    """Keep a reason code only when it really is one."""
    if value is None:
        return None
    safe = value.strip().upper()
    return safe if safe[:1].isalpha() and safe.replace("_", "").isalnum() else None
