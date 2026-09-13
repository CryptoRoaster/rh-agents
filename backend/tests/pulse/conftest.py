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
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

CHAIN = "robinhood"
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


def task_input(now, *, trigger="default", observation="default", pair_id=PAIR_ID) -> PulseTaskInput:
    return PulseTaskInput(
        trade_case_id=uuid4(),
        task_id=uuid4(),
        market_pair_id=pair_id,
        trigger=watched(now) if trigger == "default" else trigger,
        observation=observed(now) if observation == "default" else observation,
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
    """The one read the context needs, and no write of any kind."""

    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        return self._snapshot


@pytest.fixture
def market():
    return market_identity()
