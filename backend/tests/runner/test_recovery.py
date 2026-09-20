"""Interruption, and what the next explicit run finds.

A run is not one transaction. Steps that committed stay committed; the step that
was interrupted follows its own contract. What must never happen is a second
order, a second fill, or a booking that only half happened.
"""

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

# The whole run, as this test module's own input. Generous on purpose:
# everything before a deliberately hanging call has to fit inside it on a
# machine running the rest of the suite beside it, and the property under test
# is what happens when the bound is reached — not how narrow the bound is.
RUN_SECONDS = 2.0


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

    The two facts this rests on are established by events rather than by
    assuming how quickly a scheduler gets anywhere: the request really was
    entered, and the run really cancelled it and waited for that cancellation.
    An earlier version asserted on a case the run had to reach inside fifty
    milliseconds, which under a loaded machine it sometimes did not — and the
    test then failed while looking up a case that was legitimately absent,
    saying nothing about the contract it was meant to protect. The deadline is
    the test's own input and is now ample; the timeout being tested is the run's
    own and is unchanged.
    """
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-unknown")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_answers(*arguments, **keywords):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    object.__setattr__(stack.risk, "request_risk_evaluation", never_answers)

    summary = await BoundedPaperRun(stack, deadline=Deadline(RUN_SECONDS)).execute()

    # The call was reached, and the run cut it off rather than abandoning it.
    assert entered.is_set(), "the risk request was never reached"
    assert cancelled.is_set(), "the hanging request was abandoned rather than cancelled"

    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.outcome_unknown is True
    assert progress.execution_id is None
    assert summary.fills == 0
    assert summary.stop.value == "TIME_BUDGET_REACHED"
    # And nothing was invented: the account is untouched.
    assert await executions(sessions) == []
