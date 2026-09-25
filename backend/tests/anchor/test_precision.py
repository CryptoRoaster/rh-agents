"""ANCHOR reads recorded prices at market precision; what it computes stays bounded.

Field by field:

- ``ReferenceMarket.price`` and ``QuoteAssetValuation.usd_per_token`` are
  RECORDED_MARKET_FACTs, copied from a MarketSnapshot. They keep the market
  layer's precision, and so do the same two numbers where the assessment and its
  evidence repeat them.
- ``ReferenceMarket.liquidity_usd`` was already an unbounded Decimal.
- Notionals, token amounts, effective prices, deviations and capacity figures are
  DERIVED_EXECUTION_VALUEs; they are quantized where they are computed and keep
  their 18-place types.
"""

from decimal import Decimal

from src.agents.anchor.assessment import assess
from src.agents.anchor.context import quote_asset_valuation, reference_market
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from tests.anchor.conftest import (
    CHAIN,
    NETWORK,
    QUOTE_TOKEN,
    ladder_from,
    payment_snapshot,
    reference,
    source,
    task_input,
    valuation,
)
from tests.anchor.test_context import snapshot_for

# Synthetic, 23 decimal places.
WIDE_REFERENCE = Decimal("200.00000000000000000000001")
WIDE_PAYMENT = Decimal("1.00000000000000000000001")


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def test_reference_market_keeps_the_recorded_price_exactly(now) -> None:
    built = reference_market(snapshot_for(now, price=WIDE_REFERENCE), now)
    assert built is not None
    assert built.price == WIDE_REFERENCE
    assert built.price.as_tuple() == WIDE_REFERENCE.as_tuple()


def test_quote_asset_valuation_keeps_the_recorded_price_exactly(now) -> None:
    pair = snapshot_for(now)
    built = quote_asset_valuation(
        payment_snapshot(pair, usd_per_token=WIDE_PAYMENT),
        f"{CHAIN}:{NETWORK}:{QUOTE_TOKEN}",
        now,
    )
    assert built is not None
    assert built.usd_per_token == WIDE_PAYMENT
    assert built.usd_per_token.as_tuple() == WIDE_PAYMENT.as_tuple()


async def test_assessment_repeats_recorded_prices_and_bounds_derived_values(now) -> None:
    ladder = await ladder_from(source(now), None)
    result = assess(
        task_input(
            now,
            ladder=ladder,
            ref=reference(now, price=WIDE_REFERENCE),
            value=valuation(now, usd_per_token=WIDE_PAYMENT),
        ),
        now,
        ANCHOR_EXECUTION_V1,
    )
    assert result.reference_price == WIDE_REFERENCE
    assert result.quote_asset_usd_price == WIDE_PAYMENT
    derived = [
        result.largest_tested_acceptable_notional_usd,
        result.first_tested_rejected_notional_usd,
        result.effective_price_usd_at_capacity,
    ]
    for value in derived:
        if value is not None:
            assert places(value) <= 18
    for point in result.ladder:
        for value in (point.notional_usd, point.amount_in_tokens, point.effective_price_usd):
            if value is not None:
                assert places(value) <= 18
