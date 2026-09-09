from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.models import RiskLimits, RiskOutcome, SafetyStatus, Side, TradeIntent, TradingMode
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
