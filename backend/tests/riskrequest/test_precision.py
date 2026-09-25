"""The risk boundary converts recorded market facts to ledger precision explicitly.

Recorded prices and liquidity keep the market layer's precision everywhere
upstream. SENTINEL's `MarketSnapshot` is ledger-typed (`Numeric(38, 18)`), so
`risk_market` is where the conversion happens: the price through
`src.core.numbers.quantize`, liquidity floored so it can only look thinner, and
a positive price the ledger cannot express refused with a typed code rather
than passed on as zero. No database is involved.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.models import AgentRole, MarketSnapshot, RiskLimits
from src.core.numbers import quantize, quantize_down
from src.orchestration.riskrequest.service import RiskRequestUnavailable, risk_market
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

WIDE_PRICE = Decimal("1.25000000000000000000123")
CORRELATION = UUID("00000000-0000-4000-8000-00000000c0de")


def build(now, snapshot, *, key: str = "precision"):
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
        identity_key=key,
    )


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def test_a_wide_price_is_quantized_explicitly_at_the_boundary(now) -> None:
    snapshot = recorded_snapshot(now, price=WIDE_PRICE)
    market = build(now, snapshot)

    assert market.price_usd == quantize(WIDE_PRICE)
    assert market.price_usd == Decimal("1.250000000000000000")
    assert places(market.price_usd) <= 18
    # The recorded fact itself is untouched.
    assert snapshot.price.value_usd == WIDE_PRICE
    assert snapshot.price.value_usd.as_tuple() == WIDE_PRICE.as_tuple()


def test_the_same_inputs_give_the_same_market(now) -> None:
    snapshot = recorded_snapshot(now, price=WIDE_PRICE)
    first = build(now, snapshot)
    second = build(now, snapshot)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_a_positive_price_below_ledger_precision_is_refused_not_zeroed(now) -> None:
    tiny = Decimal("0.0000000000000000004")
    assert tiny > 0 and quantize(tiny) == 0
    snapshot = recorded_snapshot(now, price=tiny)
    with pytest.raises(RiskRequestUnavailable) as refused:
        build(now, snapshot)
    assert refused.value.reason_code == "REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION"
    assert str(refused.value) == "REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION"


def test_liquidity_is_floored_never_rounded_up_over_a_limit(now) -> None:
    just_below = Decimal("99999.9999999999999999999")
    # Half-even rounding would have made this exactly the default minimum.
    assert quantize(just_below) == Decimal("100000")
    market = build(now, recorded_snapshot(now, liquidity=just_below))
    assert market.liquidity.liquidity_usd == Decimal("99999.999999999999999999")
    assert market.liquidity.liquidity_usd < RiskLimits().min_liquidity_usd


def test_sub_precision_liquidity_becomes_zero_and_is_rejected(intent, market, context, now):
    """Zero is the conservative answer, and the engine treats it as insufficient."""
    tiny = Decimal("0.0000000000000000000001")
    assert quantize_down(tiny) == 0
    thin = market.model_copy(
        update={
            "liquidity": market.liquidity.model_copy(update={"liquidity_usd": quantize_down(tiny)})
        }
    )
    decision = evaluate(intent, thin, context, RiskLimits(), now=now)
    assert "INSUFFICIENT_LIQUIDITY" in decision.reason_codes


def test_ledger_types_are_still_18_places() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as refused:
        MarketSnapshot.model_validate({"price_usd": "1.0000000000000000001"})
    assert any(
        error["loc"] == ("price_usd",) and error["type"] == "decimal_max_places"
        for error in refused.value.errors()
    )


def test_quantize_down_truncates_toward_zero() -> None:
    assert quantize_down(Decimal("1.0000000000000000009")) == Decimal("1.000000000000000000")
    assert quantize_down(Decimal("123456789012345678901.5")) == Decimal(
        "123456789012345678901.500000000000000000"
    )
    assert quantize_down(Decimal("0.00000000000000000099")) == 0
