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
from src.runner.service import BoundedPaperRun, order_key
from tests.runner.conftest import (
    executions,
    read_account,
    ready_case,
    record_market,
    run,
    runner_settings,
    stack_for,
)


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
    assert summary.errors == ("DATABASE_UNAVAILABLE",)
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE
    assert await executions(sessions) == []
