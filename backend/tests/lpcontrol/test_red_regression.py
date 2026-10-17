"""Issue #92: a BUY on a 20-byte pool may not pass without LP-control evidence.

Every fact SENTINEL asked for before this issue is PASS here: the token, the
route, the holder domain, accounting, liquidity well over the minimum, slippage
inside the limit and a raw top ten of 0.31, under the 0.35 limit. What nobody
established is who can withdraw the pool's liquidity -- here the creator holds
the LP tokens in their own wallet, and the raw top ten counts the pair contract
as one ordinary holder. No V2/V3 LP-control source exists, so the BUY must be
refused, on every 20-byte venue the discovery shadow saw.

Built through the production market view (`risk_market`) and the production
engine; no provider is contacted.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.models import (
    AgentRole,
    ExecutionTiming,
    RiskLimits,
    RiskOutcome,
    Side,
    TradeIntent,
)
from src.orchestration.riskrequest.service import risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType
from src.risk.engine import evaluate
from tests.anchor.conftest import evidence_envelope
from tests.riskdata.conftest import (
    BASE_ASSET,
    anchor_payload,
    configured_costs,
    onchain_payload,
    recorded_snapshot,
)

CORRELATION = UUID("00000000-0000-4000-8000-00000000092a")
# The 20-byte venues of the 2026-10-14 discovery shadow (pools in ~9.5 h).
TWENTY_BYTE_VENUES = (
    "uniswap-v2-robinhood",
    "pons-v2",
    "uniswap-v3-robinhood",
    "ramses-v3-robinhood",
    "virtuals-robinhood",
)


def buy_market(now, venue: str, *, side: Side = Side.BUY):
    snapshot = recorded_snapshot(now, venue=venue)
    assert snapshot.pair.pool_locator is not None
    assert len(snapshot.pair.pool_locator.value) == 42  # a 20-byte pool address
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    return risk_market(
        base_asset_id=BASE_ASSET,
        price=price,
        base_asset=metadata,
        snapshot=snapshot,
        onchain=evidence_envelope(now, EvidenceType.ONCHAIN, AgentRole.ATLAS, onchain_payload(now)),
        anchor=evidence_envelope(
            now,
            EvidenceType.LIQUIDITY_EXECUTION,
            AgentRole.ANCHOR,
            anchor_payload(uuid4(), uuid4()),
        ),
        costs=configured_costs(),
        correlation_id=CORRELATION,
        identity_key=f"issue-92:{venue}:{side.value}",
        side=side,
    )


def trade(now, side: Side = Side.BUY) -> TradeIntent:
    return TradeIntent(
        source="COMMANDER",
        correlation_id=CORRELATION,
        created_at=now,
        updated_at=now,
        asset_id=BASE_ASSET,
        side=side,
        quantity=Decimal("100"),
        signal_price=Decimal("1.25"),
        max_slippage_bps=Decimal("100"),
        timing=ExecutionTiming(detected_at=now - timedelta(seconds=1), decision_at=now),
    )


@pytest.mark.parametrize("venue", TWENTY_BYTE_VENUES)
def test_a_buy_on_a_twenty_byte_pool_without_lp_control_is_refused(now, context, venue) -> None:
    market = buy_market(now, venue)
    limits = RiskLimits()
    # Every bar the engine applied before issue #92 is met.
    assert market.holders.top_ten_fraction == Decimal("0.31")
    assert market.holders.top_ten_fraction < limits.max_top_ten_holder_fraction
    assert market.liquidity.liquidity_usd is not None
    assert market.liquidity.liquidity_usd >= limits.min_liquidity_usd

    decision = evaluate(trade(now), market, context, limits, now=now)

    assert decision.outcome is RiskOutcome.REJECT
    assert decision.reason_codes == ("LP_CONTROL_UNSUPPORTED",)
