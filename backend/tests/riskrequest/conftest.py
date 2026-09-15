"""Fixtures for the canonical risk request.

One market throughout, the same one ATLAS and the completeness check use, so a
case can be carried from evidence to a bound SENTINEL verdict without two
notions of which token this is.

Sources are deliberately built *fresh* here — a few seconds old rather than the
thirty the completeness check tolerates — because SENTINEL applies its own
tighter bound and this suite is about what happens when the data actually
qualifies.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import AgentRole, RiskLimits, TradingMode
from src.data.tables import AccountRow
from src.orchestration.riskrequest.service import RiskRequestService
from src.orchestration.workflow.models import EvidenceType, SentimentPayload
from src.orchestration.workflow.service import TradeCaseService
from tests.riskdata.conftest import (  # noqa: F401
    BASE_ASSET,
    IDENTITY,
    PAIR_ID,
    PRICE,
    RecordedMarkets,
    RunningSystem,
    anchor_payload,
    configured_costs,
    holder_block,
    onchain_payload,
    open_case,
    record,
    record_onchain,
    recorded_snapshot,
)
from tests.worker.conftest import setup_payload, trigger_payload

FRESH = timedelta(seconds=5)


@pytest.fixture
async def risk_db():
    """A schema carrying accounting *and* workflow, as a real deployment does.

    The worker fixture applies only the workflow migrations, which is the right
    slice for everything that never touches the paper account. A risk request
    does: it locks the account, values the portfolio and may set the durable
    pause, so it needs the tables those live in. `paper_accounts` has been in
    the regular Alembic chain since 0001, and 0001 seeds the single row.
    """
    import importlib.util
    import os
    from pathlib import Path
    from uuid import uuid4 as _uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.data.tables import Base

    url = os.environ.get("TEST_DATABASE_URL")
    schema = "riskrequest_test_" + _uuid4().hex
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        if url:
            versions = Path(__file__).parents[2] / "migrations/versions"
            modules = []
            for name in (
                "0001_foundation",
                "0005_trade_case_workflow",
                "0006_worker_runtime",
                "0007_trade_case_risk_requests",
                "0008_trade_case_executions",
                "0009_position_market_identity",
            ):
                spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                modules.append(module)

            def migrate(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    for item in modules:
                        item.upgrade()

            async with engine.begin() as connection:
                await connection.run_sync(migrate)
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await seed_account(sessions)
        yield engine, sessions
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def seed_account(sessions):
    """Ensure the single authoritative paper account exists.

    Migration 0001 seeds it; the metadata-only SQLite path does not, so the row
    is created there rather than letting the difference decide the test.
    """
    from datetime import date

    from src.data.tables import AccountRow

    async with sessions.begin() as session:
        if await session.scalar(select(AccountRow.id).where(AccountRow.id == 1)) is None:
            session.add(
                AccountRow(
                    id=1,
                    cash_usd=Decimal("10000"),
                    initial_cash_usd=Decimal("10000"),
                    fees_paid_usd=Decimal("0"),
                    loss_day=date(2026, 9, 9),
                    realized_loss_today_usd=Decimal("0"),
                    paused=False,
                )
            )


def fresh_snapshot(now, **overrides):
    arguments = {"age": FRESH, "metadata_age": FRESH}
    return recorded_snapshot(now, **{**arguments, **overrides})


def fresh_onchain(now, **overrides):
    """ATLAS evidence whose holder metrics are recent enough for SENTINEL."""
    arguments = {"holders": holder_block(now, age=FRESH)}
    return onchain_payload(now, **{**arguments, **overrides})


async def ready_case(
    cases,
    now,
    trace,
    *,
    onchain=None,
    key="riskrequest-case",
    anchor=True,
    lifetime=timedelta(hours=1),
    identity=None,
):
    """A case the evaluator publishes as READY_FOR_RISK.

    Every required envelope, in order: ORBIT's discovery arrives at open, then
    ATLAS, SIGNAL and VECTOR before the trigger, then PULSE and ANCHOR.
    """
    trade_case = await open_case(cases, now, trace, key, lifetime=lifetime, identity=identity)
    await record_onchain(cases, trade_case, now, onchain or fresh_onchain(now))
    await record(
        cases,
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        SentimentPayload(assessment="NEUTRAL"),
        key=f"rr-signal-{trade_case.id}",
    )
    setup = await record(
        cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(),
        key=f"rr-setup-{trade_case.id}",
    )
    trigger = await record(
        cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"rr-trigger-{trade_case.id}",
    )
    if anchor:
        await record(
            cases,
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            anchor_payload(setup.evidence_id, trigger.evidence_id),
            key=f"rr-anchor-{trade_case.id}",
        )
    return await cases.get_trade_case(trade_case.id)


def build_service(
    sessions,
    now,
    *,
    feed=None,
    notional="500",
    costs=None,
    limits=None,
    pause="running",
    **overrides,
):
    """The real workflow service and the real SENTINEL behind one narrow call."""
    clock = FixedClock(now)
    arguments = {
        "sessions": sessions,
        "cases": TradeCaseService(sessions, clock=clock),
        "markets": feed if feed is not None else RecordedMarkets(fresh_snapshot(now)),
        "costs": configured_costs() if costs is None else costs,
        "requested_notional_usd": None if notional is None else Decimal(notional),
        "limits": limits if limits is not None else RiskLimits(),
        "trading_mode": TradingMode.PAPER,
        "clock": clock,
        "pause": RunningSystem() if pause == "running" else pause,
        "include_fixtures": False,
    }
    return RiskRequestService(**{**arguments, **overrides})


async def set_account(sessions, **values):
    """Move the paper account, as a fill or an operator would have."""
    async with sessions.begin() as session:
        account = await session.scalar(select(AccountRow).where(AccountRow.id == 1))
        for name, value in values.items():
            setattr(account, name, value)


async def read_account(sessions):
    async with sessions() as session:
        return await session.scalar(select(AccountRow).where(AccountRow.id == 1))


@pytest.fixture
def request_key():
    return f"risk-request-{uuid4()}"
