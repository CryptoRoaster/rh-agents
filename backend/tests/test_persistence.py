"""Set TEST_DATABASE_URL to exercise these same contracts against PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from src.orchestration.paper import PaperTradingService


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
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    first = await service.process(intent, market, now=now)
    restarted = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    second = await restarted.process(intent, market, now=now)
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
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    await service.process(intent, market, now=now)
    changed = intent.model_copy(update={"quantity": Decimal("3")})
    with pytest.raises(ValueError, match="Idempotency conflict"):
        await service.process(changed, market, now=now)


async def test_rejection_is_durable_without_execution(sessions, intent, market, now):
    service = PaperTradingService(
        sessions, RiskLimits(max_exposure_usd=Decimal("100")), TradingMode.PAPER
    )
    risk = await service.process(intent, market, now=now)
    assert isinstance(risk, RiskDecision)
    assert risk.outcome == RiskOutcome.REJECT
    assert await service.process(intent, market, now=now) == risk
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RiskRow)) == 1
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 0


async def test_fill_and_accounting_commit_atomically(
    sessions, intent, market, now, monkeypatch, caplog
):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)

    async def fail(*args):
        raise ValueError("Simulated execution failure")

    monkeypatch.setattr(service.executor, "execute", fail)
    with pytest.raises(ValueError, match="execution failure"):
        await service.process(intent, market, now=now)
    assert caplog.records[-1].correlation_id == str(intent.correlation_id)
    assert caplog.records[-1].intent_id == str(intent.id)
    async with sessions() as session:
        for table in (IntentRow, RiskRow, OrderRow, ExecutionRow, TradeRow, PositionRow, PnLRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        assert (await session.get(AccountRow, 1)).cash_usd == 10000


async def test_pause_is_latched_across_service_instances(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(kill_switch=True), TradingMode.PAPER)
    assert (await service.process(intent, market, now=now)).outcome == RiskOutcome.PAUSE_SYSTEM
    restarted = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    intent = intent.model_copy(update={"id": uuid4()})
    assert (await restarted.process(intent, market, now=now)).outcome == RiskOutcome.PAUSE_SYSTEM


async def test_persisted_buy_sell_and_pnl(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    await service.process(intent, market, now=now)
    sell = intent.model_copy(update={"id": uuid4(), "side": Side.SELL})
    fill = await service.process(sell, market, now=now)
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
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    await service.process(intent, market, now=now)
    new_asset = "paper:SECOND"
    data = market.model_dump()
    data.update(id=uuid4(), asset_id=new_asset)
    for key in ("token", "liquidity", "holders"):
        data[key].update(id=uuid4(), asset_id=new_asset)
    new_market = type(market).model_validate(data)
    new_intent = intent.model_copy(update={"id": uuid4(), "asset_id": new_asset})
    risk = await service.process(new_intent, new_market, now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert "ACCOUNTING_UNKNOWN" in risk.reason_codes


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_concurrent_duplicates_have_one_fill(sessions, intent, market, now):
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER)
    results = await asyncio.gather(*(service.process(intent, market, now=now) for _ in range(4)))
    assert all(result == results[0] for result in results)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeRow)) == 1


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_concurrent_intents_cannot_overspend_exposure(sessions, intent, market, now):
    service = PaperTradingService(
        sessions, RiskLimits(max_exposure_usd=Decimal("300")), TradingMode.PAPER
    )
    second = intent.model_copy(update={"id": uuid4()})
    results = await asyncio.gather(
        service.process(intent, market, now=now), service.process(second, market, now=now)
    )
    assert sum(isinstance(result, ExecutionResult) for result in results) == 1
    assert (
        sum(
            isinstance(result, RiskDecision) and result.outcome == RiskOutcome.REJECT
            for result in results
        )
        == 1
    )
