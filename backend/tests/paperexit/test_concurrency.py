"""Two callers, one holding, and the guarantees that survive them.

PostgreSQL only. Row locks are what these prove, and SQLite has none — a test
that ran there would report a pass it had not earned.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.models import Side, Trade
from src.data.tables import ExecutionRow, TradeCaseExitRow, TradeRow
from src.orchestration.paperexit.models import ExitRefusal
from tests.paperexit.conftest import build_exit_service, entered, exits, market_feed, position_of
from tests.riskrequest.conftest import read_account, set_account

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def sales(sessions):
    """Every booked SELL, counted out of the ledger itself."""
    async with sessions() as session:
        rows = (await session.scalars(select(TradeRow))).all()
    return [
        item
        for item in (Trade.model_validate(row.payload) for row in rows)
        if item.side is Side.SELL
    ]


async def test_two_identical_orders_produce_one_sale_and_one_replay(risk_db, now, trace):
    """One order, one sale. The second caller gets history, not a second fill."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    first = build_exit_service(sessions, now, feed=feed)
    second = build_exit_service(sessions, now, feed=feed)

    results = await asyncio.gather(
        first.execute_position_exit(position.id, request_key="race"),
        second.execute_position_exit(position.id, request_key="race"),
    )

    assert {item.kind for item in results} == {"paper_exit_recorded"}
    assert {item.execution_id for item in results} == {results[0].execution_id}
    assert sorted(item.replayed for item in results) == [False, True]
    assert len(await sales(sessions)) == 1
    assert len(await exits(sessions)) == 1
    assert (await position_of(sessions)).quantity == Decimal("0")


async def test_two_different_orders_sell_the_holding_at_most_once(risk_db, now, trace):
    """Different keys are different orders, and there is only one holding."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    before = await read_account(sessions)

    results = await asyncio.gather(
        build_exit_service(sessions, now, feed=feed).execute_position_exit(
            position.id, request_key="one"
        ),
        build_exit_service(sessions, now, feed=feed).execute_position_exit(
            position.id, request_key="two"
        ),
    )

    sold = [item for item in results if item.kind == "paper_exit_recorded"]
    refused = [item for item in results if item.kind == "exit_refused"]
    assert len(sold) == 1
    assert len(refused) == 1
    assert refused[0].reason is ExitRefusal.POSITION_ALREADY_CLOSED
    assert len(await sales(sessions)) == 1
    assert len(await exits(sessions)) == 1
    after = await read_account(sessions)
    assert after.cash_usd > before.cash_usd
    assert (await position_of(sessions)).quantity == Decimal("0")


async def test_a_pause_racing_an_exit_leaves_neither_stepping_over_the_other(risk_db, now, trace):
    """Both go through the account row, so the order is decided, not chanced."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)

    async def pause():
        await set_account(sessions, paused=True)

    result, _ = await asyncio.gather(
        build_exit_service(sessions, now, feed=feed).execute_position_exit(
            position.id, request_key="pause-race"
        ),
        pause(),
    )

    if result.kind == "paper_exit_recorded":
        # The sale won the lock: it is booked exactly once and the pause stands.
        assert len(await sales(sessions)) == 1
        assert (await position_of(sessions)).quantity == Decimal("0")
    else:
        assert result.reason is ExitRefusal.SYSTEM_PAUSED
        assert await sales(sessions) == []
        assert (await position_of(sessions)).quantity == position.quantity
    assert (await read_account(sessions)).paused is True


async def test_a_failure_before_commit_leaves_no_partial_bookings(risk_db, now, trace, monkeypatch):
    """All of it or none of it: the sale, the ledger and the exit record."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    before = await read_account(sessions)
    service = build_exit_service(sessions, now, feed=feed)
    original = type(service)._record

    def explode(self, *arguments, **keywords):
        original(self, *arguments, **keywords)
        raise RuntimeError("the transaction dies after everything is staged")

    monkeypatch.setattr(type(service), "_record", explode)
    with pytest.raises(RuntimeError):
        await service.execute_position_exit(position.id, request_key="boom")
    monkeypatch.undo()

    assert await exits(sessions) == []
    assert await sales(sessions) == []
    assert (await position_of(sessions)).quantity == position.quantity
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1

    # And a later order still succeeds: nothing was consumed by the failure.
    again = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="after-boom"
    )
    assert again.kind == "paper_exit_recorded"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeCaseExitRow)) == 1
