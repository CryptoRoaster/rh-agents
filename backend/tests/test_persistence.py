"""Set TEST_DATABASE_URL to exercise these same contracts against PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.clock import FixedClock, SystemClock
from src.core.models import (
    ExecutionResult,
    RiskDecision,
    RiskLimits,
    RiskOutcome,
    Side,
    TradingMode,
)
from src.data.tables import (
    AccountRow,
    Base,
    ExecutionRow,
    IntentRow,
    OrderRow,
    PnLRow,
    PositionRow,
    RiskRow,
    TradeRow,
)
from src.orchestration.paper import PaperExecutionExpired, PaperTradingService


@pytest.fixture
async def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    schema = f"test_{uuid4().hex}"
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            session.add(
                AccountRow(
                    id=1,
                    cash_usd=Decimal("10000"),
                    initial_cash_usd=Decimal("10000"),
                    fees_paid_usd=Decimal("0"),
                    loss_day=datetime(2026, 9, 9, tzinfo=UTC).date(),
                    realized_loss_today_usd=Decimal("0"),
                    paused=False,
                )
            )
        yield factory
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def test_durable_idempotency_survives_service_restart(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    first = await service.process(intent, market)
    restarted = PaperTradingService(
        sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now)
    )
    second = await restarted.process(intent, market)
    assert isinstance(first, ExecutionResult)
    assert second == first
    async with sessions() as session:
        for table in (ExecutionRow, IntentRow, OrderRow, RiskRow, PositionRow, TradeRow, PnLRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
        position = await session.scalar(select(PositionRow))
        assert position.quantity == 2
        account = await session.get(AccountRow, 1)
        assert account.cash_usd.quantize(Decimal("0.000001")) == Decimal("9798.799000")


async def test_conflicting_idempotency_key_is_rejected(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    await service.process(intent, market)
    changed = intent.model_copy(update={"quantity": Decimal("3")})
    with pytest.raises(ValueError, match="Idempotency conflict"):
        await service.process(changed, market)


async def test_rejection_is_durable_without_execution(sessions, intent, market, now):
    service = PaperTradingService(
        sessions,
        RiskLimits(max_exposure_usd=Decimal("100")),
        TradingMode.PAPER,
        clock=FixedClock(now),
    )
    risk = await service.process(intent, market)
    assert isinstance(risk, RiskDecision)
    assert risk.outcome == RiskOutcome.REJECT
    assert await service.process(intent, market) == risk
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RiskRow)) == 1
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 0


async def test_fill_and_accounting_commit_atomically(
    sessions, intent, market, now, monkeypatch, caplog
):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))

    async def fail(*args):
        raise ValueError("Simulated execution failure")

    monkeypatch.setattr(service.executor, "execute", fail)
    with pytest.raises(ValueError, match="execution failure"):
        await service.process(intent, market)
    assert caplog.records[-1].correlation_id == str(intent.correlation_id)
    assert caplog.records[-1].intent_id == str(intent.id)
    async with sessions() as session:
        for table in (IntentRow, RiskRow, OrderRow, ExecutionRow, TradeRow, PositionRow, PnLRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        assert (await session.get(AccountRow, 1)).cash_usd == 10000


async def test_pause_is_latched_across_service_instances(sessions, intent, market, now):
    service = PaperTradingService(
        sessions, RiskLimits(kill_switch=True), TradingMode.PAPER, clock=FixedClock(now)
    )
    assert (await service.process(intent, market)).outcome == RiskOutcome.PAUSE_SYSTEM
    restarted = PaperTradingService(
        sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now)
    )
    intent = intent.model_copy(update={"id": uuid4()})
    assert (await restarted.process(intent, market)).outcome == RiskOutcome.PAUSE_SYSTEM


async def test_persisted_buy_sell_and_pnl(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    await service.process(intent, market)
    sell = intent.model_copy(update={"id": uuid4(), "side": Side.SELL})
    fill = await service.process(sell, market)
    assert isinstance(fill, ExecutionResult)
    async with sessions() as session:
        position = await session.scalar(select(PositionRow))
        assert position.quantity == 0
        assert position.cost_basis_usd == 0
        account = await session.get(AccountRow, 1)
        assert account.cash_usd.quantize(Decimal("0.000001")) == Decimal("9997.600000")
        pnl = (await session.scalars(select(PnLRow))).all()
        assert len(pnl) == 2
        final = next(row.payload for row in pnl if row.payload["market_value_usd"] == "0E-18")
        assert Decimal(final["total_pnl_usd"]).quantize(Decimal("0.000001")) == Decimal("-2.400000")


async def test_missing_portfolio_mark_fails_closed(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    await service.process(intent, market)
    new_asset = "paper:SECOND"
    data = market.model_dump()
    data.update(id=uuid4(), asset_id=new_asset)
    for key in ("token", "liquidity", "holders"):
        data[key].update(id=uuid4(), asset_id=new_asset)
    new_market = type(market).model_validate(data)
    new_intent = intent.model_copy(update={"id": uuid4(), "asset_id": new_asset})
    risk = await service.process(new_intent, new_market)
    assert risk.outcome == RiskOutcome.REJECT
    assert "ACCOUNTING_UNKNOWN" in risk.reason_codes


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_concurrent_duplicates_have_one_fill(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    results = await asyncio.gather(*(service.process(intent, market) for _ in range(4)))
    assert all(result == results[0] for result in results)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeRow)) == 1


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_concurrent_intents_cannot_overspend_exposure(sessions, intent, market, now):
    service = PaperTradingService(
        sessions,
        RiskLimits(max_exposure_usd=Decimal("300")),
        TradingMode.PAPER,
        clock=FixedClock(now),
    )
    second = intent.model_copy(update={"id": uuid4()})
    results = await asyncio.gather(service.process(intent, market), service.process(second, market))
    assert sum(isinstance(result, ExecutionResult) for result in results) == 1
    assert (
        sum(
            isinstance(result, RiskDecision) and result.outcome == RiskOutcome.REJECT
            for result in results
        )
        == 1
    )


async def test_service_uses_injected_clock_for_freshness(sessions, intent, market, now):
    later = now + timedelta(seconds=31)
    service = PaperTradingService(
        sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(later)
    )
    risk = await service.process(intent, market)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.evaluated_at == later
    assert "STALE_OR_FUTURE_MARKET" in risk.reason_codes
    assert risk.max_additional_notional_usd == 0


async def test_service_defaults_to_system_clock(sessions, intent, market, now, monkeypatch):
    monkeypatch.setattr(SystemClock, "now", lambda self: now + timedelta(seconds=31))
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    risk = await service.process(intent, market)
    assert "STALE_OR_FUTURE_MARKET" in risk.reason_codes


async def test_trading_caller_cannot_supply_now(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))
    with pytest.raises(TypeError, match="now"):
        await service.process(intent, market, now=now)


async def test_an_approval_that_lapses_before_the_fill_stops_the_execution(
    sessions, intent, market, now
):
    """Time passes while the decision is persisted, and that has to count.

    Between `evaluate()` and the executor the service writes the market, the
    intent and the decision — three database round trips. The clock is read
    again at the execution boundary rather than carried from before them,
    because an older timestamp would hide exactly that wait instead of
    respecting it. An approval that has lapsed by then is a typed stop and a
    full rollback, not a fill and not a manufactured rejection.
    """

    class AdvancingClock:
        def __init__(self) -> None:
            self.reads = 0

        def now(self):
            self.reads += 1
            return now + timedelta(seconds=6 * (self.reads - 1))

    clock = AdvancingClock()
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=clock)
    with pytest.raises(PaperExecutionExpired, match="APPROVAL_EXPIRED"):
        await service.process(intent, market)
    async with sessions() as session:
        for table in (ExecutionRow, IntentRow, RiskRow, OrderRow, PositionRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        assert (await session.get(AccountRow, 1)).cash_usd == 10000


async def test_the_order_is_timestamped_at_the_boundary_it_was_placed_at(
    sessions, intent, market, now
):
    """Truthful, not convenient: the instant the order was actually placed."""

    class AdvancingClock:
        def __init__(self) -> None:
            self.reads = 0

        def now(self):
            self.reads += 1
            return now + timedelta(seconds=self.reads - 1)

    clock = AdvancingClock()
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=clock)
    fill = await service.process(intent, market)
    assert clock.reads == 2, "the evaluation and the execution boundary are separate"
    assert fill.timing.execution_requested_at == now + timedelta(seconds=1)
    async with sessions() as session:
        order = await session.scalar(select(OrderRow))
        risk = await session.scalar(select(RiskRow))
    assert order.payload["execution_requested_at"] > risk.payload["evaluated_at"]


async def test_legacy_rejection_replays_without_rewriting_history(sessions, intent, market, now):
    service = PaperTradingService(
        sessions,
        RiskLimits(max_exposure_usd=Decimal("100")),
        TradingMode.PAPER,
        clock=FixedClock(now),
    )
    first = await service.process(intent, market)
    async with sessions.begin() as session:
        row = await session.scalar(select(RiskRow))
        legacy = dict(row.payload)
        legacy["max_allowed_position_size_usd"] = legacy.pop("position_size_limit_usd")
        del legacy["max_additional_notional_usd"]
        row.payload = legacy
    restarted = PaperTradingService(
        sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now + timedelta(days=1))
    )
    replay = await restarted.process(intent, market)
    assert replay.id == first.id
    assert replay.outcome == first.outcome == RiskOutcome.REJECT
    assert replay.position_size_limit_usd == first.position_size_limit_usd
    assert replay.max_additional_notional_usd == 0
    async with sessions() as session:
        assert (await session.scalar(select(RiskRow))).payload == legacy


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_trusted_time_is_read_after_waiting_for_portfolio_lock(sessions, intent, market, now):
    attempted = asyncio.Event()
    clock_allowed = asyncio.Event()
    engine = sessions.kw["bind"]

    class AfterLockClock:
        def now(self):
            assert clock_allowed.is_set(), "Clock was sampled before waiting for the lock"
            return now + timedelta(seconds=31)

    def lock_attempt(connection, cursor, statement, parameters, execution_context, executemany):
        if "FOR UPDATE" in statement:
            attempted.set()

    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=AfterLockClock())
    task = None
    event.listen(engine.sync_engine, "before_cursor_execute", lock_attempt)
    try:
        async with sessions.begin() as session:
            await session.scalar(select(AccountRow).where(AccountRow.id == 1).with_for_update())
            attempted.clear()
            task = asyncio.create_task(service.process(intent, market))
            await asyncio.wait_for(attempted.wait(), timeout=5)
            assert not task.done()
            clock_allowed.set()
        risk = await asyncio.wait_for(task, timeout=5)
        assert risk.outcome == RiskOutcome.REJECT
        assert "STALE_OR_FUTURE_MARKET" in risk.reason_codes
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", lock_attempt)
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_an_unrepresentable_holding_value_is_rejected_not_crashed(
    sessions, intent, market, now
):
    """The standalone PAPER path has no refusal layer of its own.

    A held position whose recorded mark makes exposure larger than the ledger can
    hold leaves accounting UNKNOWN, and SENTINEL rejects on that. No exception,
    no fill.
    """
    from src.core.models import Position
    from src.data.repository import save_position
    from src.orchestration.valuation.models import PositionMark

    other = "paper:OTHER"
    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=uuid4(),
                asset_id=other,
                market_pair_id="paper:OTHER-USD",
                market_chain="paper",
                market_network="paper",
                market_provider="paper",
                quantity=Decimal("1"),
                cost_basis_usd=Decimal("10"),
                created_at=now,
                updated_at=now,
            ),
        )
    mark = PositionMark(
        asset_id=other,
        pair_id="paper:OTHER-USD",
        provider="paper",
        snapshot_id=uuid4(),
        observation_id=uuid4(),
        price_usd=Decimal("1E+25"),
        observed_at=now - timedelta(seconds=1),
    )
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))

    risk = await service.process(intent, market, marks={other: mark})

    assert isinstance(risk, RiskDecision)
    assert risk.outcome == RiskOutcome.REJECT
    assert "ACCOUNTING_UNKNOWN" in risk.reason_codes
    assert "PORTFOLIO_DATA_UNKNOWN" in risk.reason_codes
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 0
