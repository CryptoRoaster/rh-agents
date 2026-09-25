"""The risk boundary converts recorded market facts to ledger precision explicitly.

Recorded prices and liquidity keep the market layer's precision everywhere
upstream. SENTINEL's `MarketSnapshot` is ledger-typed (`Numeric(38, 18)`), so
`risk_market` is where the conversion happens: the price rounded up, because
sizing only buys and a lower ledger price would understate the notional
SENTINEL checks; liquidity floored, so it can only look thinner; and anything
the ledger cannot express refused with a typed code rather than zeroed, capped
or raised. No database is involved.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.models import AgentRole, MarketSnapshot, RiskLimits, Side
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


def build(now, snapshot, *, key: str = "precision", side: Side = Side.BUY):
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
        side=side,
    )


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def test_a_wide_price_is_rounded_up_at_the_boundary(now) -> None:
    snapshot = recorded_snapshot(now, price=WIDE_PRICE)
    market = build(now, snapshot)

    assert market.price_usd == Decimal("1.250000000000000001")
    assert market.price_usd >= WIDE_PRICE
    assert market.price_usd - WIDE_PRICE < LEDGER_UNIT
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


LEDGER_UNIT = Decimal("0.000000000000000001")


def test_an_exact_18_place_price_is_unchanged(now) -> None:
    exact = Decimal("1.234567890123456789")
    assert build(now, recorded_snapshot(now, price=exact)).price_usd == exact


@pytest.mark.parametrize(
    "recorded",
    [
        Decimal("0.0000000000000000014"),
        Decimal("0.0000000000000000015"),
        Decimal("1.2500000000000000004"),
        Decimal("99.9999999999999999991"),
    ],
)
def test_a_wide_price_never_rounds_down(now, recorded) -> None:
    price = build(now, recorded_snapshot(now, price=recorded)).price_usd
    assert price >= recorded
    assert price - recorded < LEDGER_UNIT


def test_sizing_to_risk_never_understates_the_buy_notional(now) -> None:
    """The real path: recorded price, sizing, `risk_market`, SENTINEL's snapshot.

    Sizing divides by the exact recorded price and floors the quantity. If the
    ledger price were then rounded down, SENTINEL's `quantity * price_usd`
    would be smaller than what the quantity actually costs at the recorded
    price — here by more than a quarter of the whole notional.
    """
    from src.core.models import TradingMode
    from src.orchestration.sizing.calculator import assess_paper_sizing
    from src.orchestration.sizing.models import SizingAssessment
    from src.orchestration.sizing.policy import PAPER_SIZING_V1

    recorded = Decimal("0.0000000000000000014")
    snapshot = recorded_snapshot(now, price=recorded)
    sizing = assess_paper_sizing(
        trade_case_id=uuid4(),
        base_asset_id=BASE_ASSET,
        setup_evidence_id=uuid4(),
        side=Side.BUY,
        trading_mode=TradingMode.PAPER,
        requested_notional_usd=Decimal("1"),
        price=reference_price(snapshot),
        base_asset=base_asset_metadata(snapshot),
        now=now,
        policy=PAPER_SIZING_V1,
    )
    assert isinstance(sizing, SizingAssessment), sizing
    assert sizing.reference_price.usd_per_base_unit == recorded

    market = risk_market(
        base_asset_id=BASE_ASSET,
        price=sizing.reference_price,
        base_asset=base_asset_metadata(snapshot),
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
        identity_key="sizing-path",
        side=Side.BUY,
    )
    assert market.price_usd >= recorded
    assert sizing.quantity * market.price_usd >= sizing.quantity * recorded


def test_rounding_up_out_of_the_ledger_range_is_refused(now) -> None:
    """Twenty integer digits fit; the carry that makes a twenty-first does not."""
    carry = Decimal("99999999999999999999.9999999999999999991")
    with pytest.raises(RiskRequestUnavailable) as refused:
        build(now, recorded_snapshot(now, price=carry))
    assert refused.value.reason_code == "REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION"


def test_a_price_too_large_for_the_ledger_is_refused(now) -> None:
    with pytest.raises(RiskRequestUnavailable) as refused:
        build(now, recorded_snapshot(now, price=Decimal("1E+25")))
    assert refused.value.reason_code == "REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION"


def test_liquidity_too_large_for_the_ledger_is_refused_not_capped(now) -> None:
    with pytest.raises(RiskRequestUnavailable) as refused:
        build(now, recorded_snapshot(now, liquidity=Decimal("123456789012345678901234.5")))
    assert refused.value.reason_code == "LIQUIDITY_OUTSIDE_ACCOUNTING_PRECISION"


@pytest.mark.parametrize(
    "recorded",
    [Decimal("0.0000000000000000014"), Decimal("1.2500000000000000009")],
)
def test_an_exit_is_priced_by_a_floor_never_above_the_recorded_price(now, recorded) -> None:
    """A sell's conservative direction is down: it may understate, never overstate, proceeds."""
    price = build(now, recorded_snapshot(now, price=recorded), side=Side.SELL).price_usd
    assert price <= recorded
    assert recorded - price < LEDGER_UNIT
    assert price > 0


def test_a_sub_precision_price_is_refused_for_an_exit_too(now) -> None:
    with pytest.raises(RiskRequestUnavailable) as refused:
        build(now, recorded_snapshot(now, price=Decimal("0.0000000000000000004")), side=Side.SELL)
    assert refused.value.reason_code == "REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION"
