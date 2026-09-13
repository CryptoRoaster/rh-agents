"""Fixtures for PULSE: one setup to watch, one price to judge it against.

Every number is chosen so the arithmetic is obvious. The watched level is 1.20,
the market sits at 1.00, and a crossing is therefore always visible at a glance
rather than buried in a decimal.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.agents.pulse.models import PriceObservation, PulseTaskInput, WatchedTrigger
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.agents.vector.models import TriggerType
from src.core.models import AgentRole, Side
from src.markets.models import MarketIdentity
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    TradeSetupDetail,
    TradeSetupPayload,
    TradeSetupTrigger,
)
from tests.markets.conftest import market_sessions as market_sessions  # noqa: F401
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

# Matches the market layer's own fixture snapshot, so a snapshot built here
# is internally coherent and survives the recorder's revalidation rather than
# only working against stubs.
CHAIN = "ethereum"
NETWORK = "mainnet"
TOKEN = "0x" + "a1" * 20
QUOTE = "0x" + "b2" * 20
POOL = "0x" + "e5" * 20
PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{POOL}"

# The level being watched, and where the market currently is.
LEVEL = Decimal("1.20")
SPOT = Decimal("1.00")


def stable_id(label: str):
    return uuid5(NAMESPACE_URL, f"rh-agents:pulse-test:{label}")


def market_identity(chain: str = CHAIN, pair_id: str = PAIR_ID) -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network=NETWORK,
        pair_id=pair_id,
        base_asset_id=f"{chain}:{NETWORK}:{TOKEN}",
        quote_asset_id=f"{chain}:{NETWORK}:{QUOTE}",
        venue="uniswap-v3",
        is_fixture=False,
    )


def watched(now, **overrides) -> WatchedTrigger:
    """A breakout condition being watched: cross 1.20, an hour in, an hour left.

    ``valid_from`` sits in the past because that is what a monitor actually sees:
    a setup proposed some time ago, still inside its window, with observations
    arriving after it. A trigger whose window opened this instant would make
    every recent observation predate it.
    """
    defaults: dict[str, object] = dict(
        setup_evidence_id=stable_id("setup-evidence"),
        setup_id=stable_id("setup"),
        setup_fingerprint="a" * 64,
        type=TriggerType.PRICE_GTE,
        reference_price=LEVEL,
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    defaults.update(overrides)
    return WatchedTrigger(**defaults)  # type: ignore[arg-type]


def observed(now, *, price=SPOT, seconds_ago: int = 30, pair_id: str = PAIR_ID, **overrides):
    defaults: dict[str, object] = dict(
        observation_id=stable_id("price"),
        snapshot_id=stable_id("snapshot"),
        pair_id=pair_id,
        chain=CHAIN,
        network=NETWORK,
        venue="uniswap-v3",
        base_asset_id=f"{CHAIN}:{NETWORK}:{TOKEN}",
        quote_asset_id=f"{CHAIN}:{NETWORK}:{QUOTE}",
        provider="geckoterminal",
        is_fixture=False,
        price=price,
        observed_at=now - timedelta(seconds=seconds_ago),
    )
    defaults.update(overrides)
    return PriceObservation(**defaults)  # type: ignore[arg-type]


def task_input(
    now,
    *,
    trigger="default",
    observation="default",
    observations=None,
    truncated=False,
    latest="default",
    pair_id=PAIR_ID,
) -> PulseTaskInput:
    """One check's view.

    ``observation`` is a convenience for the common single-price case; a window
    of several is passed through ``observations``. The two are the same thing —
    a window of one — so tests that care about a single price stay readable.
    """
    if observations is None:
        single = observed(now) if observation == "default" else observation
        observations = () if single is None else (single,)
    newest = observations[-1] if observations else None
    return PulseTaskInput(
        trade_case_id=uuid4(),
        task_id=uuid4(),
        market_pair_id=pair_id,
        trigger=watched(now) if trigger == "default" else trigger,
        observations=tuple(observations),
        window_truncated=truncated,
        latest=newest if latest == "default" else latest,
        policy_version=PULSE_TRIGGER_V1.version,
        evaluated_at=now,
    )


def setup_detail(now, **overrides) -> TradeSetupDetail:
    """The VECTOR detail PULSE reads its condition out of."""
    # The window opened an hour ago and has an hour left, matching `watched`:
    # a monitor sees setups that are already running, with observations arriving
    # after they began rather than before.
    trigger_defaults: dict[str, object] = dict(
        type="PRICE_GTE",
        price_basis="USD_PER_BASE_UNIT",
        reference_price=LEVEL,
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    trigger_defaults.update(overrides.pop("trigger", {}))
    defaults: dict[str, object] = dict(
        setup_fingerprint="a" * 64,
        policy_version="vector-setup-v2",
        kind="BREAKOUT_LONG",
        price_basis="USD_PER_BASE_UNIT",
        entry_low=LEVEL,
        entry_high=LEVEL,
        reference_price=SPOT,
        expires_at=now + timedelta(hours=1),
        trigger=TradeSetupTrigger(**trigger_defaults),  # type: ignore[arg-type]
        reason_codes=("PRICE_AVAILABLE",),
        summary="Waiting for a move through 1.20.",
        input_digest="b" * 64,
    )
    defaults.update(overrides)
    return TradeSetupDetail(**defaults)  # type: ignore[arg-type]


def setup_payload(now, **overrides) -> TradeSetupPayload:
    detail = overrides.pop("detail", "default")
    return TradeSetupPayload(
        setup_id=overrides.pop("setup_id", stable_id("setup")),
        side=Side.BUY,
        entry_price=LEVEL,
        invalidation_price=Decimal("0.92"),
        target_prices=(Decimal("1.30"),),
        setup=setup_detail(now, **overrides) if detail == "default" else detail,
    )


def setup_envelope(now, *, payload=None, status=EvidenceStatus.AVAILABLE, valid_for=None, **kw):
    evidence_id = kw.pop("evidence_id", stable_id("setup-evidence"))
    trade_case_id = kw.pop("trade_case_id", uuid4())
    return EvidenceEnvelope(
        evidence_id=evidence_id,
        trade_case_id=trade_case_id,
        producer_role=AgentRole.VECTOR,
        evidence_type=EvidenceType.TRADE_SETUP,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=now,
        created_at=now,
        recorded_at=now,
        valid_until=now + (valid_for or timedelta(hours=2)),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_UNKNOWN",),
        payload=payload if payload is not None else setup_payload(now, **kw),
        correlation_id=uuid4(),
        idempotency_key=str(evidence_id),
        submission_fingerprint="c" * 64,
    )


class StubCases:
    def __init__(self, trade_case, evidence=()) -> None:
        self._trade_case = trade_case
        self._evidence = tuple(evidence)

    async def get_trade_case(self, trade_case_id):
        return self._trade_case

    async def evidence(self, trade_case_id):
        return self._evidence


class StubTradeCase:
    def __init__(self, market: MarketIdentity) -> None:
        self.market = market
        self.id = uuid4()


class StubMarkets:
    """The two reads the context needs, and no write of any kind."""

    def __init__(self, snapshot=None, window=None) -> None:
        self._snapshot = snapshot
        # A window of one, unless a test says otherwise: the ordinary case is a
        # single recorded price, and the interesting cases are several.
        self._window = window if window is not None else ([] if snapshot is None else [snapshot])

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        return self._snapshot

    async def observations(
        self, identity: str, *, since, until, limit: int, include_fixtures: bool = False
    ):
        inside = [item for item in self._window if since <= item.price.observed_at <= until]
        inside.sort(key=lambda item: (item.price.observed_at, item.id))
        return tuple(inside[: limit + 1])


@pytest.fixture
def market():
    return market_identity()


@pytest.fixture
async def pulse_db():
    """A database holding both the workflow and the recorded market stream.

    PULSE is the first worker that reads the market tables and the workflow
    tables in one check, so it is the first that needs both present. The shared
    worker fixture applies only the workflow migrations.
    """
    import importlib.util
    import os
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.data.tables import Base

    url = os.environ.get("TEST_DATABASE_URL")
    schema = "pulse_test_" + uuid4().hex
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
                for name in (
                    "0002_market_observations",
                    "0003_pool_locator",
                    "0005_trade_case_workflow",
                    "0006_worker_runtime",
                ):
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
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
