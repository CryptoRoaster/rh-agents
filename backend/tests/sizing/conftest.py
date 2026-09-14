"""Fixtures for PAPER sizing.

Deliberately built on the market layer's own fixture snapshot rather than on
hand-made objects: it is internally coherent, it carries real token decimals,
and a price taken from it is a price the recorder would accept. A sizing test
that only worked against a stub would prove the arithmetic and nothing about the
data it is supposed to read.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from src.core.clock import FixedClock
from src.core.models import AgentRole, Side, TradingMode
from src.markets.fake import fixture_snapshot
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.sizing.context import PaperSizingReader
from src.orchestration.sizing.models import BaseAssetMetadata, ReferencePrice
from src.orchestration.sizing.policy import PAPER_SIZING_V1
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.service import TradeCaseService
from tests.worker.conftest import setup_payload, submission
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

# The fixture market's own figures, restated so a test that depends on one says
# which one. The base token has eighteen decimals and costs a little over two
# thousand dollars; both come from `src.markets.fake`.
BASE_ASSET = "ethereum:mainnet:0xfixture-weth"
PAIR_ID = "ethereum:mainnet:fixture-weth-usdc"
FIXTURE_PRICE = Decimal("2345.123456789012345678")


def stable_id(label: str) -> UUID:
    """A repeatable identifier, so "the same inputs" really are the same.

    The identity tests compare digests across separately built argument sets. A
    `uuid4` in a shared builder would make every such comparison differ for a
    reason the test never names, and the assertions would pass while proving
    nothing.
    """
    return uuid5(NAMESPACE_URL, f"rh-agents:sizing-test:{label}")


class RecordedMarkets:
    """One recorded observation per market, answering the single read port.

    No provider, no transport and no way to ask for anything else — the same
    surface `MarketReader.latest` offers, narrowed to what sizing uses.
    """

    def __init__(self, snapshot: MarketSnapshot | None) -> None:
        self._snapshot = snapshot
        self.requested: list[tuple[str, bool]] = []

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        self.requested.append((identity, include_fixtures))
        if self._snapshot is None or self._snapshot.pair.pair_id != identity:
            return None
        if self._snapshot.is_fixture and not include_fixtures:
            return None
        return self._snapshot


def recorded_snapshot(
    now,
    *,
    age: timedelta = timedelta(seconds=30),
    price: Decimal | None = FIXTURE_PRICE,
    decimals: int | None = 18,
    trace=None,
) -> MarketSnapshot:
    """A snapshot as the market layer stores them, with the knobs tests need."""
    base = fixture_snapshot(now - age, trace or uuid4())
    pair = base.pair.model_copy(
        update={"base": base.pair.base.model_copy(update={"decimals": decimals})}
    )
    observation = base.price.model_copy(
        update={
            "status": Availability.AVAILABLE if price is not None else Availability.UNKNOWN,
            "value_usd": price,
        }
    )
    return base.model_copy(update={"pair": pair, "price": observation})


def reference_price(
    now,
    *,
    value: Decimal = FIXTURE_PRICE,
    asset_id: str = BASE_ASSET,
    age=timedelta(seconds=30),
    observation: str = "price",
) -> ReferencePrice:
    return ReferencePrice(
        snapshot_id=stable_id(f"snapshot:{observation}"),
        observation_id=stable_id(f"observation:{observation}"),
        provider="fixture:memory",
        asset_id=asset_id,
        usd_per_base_unit=value,
        observed_at=now - age,
    )


def base_metadata(
    now,
    *,
    decimals: int = 18,
    asset_id: str = BASE_ASSET,
    age=timedelta(seconds=30),
    observation: str = "metadata",
) -> BaseAssetMetadata:
    return BaseAssetMetadata(
        asset_id=asset_id,
        symbol="WETH",
        decimals=decimals,
        source_provider="fixture:memory",
        source_observation_id=stable_id(f"observation:{observation}"),
        source_observed_at=now - age,
    )


def sizing_inputs(now, **overrides):
    """The complete argument set for one successful assessment."""
    arguments = {
        "trade_case_id": stable_id("case"),
        "base_asset_id": BASE_ASSET,
        "setup_evidence_id": stable_id("setup"),
        "side": Side.BUY,
        "trading_mode": TradingMode.PAPER,
        "requested_notional_usd": Decimal("500"),
        "price": reference_price(now),
        "base_asset": base_metadata(now),
        "now": now,
        "policy": PAPER_SIZING_V1,
    }
    return {**arguments, **overrides}


@pytest.fixture
def markets(now):
    return RecordedMarkets(recorded_snapshot(now))


async def open_case(cases, now, trace, key="sizing-case"):
    return await cases.open_trade_case(
        fixture_snapshot(now, trace).pair.market_identity,
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
        strategy_policy_id="paper-policy-v1",
    )


async def record_setup(cases, trade_case, now, *, key=None):
    return await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            setup_payload(),
            key=key or f"sizing-setup-{trade_case.id}",
        ),
    )


async def record_trigger(cases, trade_case, now, setup):
    from tests.worker.conftest import trigger_payload

    return await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup.evidence_id),
            key=f"sizing-trigger-{trade_case.id}",
        ),
    )


async def record_anchor(cases, trade_case, now, setup, trigger, capacity=Decimal("50000")):
    """ANCHOR's finding, carrying a tested capacity nothing here may read.

    A round number in dollars, already validated, sitting exactly where a
    desired size would go. That is what makes it dangerous rather than useful.
    """
    from src.orchestration.workflow.models import LiquidityExecutionPayload

    return await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            LiquidityExecutionPayload(
                setup_evidence_id=setup.evidence_id,
                trigger_evidence_id=trigger.evidence_id,
                quoted_price=Decimal("1"),
                liquidity_usd=Decimal("500000"),
                estimated_slippage_bps=Decimal("25"),
                price_impact_bps=Decimal("20"),
                maximum_safe_size_usd=capacity,
                routing_provenance="quoted-route-v1",
            ),
            key=f"sizing-anchor-{trade_case.id}",
        ),
    )


def build_reader(sessions, now, *, feed, notional=Decimal("500"), **overrides):
    """The real workflow service behind the sizing reader, never a stub."""
    clock = FixedClock(now)
    arguments = {
        "cases": TradeCaseService(sessions, clock=clock),
        "markets": feed,
        "requested_notional_usd": notional,
        "trading_mode": TradingMode.PAPER,
        "clock": clock,
        "include_fixtures": True,
    }
    return PaperSizingReader(**{**arguments, **overrides})
