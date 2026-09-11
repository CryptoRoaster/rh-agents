import importlib.util
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.data.tables import Base
from src.markets.fake import fixture_snapshot
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import (
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    LiquidityExecutionPayload,
    OnchainPayload,
    SentimentPayload,
    TradeSetupPayload,
    TriggerPayload,
)
from src.orchestration.workflow.service import TradeCaseService

MIGRATIONS = ("0005_trade_case_workflow", "0006_worker_runtime")


@pytest.fixture
async def worker_db():
    url = os.environ.get("TEST_DATABASE_URL")
    schema = "worker_test_" + uuid4().hex
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            if url:
                versions = Path(__file__).parents[2] / "migrations/versions"
                modules = []
                for name in MIGRATIONS:
                    spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    modules.append(module)

                def migrate(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        for item in modules:
                            item.upgrade()

                await connection.run_sync(migrate)
            else:
                await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield engine, sessions
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.fixture
def runtime(worker_db, now):
    """Services sharing one FixedClock, as a worker process would see them."""
    _, sessions = worker_db
    return build_runtime(sessions, now)


def build_runtime(sessions, instant):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    return WorkerRuntimeService(sessions, cases, clock=clock)


def advanced(sessions, instant, delta):
    return build_runtime(sessions, instant + delta)


async def open_case(cases, now, trace, key="worker-case"):
    return await cases.open_trade_case(
        fixture_snapshot(now, trace).pair.market_identity,
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
        strategy_policy_id="paper-policy-v1",
    )


def submission(
    trade_case,
    now,
    role,
    evidence_type,
    payload,
    *,
    key="worker-evidence",
    status=EvidenceStatus.AVAILABLE,
    supersedes_id=None,
    valid_until=None,
):
    return EvidenceSubmission(
        idempotency_key=key,
        producer_role=role,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test-worker", reference_id=uuid4()),
        observed_at=now,
        valid_until=valid_until or now + timedelta(minutes=30),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_NOT_VERIFIED",),
        payload=payload,
        correlation_id=trade_case.correlation_id,
        supersedes_id=supersedes_id,
    )


def atlas_payload():
    return OnchainPayload(
        holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="PASS"
    )


def signal_payload():
    return SentimentPayload(assessment="NEUTRAL")


def setup_payload():
    return TradeSetupPayload(
        setup_id=uuid4(),
        side="BUY",
        entry_price=Decimal("1"),
        invalidation_price=Decimal("0.8"),
        target_prices=(Decimal("1.2"),),
    )


def trigger_payload(setup_evidence_id):
    return TriggerPayload(
        setup_evidence_id=setup_evidence_id,
        observed_price=Decimal("1"),
        trigger_code="ENTRY_LEVEL_REACHED",
    )


def anchor_payload(setup_evidence_id, trigger_evidence_id):
    return LiquidityExecutionPayload(
        setup_evidence_id=setup_evidence_id,
        trigger_evidence_id=trigger_evidence_id,
        quoted_price=Decimal("1"),
        liquidity_usd=Decimal("500000"),
        estimated_slippage_bps=Decimal("25"),
        price_impact_bps=Decimal("20"),
        maximum_safe_size_usd=Decimal("2500"),
        routing_provenance="quoted-route-v1",
    )


ROLE_PAYLOADS = {
    AgentRole.ATLAS: (EvidenceType.ONCHAIN, atlas_payload),
    AgentRole.SIGNAL: (EvidenceType.SENTIMENT, signal_payload),
    AgentRole.VECTOR: (EvidenceType.TRADE_SETUP, setup_payload),
}
