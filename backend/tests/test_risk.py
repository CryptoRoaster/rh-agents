from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.models import (
    RiskDecision,
    RiskLimits,
    RiskOutcome,
    SafetyStatus,
    Side,
    TradeIntent,
    TradingMode,
)
from src.risk.engine import evaluate


def test_valid_intent_is_approved(intent, market, context, now):
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.APPROVE
    assert risk.intent_fingerprint == intent.fingerprint()
    assert risk.metrics.worst_case_notional_usd == Decimal("202.202")


@pytest.mark.parametrize(
    "section,field",
    [
        ("token", "tradable"),
        ("liquidity", "routing"),
        ("holders", "concentration_check"),
    ],
)
@pytest.mark.parametrize("status", [SafetyStatus.UNKNOWN, SafetyStatus.FAIL])
def test_non_pass_safety_checks_reject(intent, market, context, now, section, field, status):
    nested = getattr(market, section).model_copy(update={field: status})
    market = market.model_copy(update={section: nested})
    assert evaluate(intent, market, context, RiskLimits(), now=now).outcome == RiskOutcome.REJECT


@pytest.mark.parametrize(
    "field", ["cash_usd", "exposure_usd", "position_quantity", "daily_loss_usd"]
)
def test_unknown_portfolio_rejects(intent, market, context, now, field):
    context = context.model_copy(update={field: None})
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert "PORTFOLIO_DATA_UNKNOWN" in risk.reason_codes
    assert risk.max_additional_notional_usd == 0


@pytest.mark.parametrize(
    "section,field",
    [("liquidity", "liquidity_usd"), ("liquidity", "estimated_slippage_bps"), (None, "fee_bps")],
)
def test_unknown_cost_or_liquidity_rejects(intent, market, context, now, section, field):
    if section:
        market = market.model_copy(
            update={section: getattr(market, section).model_copy(update={field: None})}
        )
    else:
        market = market.model_copy(update={field: None})
    assert evaluate(intent, market, context, RiskLimits(), now=now).outcome == RiskOutcome.REJECT


@pytest.mark.parametrize("offset", [-31, 1])
def test_stale_or_future_market_rejects(intent, market, context, now, offset):
    market = market.model_copy(update={"observed_at": now + timedelta(seconds=offset)})
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert "STALE_OR_FUTURE_MARKET" in risk.reason_codes


def test_stale_safety_data_rejects(intent, market, context, now):
    market = market.model_copy(
        update={
            "holders": market.holders.model_copy(update={"created_at": now - timedelta(minutes=1)})
        }
    )
    assert (
        "STALE_OR_FUTURE_SAFETY_DATA"
        in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("max_exposure_usd", "200", "MAX_EXPOSURE"),
        ("max_position_size_usd", "200", "MAX_POSITION_SIZE"),
        ("max_slippage_bps", "10", "SLIPPAGE_LIMIT"),
        ("min_liquidity_usd", "2000000", "INSUFFICIENT_LIQUIDITY"),
    ],
)
def test_hard_limits(intent, market, context, now, field, value, reason):
    limits = RiskLimits(**{field: Decimal(value)})
    risk = evaluate(intent, market, context, limits, now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert reason in risk.reason_codes


def test_position_cap_includes_existing_position_and_costs(intent, market, context, now):
    context = context.model_copy(
        update={"position_quantity": Decimal("23"), "exposure_usd": Decimal("2300")}
    )
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert "MAX_POSITION_SIZE" in risk.reason_codes


def test_unknown_accounting_rejects(intent, market, context, now):
    context = context.model_copy(update={"accounting": SafetyStatus.UNKNOWN})
    assert (
        "ACCOUNTING_UNKNOWN"
        in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


@pytest.mark.parametrize("kill,loss", [(True, "0"), (False, "500")])
def test_pause_conditions(intent, market, context, now, kill, loss):
    context = context.model_copy(update={"daily_loss_usd": Decimal(loss)})
    assert (
        evaluate(intent, market, context, RiskLimits(kill_switch=kill), now=now).outcome
        == RiskOutcome.PAUSE_SYSTEM
    )


@pytest.mark.parametrize("mode", [TradingMode.OBSERVE, TradingMode.LIVE_AUTONOMOUS])
def test_only_paper_can_be_approved(intent, market, context, now, mode):
    intent = intent.model_copy(update={"mode": mode})
    assert (
        "MODE_NOT_EXECUTABLE"
        in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


def test_sell_requires_holdings(intent, market, context, now):
    intent = intent.model_copy(update={"side": Side.SELL})
    assert (
        "INSUFFICIENT_POSITION"
        in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_invalid_quantities_fail_validation(intent, quantity):
    data = intent.model_dump()
    data["quantity"] = quantity
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(data)


def test_insufficient_cash_includes_fees(intent, market, context, now):
    context = context.model_copy(update={"cash_usd": Decimal("202")})
    assert (
        "INSUFFICIENT_CASH" in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("holder_count", None, "HOLDER_METRICS_UNKNOWN"),
        ("top_ten_fraction", None, "HOLDER_METRICS_UNKNOWN"),
        ("holder_count", 0, "HOLDER_CONCENTRATION_LIMIT"),
        ("top_ten_fraction", Decimal("0.36"), "HOLDER_CONCENTRATION_LIMIT"),
    ],
)
def test_holder_metrics_enforced_independent_of_pass_status(
    intent, market, context, now, field, value, reason
):
    market = market.model_copy(update={"holders": market.holders.model_copy(update={field: value})})
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert reason in risk.reason_codes


def test_missing_decision_timestamp_rejects(intent, market, context, now):
    intent = intent.model_copy(
        update={"timing": intent.timing.model_copy(update={"decision_at": None})}
    )
    assert evaluate(intent, market, context, RiskLimits(), now=now).outcome == RiskOutcome.REJECT


@pytest.mark.parametrize(
    "headroom",
    [
        {"cash_usd": Decimal("101.101")},
        {"exposure_usd": Decimal("9898.899")},
        {"position_quantity": Decimal("23.98899"), "exposure_usd": Decimal("2398.899")},
    ],
    ids=["cash", "exposure", "position"],
)
def test_buy_guidance_accounts_for_each_headroom_and_costs(intent, market, context, now, headroom):
    context = context.model_copy(update=headroom)
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.position_size_limit_usd == Decimal("2500")
    assert risk.max_additional_notional_usd == Decimal("100")
    assert risk.outcome == RiskOutcome.REJECT  # The original $200 intent is still too large.
    resized = intent.model_copy(update={"quantity": Decimal("1")})
    assert evaluate(resized, market, context, RiskLimits(), now=now).outcome == RiskOutcome.APPROVE


@pytest.mark.parametrize(
    "headroom",
    [
        {"cash_usd": Decimal("0")},
        {"exposure_usd": Decimal("10000")},
        {"exposure_usd": Decimal("11000")},
        {"position_quantity": Decimal("25")},
        {"position_quantity": Decimal("26")},
    ],
)
def test_exhausted_or_exceeded_headroom_never_returns_negative_guidance(
    intent, market, context, now, headroom
):
    risk = evaluate(intent, market, context.model_copy(update=headroom), RiskLimits(), now=now)
    assert risk.max_additional_notional_usd == 0
    assert risk.outcome == RiskOutcome.REJECT


@pytest.mark.parametrize("holding", [Decimal("0"), Decimal("2")])
def test_sell_has_zero_additional_buy_guidance(intent, market, context, now, holding):
    intent = intent.model_copy(update={"side": Side.SELL})
    context = context.model_copy(update={"position_quantity": holding})
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.max_additional_notional_usd == 0
    assert risk.position_size_limit_usd == Decimal("2500")
    assert risk.outcome == (RiskOutcome.APPROVE if holding else RiskOutcome.REJECT)


@pytest.mark.parametrize("blocker", ["fees", "token", "stale", "loss", "kill", "accounting"])
def test_unsafe_context_has_zero_sizing_guidance(intent, market, context, now, blocker):
    limits = RiskLimits()
    if blocker == "fees":
        market = market.model_copy(update={"fee_bps": None})
    elif blocker == "token":
        market = market.model_copy(
            update={"token": market.token.model_copy(update={"tradable": SafetyStatus.UNKNOWN})}
        )
    elif blocker == "stale":
        now += timedelta(seconds=31)
    elif blocker == "loss":
        context = context.model_copy(update={"daily_loss_usd": Decimal("500")})
    elif blocker == "kill":
        limits = RiskLimits(kill_switch=True)
    else:
        context = context.model_copy(update={"accounting": SafetyStatus.UNKNOWN})
    risk = evaluate(intent, market, context, limits, now=now)
    assert risk.outcome != RiskOutcome.APPROVE
    assert risk.max_additional_notional_usd == 0


@pytest.mark.parametrize("cash", ["1", "0.000000000000000001", "1.000000000000000001"])
def test_sizing_rounds_down_and_survives_final_cost_rounding(intent, market, context, now, cash):
    market = market.model_copy(update={"price_usd": Decimal("1"), "fee_bps": Decimal("789")})
    context = context.model_copy(update={"cash_usd": Decimal(cash)})
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    capacity = risk.max_additional_notional_usd
    assert 0 <= capacity <= Decimal(cash) / Decimal("1.089689")
    if capacity:
        resized = intent.model_copy(update={"quantity": capacity})
        final = evaluate(resized, market, context, RiskLimits(), now=now)
        assert final.outcome == RiskOutcome.APPROVE
        assert final.metrics.worst_case_notional_usd <= Decimal(cash)


def test_smaller_slippage_allowance_increases_guidance(intent, market, context, now):
    context = context.model_copy(update={"cash_usd": Decimal("100")})
    default = evaluate(intent, market, context, RiskLimits(), now=now)
    tighter = intent.model_copy(update={"max_slippage_bps": Decimal("50")})
    risk = evaluate(tighter, market, context, RiskLimits(), now=now)
    assert risk.max_additional_notional_usd > default.max_additional_notional_usd


def test_sizing_guidance_does_not_bypass_final_safety_checks(intent, market, context, now):
    guidance = evaluate(intent, market, context, RiskLimits(), now=now)
    resized = intent.model_copy(
        update={
            "quantity": (guidance.max_additional_notional_usd / market.price_usd).quantize(
                Decimal("0.000001"), rounding="ROUND_DOWN"
            )
        }
    )
    unsafe = market.model_copy(update={"fee_bps": None})
    final = evaluate(resized, unsafe, context, RiskLimits(), now=now)
    assert final.outcome == RiskOutcome.REJECT
    assert "FEES_UNKNOWN" in final.reason_codes


def test_legacy_risk_payload_reads_without_inventing_capacity(intent, market, context, now):
    data = evaluate(intent, market, context, RiskLimits(), now=now).model_dump(mode="json")
    data["max_allowed_position_size_usd"] = data.pop("position_size_limit_usd")
    del data["max_additional_notional_usd"]
    risk = RiskDecision.model_validate(data)
    assert risk.position_size_limit_usd == Decimal("2500")
    assert risk.max_additional_notional_usd == 0
    assert "max_allowed_position_size_usd" not in risk.model_dump()


def test_new_risk_payload_requires_nonnegative_capacity(intent, market, context, now):
    data = evaluate(intent, market, context, RiskLimits(), now=now).model_dump()
    data["max_additional_notional_usd"] = Decimal("-1")
    with pytest.raises(ValidationError):
        RiskDecision.model_validate(data)
    del data["max_additional_notional_usd"]
    with pytest.raises(ValidationError):
        RiskDecision.model_validate(data)
