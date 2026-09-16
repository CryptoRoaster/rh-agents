"""One bounded pass: intake, specialist steps, risk request, fill.

The whole run is a sequence of calls into services that already exist, each of
which owns its own transaction, its own idempotency and its own refusals. This
adds no workflow rule, no risk rule, no sizing rule and no accounting. What it
adds is an order, four budgets and a structured account of what happened.

**A run is not a transaction.** Steps that completed are committed and stay
committed; an interrupted step follows the contract of whatever it was doing —
a claimed task keeps its lease until it expires, a fill either committed or
rolled back. Wrapping the pass in one transaction would mean a crash at the end
discarded a fill that really happened, which is the opposite of safe.

**A run never waits for work to appear.** When no role can claim a task, the
pass ends. Work that is not due yet stays in the task table, which is where the
loop lives — durably, and not inside a process somebody has to keep alive.

**Order keys come from case identity, never from the run.** One case gets one
risk request and one fill, whichever run reaches it, so a second run after an
interruption replays rather than duplicating. A key derived from the run id
would make every restart a new order.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.core.models import TradingMode
from src.data.tables import TradeCaseRow
from src.orchestration.commander.context import SystemPauseUnavailable
from src.orchestration.commander.intake import IntakeRefusal
from src.orchestration.workflow.models import TERMINAL_CASE_STATUSES, TradeCaseStatus
from src.runner.composition import RunnerStack
from src.runner.models import (
    CaseProgress,
    ConfigurationRefused,
    RunLimits,
    RunReading,
    RunStop,
    RunSummary,
)

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


@dataclass
class _Budget:
    """What is left of this run, and the first bound that ran out."""

    limits: RunLimits
    deadline: datetime
    steps: int = 0
    opened: int = 0
    stop: RunStop = RunStop.NOTHING_LEFT_TO_DO

    def expired(self, now: datetime) -> bool:
        if now >= self.deadline:
            self.stop = RunStop.TIME_BUDGET_REACHED
            return True
        return False

    def stepped(self) -> bool:
        self.steps += 1
        if self.steps >= self.limits.max_steps:
            self.stop = RunStop.STEP_BUDGET_REACHED
            return False
        return True


class BoundedPaperRun:
    """Coordinates one explicit pass. Owns no rule and no state of its own."""

    def __init__(self, stack: RunnerStack, *, run_id: UUID | None = None) -> None:
        self.stack = stack
        # Observability only. Never an order, case, request or fill identity.
        self.run_id = run_id if run_id is not None else uuid4()

    async def execute(self) -> RunReading:
        stack = self.stack
        refusal = refuse(stack.settings)
        if refusal is not None:
            return refusal
        started = stack.clock.now()
        budget = _Budget(limits=stack.limits, deadline=started + stack.limits.runtime)
        errors: list[str] = []
        opened: list[UUID] = []
        intake_refusals: tuple[str, ...] = ()
        candidates = 0
        steps = 0

        try:
            candidates, opened, intake_refusals = await self._intake(budget)
            steps = await self._work(budget)
            progress = await self._decide(budget, errors)
        except SystemPauseUnavailable:
            # An unreadable stop is unknown, and unknown is not permission.
            return self._summary(
                started,
                budget,
                stop=RunStop.SYSTEM_STOPPED,
                errors=("SYSTEM_STOP_UNREADABLE",),
            )
        except (SQLAlchemyError, OSError):
            return self._summary(started, budget, errors=("DATABASE_UNAVAILABLE",))
        except asyncio.CancelledError:
            # Nothing half-written survives this: every service committed or
            # rolled back on its own before control came back here.
            raise

        return self._summary(
            started,
            budget,
            candidates=candidates,
            opened=len(opened),
            intake_refusals=intake_refusals,
            steps=steps,
            cases=progress,
            errors=tuple(errors),
        )

    # ------------------------------------------------------------- the pass

    async def _intake(self, budget: _Budget) -> tuple[int, list[UUID], tuple[str, ...]]:
        """One bounded intake cycle, through the existing control plane."""
        if budget.expired(self.stack.clock.now()):
            return 0, [], ()
        outcome = await self.stack.intake.run_cycle()
        refusals = tuple(sorted({reason.value for _, reason in outcome.refused}))
        if any(reason is IntakeRefusal.SYSTEM_PAUSED for _, reason in outcome.refused):
            budget.stop = RunStop.SYSTEM_STOPPED
        opened = [case.id for case in outcome.opened][: budget.limits.max_candidates]
        budget.opened = len(opened)
        return len(outcome.opened) + len(outcome.refused), opened, refusals

    async def _work(self, budget: _Budget) -> int:
        """Let every available role take steps until nobody can claim anything.

        A full sweep with no disposition anywhere is the end of the pass. There
        is no sleep and no retry: a task that is not due yet is not this run's
        to wait for.
        """
        if budget.stop is RunStop.SYSTEM_STOPPED:
            return 0
        runners = self.stack.runners
        if not runners:
            return 0
        for runner in runners:
            await runner.register()
        taken = 0
        progressed = True
        while progressed:
            progressed = False
            for runner in runners:
                if budget.expired(self.stack.clock.now()):
                    return taken
                disposition = await self._step(runner)
                if disposition is None:
                    continue
                taken += 1
                progressed = True
                if not budget.stepped():
                    return taken
        return taken

    async def _step(self, runner: object) -> object | None:
        """One claim, bounded by this run's own external wait.

        The timeout covers whatever the handler does, including a provider or
        model call. On expiry the task keeps its lease and is reclaimed by
        recovery rather than being marked failed by a process that stopped
        watching it.
        """
        try:
            return await asyncio.wait_for(
                runner.run_once(),  # type: ignore[attr-defined]
                timeout=self.stack.limits.step_timeout_seconds,
            )
        except TimeoutError:
            return None

    async def _decide(self, budget: _Budget, errors: list[str]) -> tuple[CaseProgress, ...]:
        """Ask SENTINEL about what became ready, and fill what it approved."""
        if budget.stop is RunStop.SYSTEM_STOPPED:
            return ()
        progress: list[CaseProgress] = []
        for trade_case_id in await self._cases():
            if budget.expired(self.stack.clock.now()):
                break
            if len(progress) >= budget.limits.max_cases:
                budget.stop = RunStop.CASE_BUDGET_REACHED
                break
            progress.append(await self._case(trade_case_id, errors))
        return tuple(progress)

    async def _cases(self) -> tuple[UUID, ...]:
        """Every non-terminal case, oldest first, bounded by the case budget.

        Read rather than remembered: a case opened by an earlier run is exactly
        as eligible as one opened by this one, and a run that only looked at its
        own would leave the others waiting forever.
        """
        async with self.stack.sessions() as session:
            rows = (
                await session.scalars(
                    select(TradeCaseRow.id)
                    .where(
                        TradeCaseRow.status.notin_([item.value for item in TERMINAL_CASE_STATUSES])
                    )
                    .order_by(TradeCaseRow.opened_at, TradeCaseRow.id)
                    .limit(self.stack.limits.max_cases)
                )
            ).all()
        return tuple(rows)

    async def _case(self, trade_case_id: UUID, errors: list[str]) -> CaseProgress:
        """One case, as far as its own state and the existing contracts allow."""
        stack = self.stack
        case = await stack.cases.get_trade_case(trade_case_id)
        if case.status not in (TradeCaseStatus.READY_FOR_RISK, TradeCaseStatus.RISK_APPROVED):
            # Waiting on something this run does not get to hurry along.
            return CaseProgress(
                trade_case_id=trade_case_id,
                status=case.status.value,
                reason_code=_code(case.reason_code),
            )
        # `RISK_APPROVED` means an earlier run already asked and was answered,
        # and was interrupted before it could act on the answer. Asking again
        # under the same key replays the stored verdict rather than producing a
        # second one, so the order this run completes is the order that was
        # authorised — and if its short window has since closed, the fill says
        # so rather than being granted an extension.
        key = order_key(trade_case_id)
        verdict = await stack.risk.request_risk_evaluation(trade_case_id, request_key=key)
        if verdict.kind == "risk_request_refused":
            return CaseProgress(
                trade_case_id=trade_case_id,
                status=case.status.value,
                risk_refusal=verdict.reason.value,
            )
        outcome = verdict.outcome.value
        if verdict.authorization.value != "APPROVED":
            # `LIMITED`, `REJECT` and `PAUSE_SYSTEM` all end here. No second key,
            # no downsize, no retry: one order gets one verdict.
            return CaseProgress(
                trade_case_id=trade_case_id,
                status=case.status.value,
                risk_outcome=outcome,
                replayed=verdict.replayed,
            )
        fill = await stack.fills.execute_case_fill(trade_case_id, request_key=key)
        if fill.kind == "execution_refused":
            return CaseProgress(
                trade_case_id=trade_case_id,
                status=fill.trade_case_status,
                risk_outcome=outcome,
                fill_refusal=fill.reason.value,
                replayed=fill.replayed,
            )
        return CaseProgress(
            trade_case_id=trade_case_id,
            status=fill.trade_case_status,
            risk_outcome=outcome,
            execution_id=fill.execution_id,
            replayed=fill.replayed,
        )

    # ----------------------------------------------------------- the account

    def _summary(
        self,
        started: datetime,
        budget: _Budget,
        *,
        stop: RunStop | None = None,
        candidates: int = 0,
        opened: int = 0,
        intake_refusals: tuple[str, ...] = (),
        steps: int = 0,
        cases: tuple[CaseProgress, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> RunSummary:
        filled = [item for item in cases if item.execution_id is not None]
        return RunSummary(
            run_id=self.run_id,
            started_at=started.isoformat(),
            finished_at=self.stack.clock.now().isoformat(),
            stop=stop if stop is not None else budget.stop,
            limits=self.stack.limits,
            roles=self.stack.roles,
            candidates_seen=candidates,
            cases_opened=opened,
            intake_refusals=intake_refusals,
            steps_taken=steps,
            cases=cases,
            risk_requests=len([item for item in cases if item.risk_outcome is not None]),
            fills=len(filled),
            replays=len([item for item in cases if item.replayed]),
            errors=errors,
        )


def _code(value: str | None) -> str | None:
    """Keep a reason code only when it really is one."""
    if value is None:
        return None
    safe = value.strip().upper()
    return safe if safe[:1].isalpha() and safe.replace("_", "").isalnum() else None
