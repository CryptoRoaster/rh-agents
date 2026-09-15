"""Two callers, one account, and the guarantees that survive them.

PostgreSQL only. Row locks are what these prove, and SQLite has none — a test
that ran there would report a pass it had not earned.
"""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.data.tables import AccountRow, ExecutionRow, TradeCaseExecutionRow
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.workflow.models import TradeCaseStatus
from tests.casefill.conftest import approved_case, build_fill_service
from tests.riskrequest.conftest import read_account, set_account

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def counts(sessions):
    async with sessions() as session:
        fills = await session.scalar(select(func.count()).select_from(ExecutionRow))
        bound = await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))
    return fills, bound


async def test_two_callers_of_one_request_produce_one_fill(risk_db, now, trace):
    """One execution, one replay. Never two fills for one order."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    first = build_fill_service(sessions, now, feed=feed)
    second = build_fill_service(sessions, now, feed=feed)

    results = await asyncio.gather(
        first.execute_case_fill(case.id, request_key="fill-req"),
        second.execute_case_fill(case.id, request_key="fill-req"),
    )

    assert all(item.kind == "paper_fill_recorded" for item in results)
    assert sorted(item.replayed for item in results) == [False, True]
    assert len({item.execution_id for item in results}) == 1
    assert (await counts(sessions)) == (1, 1)


async def test_a_racing_second_key_never_adds_a_fill(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    first = build_fill_service(sessions, now, feed=feed)
    second = build_fill_service(sessions, now, feed=feed)

    results = await asyncio.gather(
        first.execute_case_fill(case.id, request_key="fill-req"),
        second.execute_case_fill(case.id, request_key="another-key"),
    )

    kinds = sorted(item.kind for item in results)
    assert kinds == ["execution_refused", "paper_fill_recorded"]
    refused = next(item for item in results if item.kind == "execution_refused")
    assert refused.reason is ExecutionRefusal.REQUEST_KEY_MISMATCH
    assert (await counts(sessions)) == (1, 1)


async def test_two_cases_sharing_cash_cannot_overdraw(risk_db, now, trace):
    """The account row serialises them, and the loser never books.

    Both refusals are correct answers and which one appears depends on
    ordering. The second caller priced the portfolio before the first fill
    existed, so under the lock its valuation no longer describes what is held —
    refusing there is the stricter of the two, and it happens first. When the
    valuation does still hold, the cash check bites instead. Neither outcome
    lets the account go negative.
    """
    _, sessions = risk_db
    first_case, _, feed = await approved_case(sessions, now, trace, key="cash-a")
    second_case, _, _ = await approved_case(sessions, now, uuid4(), key="cash-b")
    # Enough for one entry and its fees, not for two.
    await set_account(sessions, cash_usd=Decimal("600"))
    one = build_fill_service(sessions, now, feed=feed)
    two = build_fill_service(sessions, now, feed=feed)

    results = await asyncio.gather(
        one.execute_case_fill(first_case.id, request_key="cash-a-req"),
        two.execute_case_fill(second_case.id, request_key="cash-b-req"),
    )

    kinds = sorted(item.kind for item in results)
    assert kinds == ["execution_refused", "paper_fill_recorded"]
    refused = next(item for item in results if item.kind == "execution_refused")
    assert refused.reason in (
        ExecutionRefusal.RISK_RECHECK_REFUSED,
        ExecutionRefusal.PORTFOLIO_CHANGED_DURING_VALUATION,
    )
    if refused.reason is ExecutionRefusal.RISK_RECHECK_REFUSED:
        assert "INSUFFICIENT_CASH" in refused.reason_codes
    account = await read_account(sessions)
    assert account.cash_usd >= 0
    assert (await counts(sessions)) == (1, 1)


async def test_a_pause_and_a_fill_cannot_both_win(risk_db, now, trace):
    """They serialise on the account row, so one observes the other's commit."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    async def pause() -> None:
        async with sessions.begin() as session:
            await session.execute(select(AccountRow).where(AccountRow.id == 1).with_for_update())
            await session.execute(update(AccountRow).where(AccountRow.id == 1).values(paused=True))

    result, _ = await asyncio.gather(
        service.execute_case_fill(case.id, request_key="fill-req"), pause()
    )

    assert (await read_account(sessions)).paused is True
    fills, bound = await counts(sessions)
    if result.kind == "execution_refused":
        assert result.reason is ExecutionRefusal.SYSTEM_PAUSED
        assert (fills, bound) == (0, 0)
    else:
        assert (fills, bound) == (1, 1)


async def test_a_pause_committed_first_is_never_stepped_over(risk_db, now, trace):
    """The deterministic half of the race, with the ordering forced."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    await set_account(sessions, paused=True)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SYSTEM_PAUSED
    assert (await counts(sessions)) == (0, 0)


async def test_the_case_ends_executed_exactly_once_under_a_race(risk_db, now, trace):
    """A replay must not transition a case that is already terminal."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    one = build_fill_service(sessions, now, feed=feed)
    two = build_fill_service(sessions, now, feed=feed)

    await asyncio.gather(
        one.execute_case_fill(case.id, request_key="fill-req"),
        two.execute_case_fill(case.id, request_key="fill-req"),
    )

    after = await one.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.EXECUTED
    transitions = [
        item for item in await one.cases.timeline(case.id) if item.reason_code == "ENTRY_EXECUTED"
    ]
    assert len(transitions) == 2  # the transition and its own event, written once
