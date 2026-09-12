"""Fixtures for VECTOR: one market, one price, and proposals about it.

Every number here is chosen so the arithmetic is obvious at a glance. The
observed price is 1.00 USD per base unit, which makes an entry of 1.10 a ten
per cent breakout and an entry of 1000000 unmistakably a lost decimal point.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.agents.vector.models import (
    ObservedMeasurement,
    SetupKind,
    VectorMarketContext,
    VectorReasonCode,
    VectorSetupProposal,
    VectorTaskInput,
)
from src.agents.vector.policy import VECTOR_SETUP_V1
from src.core.models import Side
from src.markets.models import Availability, MarketIdentity
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

CHAIN = "robinhood"
NETWORK = "mainnet"
TOKEN = "0x" + "a1" * 20
QUOTE = "0x" + "b2" * 20
POOL = "0x" + "e5" * 20
PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{POOL}"

# One dollar per base unit, so every level below reads as a percentage.
SPOT = Decimal("1.00")


def stable_id(label: str):
    return uuid5(NAMESPACE_URL, f"rh-agents:vector-test:{label}")


def market_identity(chain: str = CHAIN, network: str = NETWORK) -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network=network,
        pair_id=PAIR_ID,
        base_asset_id=f"{chain}:{network}:{TOKEN}",
        quote_asset_id=f"{chain}:{network}:{QUOTE}",
        venue="uniswap-v3",
        is_fixture=False,
    )


def measurement(
    label: str,
    now,
    *,
    value: Decimal | None = SPOT,
    status: Availability = Availability.AVAILABLE,
    minutes_ago: int = 1,
) -> ObservedMeasurement:
    return ObservedMeasurement(
        observation_id=stable_id(label),
        status=status,
        value_usd=value,
        observed_at=now - timedelta(minutes=minutes_ago),
    )


def market_context(now, *, price: Decimal | None = SPOT, minutes_ago: int = 1):
    return VectorMarketContext(
        snapshot_id=stable_id("snapshot"),
        pair_id=PAIR_ID,
        chain=CHAIN,
        network=NETWORK,
        venue="uniswap-v3",
        base_asset_id=f"{CHAIN}:{NETWORK}:{TOKEN}",
        quote_asset_id=f"{CHAIN}:{NETWORK}:{QUOTE}",
        base_symbol="DEMO",
        provider="geckoterminal",
        is_fixture=False,
        observed_at=now - timedelta(minutes=minutes_ago),
        age_seconds=minutes_ago * 60,
        price=measurement("price", now, value=price, minutes_ago=minutes_ago),
        liquidity=measurement("liquidity", now, value=Decimal("250000"), minutes_ago=minutes_ago),
        volume=measurement("volume", now, value=Decimal("80000"), minutes_ago=minutes_ago),
        volume_window_seconds=86400,
    )


def task_input(now, *, evidence=(), price: Decimal | None = SPOT) -> VectorTaskInput:
    return VectorTaskInput(
        trade_case_id=uuid4(),
        task_id=uuid4(),
        market=market_context(now, price=price),
        evidence=tuple(evidence),
        policy_version=VECTOR_SETUP_V1.version,
        evaluated_at=now,
    )


def breakout(now, **overrides) -> VectorSetupProposal:
    """A coherent breakout: cross 1.10, wrong below 0.92, objectives above."""
    defaults: dict[str, object] = dict(
        kind=SetupKind.BREAKOUT_LONG,
        side=Side.BUY,
        entry_low=Decimal("1.10"),
        entry_high=Decimal("1.10"),
        invalidation_price=Decimal("0.92"),
        targets=(Decimal("1.25"), Decimal("1.45")),
        expires_at=now + timedelta(hours=2),
        reason_codes=(VectorReasonCode.PRICE_AVAILABLE, VectorReasonCode.LIQUIDITY_PRESENT),
        cited_observation_ids=(stable_id("price"),),
        summary="Waiting for a move through 1.10; the idea fails below 0.92.",
    )
    defaults.update(overrides)
    return VectorSetupProposal(**defaults)  # type: ignore[arg-type]


def pullback(now, **overrides) -> VectorSetupProposal:
    """A coherent pullback: buy the 0.90–0.95 band, wrong below 0.85."""
    defaults: dict[str, object] = dict(
        kind=SetupKind.PULLBACK_LONG,
        side=Side.BUY,
        entry_low=Decimal("0.90"),
        entry_high=Decimal("0.95"),
        invalidation_price=Decimal("0.85"),
        targets=(Decimal("1.10"), Decimal("1.30")),
        expires_at=now + timedelta(hours=1),
        reason_codes=(VectorReasonCode.PRICE_AVAILABLE,),
        cited_observation_ids=(),
        summary="Buying a retrace into 0.90-0.95; invalid under 0.85.",
    )
    defaults.update(overrides)
    return VectorSetupProposal(**defaults)  # type: ignore[arg-type]


def proposal_payload(now, **overrides) -> dict[str, object]:
    """The same breakout as a scripted provider reply."""
    proposal = breakout(now, **overrides)
    return proposal.model_dump(mode="json")


class StubMarkets:
    """The one read the context needs, and no write of any kind."""

    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        return self._snapshot


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


@pytest.fixture
def market():
    return market_identity()
