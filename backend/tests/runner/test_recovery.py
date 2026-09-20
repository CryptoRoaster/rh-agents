"""Interruption, and what the next explicit run finds.

A run is not one transaction. Steps that committed stay committed; the step that
was interrupted follows its own contract. What must never happen is a second
order, a second fill, or a booking that only half happened.
"""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.data.tables import (
    ExecutionRow,
    OrderRow,
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseRiskRequestRow,
)
from src.runner.models import ExitCode
from src.runner.service import BoundedPaperRun, Deadline, order_key
from tests.runner.conftest import (
    executions,
    read_account,
    ready_case,
    record_market,
    run,
    runner_settings,
    stack_for,
)

# What the run is told is left of its time while it is still preparing. Never
# waited on: it only has to be larger than anything the preparation asks for,
# so no amount of load can make the preparation itself run out of budget.
HELD_OPEN = 3600.0
# What it is told is left once the call under test has been reached. This is
# the only interval actually waited out, and the only thing inside it is a
# coroutine that has already been created.
MEASURED_WAIT = 0.25
# A deliberate preparation delay, twice the measured wait. Its whole purpose is
# to be irrelevant.
SLOW_PREPARATION = 0.5


class HeldDeadline(Deadline):
    """The run's own deadline, held open until the decisive call is reached.

    The production `Deadline` with one property replaced, so `expired` and
    `within` stay exactly what the run uses. What it buys is the separation the
    old form of this test did not have: preparation — intake, database reads,
    whatever a specialist does — runs against a budget it cannot exhaust, and
    the timeout being proved starts only once the run has actually reached the
    call. Before, both shared fifty milliseconds, so a loaded machine could
    spend the budget on preparation and never reach the call at all.
    """

    def __init__(self) -> None:
        super().__init__(HELD_OPEN)
        self._reached = False
        self.finished = asyncio.Event()

    @property
    def remaining(self) -> float:
        if self.finished.is_set():
            # The call has been cut off and its cancellation has completed.
            # Nothing further in this pass has any time.
            return -1.0
        return MEASURED_WAIT if self._reached else HELD_OPEN

    def reached(self) -> None:
        """Called at the moment the run creates the decisive call."""
        self._reached = True


async def stored_request(sessions, trade_case_id):
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseRiskRequestRow).where(
                TradeCaseRiskRequestRow.trade_case_id == trade_case_id
            )
        )


async def test_an_interruption_before_the_fill_leaves_the_order_addressable(risk_db, now, trace):
    """The verdict is committed, the fill is not, and the next run finishes it.

    The risk request is its own transaction and survives; the fill had not begun.
    A second run addresses the same order by the same key and completes it once.
    """
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-interrupt")

    async def interrupted(*arguments, **keywords):
        raise KeyboardInterrupt("the process was stopped between the verdict and the fill")

    object.__setattr__(stack.fills, "execute_case_fill", interrupted)
    with pytest.raises(KeyboardInterrupt):
        await BoundedPaperRun(stack).execute()

    request = await stored_request(sessions, case.id)
    assert request is not None
    assert request.request_key == order_key(case.id)
    assert await executions(sessions) == []

    # The next explicit run, with the same identities and nothing carried over.
    summary = await run(sessions, settings, now, run_id=uuid4())

    assert summary.exit_code is ExitCode.COMPLETED
    assert summary.fills == 1
    again = await stored_request(sessions, case.id)
    assert again.request_id == request.request_id
    assert again.risk_decision_id == request.risk_decision_id
    assert len(await executions(sessions)) == 1


async def test_a_failure_inside_the_fill_leaves_no_partial_booking(risk_db, now, trace):
    """All of it or none of it: the fill, the ledger and the case record."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-boom")
    before = await read_account(sessions)
    original = type(stack.fills)._record

    def explode(self, *arguments, **keywords):
        original(self, *arguments, **keywords)
        raise RuntimeError("the transaction dies after everything is staged")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(stack.fills), "_record", explode)
        with pytest.raises(RuntimeError):
            await BoundedPaperRun(stack).execute()

    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)
    async with sessions() as session:
        for table in (ExecutionRow, OrderRow, TradeCaseExecutionRow):
            assert (await session.scalar(select(func.count()).select_from(table))) == 0
        held = await session.scalar(select(PositionRow))
    assert held is None or held.quantity == Decimal("0")

    # And a later explicit run still completes the order exactly once.
    summary = await run(sessions, settings, now, run_id=uuid4())
    assert summary.fills == 1
    assert len(await executions(sessions)) == 1


async def test_a_run_that_cannot_reach_the_database_reports_a_technical_failure(
    risk_db, now, trace
):
    """Distinguishable from a refusal, because they mean different things."""
    from sqlalchemy.exc import OperationalError

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)

    async def unreachable():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    object.__setattr__(stack.intake, "run_cycle", unreachable)

    summary = await BoundedPaperRun(stack).execute()

    assert summary.kind == "paper_run_summary"
    # Named precisely: the cycle commits one case at a time, so a database that
    # stopped answering inside it leaves an outcome this run cannot confirm.
    assert summary.errors == ("INTAKE_OUTCOME_UNKNOWN",)
    assert summary.intake_outcome_unknown is True
    assert summary.cases_opened == 0
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE
    assert await executions(sessions) == []


async def test_a_later_failure_does_not_erase_confirmed_work(risk_db, now, trace):
    """A fill that committed stays in the account even if the run then breaks.

    The summary is accumulated as the pass happens rather than assembled at the
    end, so a database that stops answering after a fill cannot make the run
    report zero fills for a fill that really happened.
    """
    from sqlalchemy.exc import OperationalError

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(paper_runner_max_cases=3)
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-keep")
    # A second ready case, so there is still work left when the database stops
    # answering — the point being what the summary keeps, not what it loses.
    await ready_case(stack.cases, now, uuid4(), key="runner-keep-two")
    # The database stops answering immediately after a fill has committed,
    # whichever case that fill belonged to.
    filled: list[object] = []
    fill = stack.fills.execute_case_fill
    read = stack.cases.get_trade_case

    async def watched_fill(trade_case_id, *, request_key):
        result = await fill(trade_case_id, request_key=request_key)
        filled.append(result)
        return result

    async def breaks_after_a_fill(trade_case_id):
        if filled:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return await read(trade_case_id)

    object.__setattr__(stack.fills, "execute_case_fill", watched_fill)
    object.__setattr__(stack.cases, "get_trade_case", breaks_after_a_fill)

    summary = await BoundedPaperRun(stack).execute()

    # The fault is reported, and so is the work that had already committed.
    assert summary.errors == ("DATABASE_UNAVAILABLE",)
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE
    assert summary.fills == 1
    assert summary.risk_requests == 1
    assert len(await executions(sessions)) == 1


async def test_an_unknown_outcome_is_reported_as_unknown(risk_db, now, trace):
    """A decisive call cut off mid-flight may have committed. The run says so.

    No success is invented and no failure either: the case is marked with an
    unknown outcome, and the next explicit run addresses the same order key and
    finds out what really happened.

    Preparation and the timeout under test are separated rather than sharing one
    short budget. The run is told it has an hour while it prepares, so nothing
    about how fast the machine gets to the risk request can decide the outcome;
    the substituted service marks the deadline as reached at the moment the run
    *creates* the call, which is where the run then reads its remaining time.
    The runner's own contract is what cancels the hanging call — no cancellation
    is applied from outside — and the two facts that matter are established by
    events: the call really was entered, and its cancellation really completed.
    """
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-unknown")
    deadline = HeldDeadline()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hangs() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            deadline.finished.set()
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    def request_risk_evaluation(*arguments, **keywords):
        """The run has reached the call. From here the only thing left is the wait."""
        deadline.reached()
        return hangs()

    object.__setattr__(stack.risk, "request_risk_evaluation", request_risk_evaluation)

    summary = await BoundedPaperRun(stack, deadline=deadline).execute()

    # The call was reached, and the run cut it off rather than abandoning it.
    assert entered.is_set(), "the risk request was never entered"
    assert cancelled.is_set(), "the hanging request was abandoned rather than cancelled"
    # And nothing of it is still running.
    assert {item for item in asyncio.all_tasks() if item is not asyncio.current_task()} == set()

    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.outcome_unknown is True
    assert progress.execution_id is None
    assert summary.fills == 0
    assert summary.stop.value == "TIME_BUDGET_REACHED"
    # And nothing was invented: the account is untouched.
    assert await executions(sessions) == []


async def test_slow_preparation_does_not_change_the_unknown_outcome(risk_db, now, trace):
    """The measured wait is the call's, and nothing before it counts against it.

    The same pass with half a second of deliberate preparation delay — twice the
    interval the cut-off itself is given. Under a single shared budget that
    would have ended the run before the decisive call was ever reached, and the
    proof would have evaporated while the test still passed or failed for
    reasons of its own. Here it changes nothing at all.
    """
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-slow-preparation")
    deadline = HeldDeadline()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    unhurried = stack.cases.get_trade_case

    async def slowly(trade_case_id):
        await asyncio.sleep(SLOW_PREPARATION)
        return await unhurried(trade_case_id)

    async def hangs() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            deadline.finished.set()
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    def request_risk_evaluation(*arguments, **keywords):
        deadline.reached()
        return hangs()

    object.__setattr__(stack.cases, "get_trade_case", slowly)
    object.__setattr__(stack.risk, "request_risk_evaluation", request_risk_evaluation)

    summary = await BoundedPaperRun(stack, deadline=deadline).execute()

    assert entered.is_set(), "slow preparation stopped the run from reaching the call"
    assert cancelled.is_set()
    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.outcome_unknown is True
    assert summary.stop.value == "TIME_BUDGET_REACHED"
