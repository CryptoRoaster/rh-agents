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


def test_only_the_kill_switch_pauses_the_system(intent, market, context, now):
    assert (
        evaluate(intent, market, context, RiskLimits(kill_switch=True), now=now).outcome
        == RiskOutcome.PAUSE_SYSTEM
    )


@pytest.mark.parametrize("loss", ["500", "500.000000000000000001", "900"])
def test_the_daily_loss_limit_refuses_a_buy_without_pausing(intent, market, context, now, loss):
    """At or past the limit: a refused purchase, never a system-wide stop."""
    context = context.model_copy(update={"daily_loss_usd": Decimal(loss)})

    risk = evaluate(intent, market, context, RiskLimits(), now=now)

    assert risk.outcome == RiskOutcome.REJECT
    assert "DAILY_LOSS_LIMIT" in risk.reason_codes
    assert risk.max_additional_notional_usd == 0


def test_a_buy_just_under_the_daily_loss_limit_is_not_refused_for_it(intent, market, context, now):
    context = context.model_copy(update={"daily_loss_usd": Decimal("499.99")})
    assert (
        "DAILY_LOSS_LIMIT"
        not in evaluate(intent, market, context, RiskLimits(), now=now).reason_codes
    )


@pytest.mark.parametrize("loss", ["500", "900"])
def test_the_daily_loss_limit_never_refuses_a_sale(intent, market, context, now, loss):
    """A sale reduces risk; every other check that binds a sale still does."""
    sale = intent.model_copy(update={"side": Side.SELL})
    context = context.model_copy(
        update={"daily_loss_usd": Decimal(loss), "position_quantity": sale.quantity}
    )

    risk = evaluate(sale, market, context, RiskLimits(), now=now)

    assert risk.outcome == RiskOutcome.APPROVE, risk.reason_codes
    assert "DAILY_LOSS_LIMIT" not in risk.reason_codes


def test_a_kill_switch_still_pauses_a_sale_at_the_daily_loss_limit(intent, market, context, now):
    sale = intent.model_copy(update={"side": Side.SELL})
    context = context.model_copy(
        update={"daily_loss_usd": Decimal("900"), "position_quantity": sale.quantity}
    )

    risk = evaluate(sale, market, context, RiskLimits(kill_switch=True), now=now)

    assert risk.outcome == RiskOutcome.PAUSE_SYSTEM
    assert risk.reason_codes == ("KILL_SWITCH",)


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


# ------------------------------------------------------- BUY and SELL semantics


def _thin(market, liquidity="50000"):
    return market.model_copy(
        update={
            "liquidity": market.liquidity.model_copy(update={"liquidity_usd": Decimal(liquidity)})
        }
    )


def _concentrated(market):
    holders = market.holders.model_copy(
        update={"top_ten_fraction": Decimal("0.9"), "concentration_check": SafetyStatus.FAIL}
    )
    return market.model_copy(update={"holders": holders})


def _holding(intent, context):
    sell = intent.model_copy(update={"side": Side.SELL})
    return sell, context.model_copy(update={"position_quantity": intent.quantity})


def test_a_buy_keeps_every_entry_bar(intent, market, context, now):
    """The entry is judged exactly as before: thin and concentrated both reject."""
    thin = evaluate(intent, _thin(market), context, RiskLimits(), now=now)
    assert thin.outcome == RiskOutcome.REJECT
    assert thin.reason_codes == ("INSUFFICIENT_LIQUIDITY", "LIQUIDITY_FDV_RATIO_LIMIT")
    held = evaluate(intent, _concentrated(market), context, RiskLimits(), now=now)
    assert held.outcome == RiskOutcome.REJECT
    assert held.reason_codes == ("HOLDERS_FAIL", "HOLDER_CONCENTRATION_LIMIT")


def test_a_sale_is_not_held_to_the_entry_bars(intent, market, context, now):
    sell, context = _holding(intent, context)
    thin = evaluate(sell, _thin(market), context, RiskLimits(), now=now)
    assert thin.outcome == RiskOutcome.APPROVE
    concentrated = evaluate(sell, _concentrated(market), context, RiskLimits(), now=now)
    assert concentrated.outcome == RiskOutcome.APPROVE


@pytest.mark.parametrize(
    "change,code",
    [
        ("empty_pool", "INSUFFICIENT_LIQUIDITY"),
        ("unknown_liquidity", "LIQUIDITY_UNKNOWN"),
        ("unknown_holders", "HOLDERS_UNKNOWN"),
        ("unmeasured_holders", "HOLDER_METRICS_UNKNOWN"),
        ("token_fail", "TOKEN_FAIL"),
        ("routing_unknown", "ROUTING_UNKNOWN"),
        ("stale_holders", "STALE_OR_FUTURE_SAFETY_DATA"),
    ],
)
def test_a_sale_still_fails_closed(intent, market, context, now, change, code):
    sell, context = _holding(intent, context)
    liquidity, holders, token = market.liquidity, market.holders, market.token
    if change == "empty_pool":
        market = _thin(market, "0")
    elif change == "unknown_liquidity":
        market = market.model_copy(
            update={"liquidity": liquidity.model_copy(update={"liquidity_usd": None})}
        )
    elif change == "unknown_holders":
        market = market.model_copy(
            update={
                "holders": holders.model_copy(update={"concentration_check": SafetyStatus.UNKNOWN})
            }
        )
    elif change == "unmeasured_holders":
        market = market.model_copy(
            update={"holders": holders.model_copy(update={"top_ten_fraction": None})}
        )
    elif change == "token_fail":
        market = market.model_copy(
            update={"token": token.model_copy(update={"tradable": SafetyStatus.FAIL})}
        )
    elif change == "routing_unknown":
        market = market.model_copy(
            update={"liquidity": liquidity.model_copy(update={"routing": SafetyStatus.UNKNOWN})}
        )
    else:
        market = market.model_copy(
            update={
                "holders": holders.model_copy(update={"created_at": now - timedelta(minutes=1)})
            }
        )
    risk = evaluate(sell, market, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert code in risk.reason_codes


def _supply(market, tokens):
    """The token's total supply, in whole tokens at the fixture's 9 decimals."""
    raw = None if tokens is None else str(int(tokens) * 10**market.token.decimals)
    return market.model_copy(
        update={"token": market.token.model_copy(update={"total_supply_raw": raw})}
    )


def test_a_buy_records_its_liquidity_fdv_ratio(intent, market, context, now):
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.APPROVE
    assert risk.metrics.liquidity_fdv_ratio == Decimal("0.2")


@pytest.mark.parametrize(
    "tokens,outcome,codes",
    [
        # FDV 10,000,000: exactly the 10 % floor, which passes.
        ("100000", RiskOutcome.APPROVE, ("WITHIN_LIMITS",)),
        # One whole token more and the pool is under a tenth of the FDV.
        ("100001", RiskOutcome.REJECT, ("LIQUIDITY_FDV_RATIO_LIMIT",)),
        ("10000000", RiskOutcome.REJECT, ("LIQUIDITY_FDV_RATIO_LIMIT",)),
        # FDV 1,000,100: the pool is worth just under the whole token.
        ("10001", RiskOutcome.APPROVE, ("WITHIN_LIMITS",)),
        # FDV 1,000,000 at the ledger price: exactly 100 % there. Neither figure
        # is marked exact, so the true ratio may lie just above it.
        ("10000", RiskOutcome.REJECT, ("LIQUIDITY_FDV_RATIO_UNDECIDABLE",)),
        # The pool would be worth more than every token there is.
        ("9999", RiskOutcome.REJECT, ("LIQUIDITY_FDV_RATIO_UNVERIFIED",)),
        ("1", RiskOutcome.REJECT, ("LIQUIDITY_FDV_RATIO_UNVERIFIED",)),
    ],
)
def test_a_buy_needs_pool_liquidity_in_proportion_to_its_fdv(
    intent, market, context, now, tokens, outcome, codes
):
    risk = evaluate(intent, _supply(market, tokens), context, RiskLimits(), now=now)
    assert risk.outcome == outcome
    assert risk.reason_codes == codes


def test_the_liquidity_fdv_floor_is_a_limit(intent, market, context, now):
    risk = evaluate(
        intent, market, context, RiskLimits(min_liquidity_fdv_ratio=Decimal("0.25")), now=now
    )
    assert risk.reason_codes == ("LIQUIDITY_FDV_RATIO_LIMIT",)
    assert risk.max_additional_notional_usd == 0


@pytest.mark.parametrize("value", ["0", "-0.1", "1.01"])
def test_the_liquidity_fdv_floor_cannot_be_disabled_or_unreachable(value):
    with pytest.raises(ValidationError):
        RiskLimits(min_liquidity_fdv_ratio=Decimal(value))


@pytest.mark.parametrize("tokens", [None, "0"])
def test_an_unknown_fdv_refuses_a_buy(intent, market, context, now, tokens):
    risk = evaluate(intent, _supply(market, tokens), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("FDV_UNKNOWN",)
    assert risk.metrics.liquidity_fdv_ratio is None


def test_unknown_liquidity_is_not_also_judged_against_the_fdv(intent, market, context, now):
    market = market.model_copy(
        update={"liquidity": market.liquidity.model_copy(update={"liquidity_usd": None})}
    )
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    assert risk.reason_codes == ("LIQUIDITY_UNKNOWN",)
    assert risk.metrics.liquidity_fdv_ratio is None


@pytest.mark.parametrize("tokens", [None, "0", "10000000", "1"])
def test_a_sale_is_not_held_to_the_liquidity_fdv_ratio(intent, market, context, now, tokens):
    sell, context = _holding(intent, context)
    risk = evaluate(sell, _supply(market, tokens), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.APPROVE
    assert risk.metrics.liquidity_fdv_ratio is None


def test_records_without_a_supply_keep_their_shape(intent, market, context, now):
    """A snapshot or metrics recorded before the ratio existed serialize unchanged."""
    bare = _supply(market, None)
    assert "total_supply_raw" not in bare.token.model_dump()
    assert "total_supply_raw" not in bare.model_dump_json()
    sell, context = _holding(intent, context)
    metrics = evaluate(sell, market, context, RiskLimits(), now=now).metrics
    assert "liquidity_fdv_ratio" not in metrics.model_dump()


def _scale(market, decimals):
    """The contract's own decimals for the supply, beside the provider's."""
    return market.model_copy(
        update={"token": market.token.model_copy(update={"total_supply_decimals": decimals})}
    )


def test_an_fdv_without_the_contracts_decimals_is_unknown(intent, market, context, now):
    """A supply with no scale read beside it is not valued by the provider's decimals."""
    risk = evaluate(intent, _scale(market, None), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("FDV_UNKNOWN",)
    assert risk.metrics.liquidity_fdv_ratio is None


@pytest.mark.parametrize("decimals", [0, 8, 10, 18, 36])
def test_decimals_the_contract_and_provider_disagree_on_refuse_a_buy(
    intent, market, context, now, decimals
):
    """Either scale could be the wrong one; the FDV is not guessed between them."""
    risk = evaluate(intent, _scale(market, decimals), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("FDV_DECIMALS_MISMATCH",)
    assert risk.metrics.liquidity_fdv_ratio is None


@pytest.mark.parametrize("decimals", [None, 0, 18])
def test_a_sale_is_not_held_to_the_contracts_decimals(intent, market, context, now, decimals):
    sell, context = _holding(intent, context)
    risk = evaluate(sell, _scale(market, decimals), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.APPROVE


def _priced(market, price, raw, decimals):
    token = market.token.model_copy(
        update={"decimals": decimals, "total_supply_raw": raw, "total_supply_decimals": decimals}
    )
    return market.model_copy(update={"price_usd": Decimal(price), "token": token})


def test_one_ledger_unit_under_the_floor_refuses(intent, market, context, now):
    """The floor is compared exactly, never after rounding the ratio."""
    # Liquidity 1,000,000; FDV a hair above 10,000,000 puts the ratio just under 0.1.
    shifted = _priced(market, "100.000000000000000001", "100000000000000", 9)
    risk = evaluate(intent, shifted, context, RiskLimits(), now=now)
    assert "LIQUIDITY_FDV_RATIO_LIMIT" in risk.reason_codes
    assert risk.metrics.liquidity_fdv_ratio == Decimal("0.099999999999999999")


def test_a_full_uint256_supply_is_valued_exactly(intent, market, context, now):
    """78 digits of supply at 36 decimals: no float, no overflow, no silent rounding."""
    raw = str(2**256 - 1)
    extreme = _priced(market, "0.000000000000000003", raw, 36)
    risk = evaluate(intent, extreme, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    # FDV is about 3.47e23 USD against a 1,000,000 pool.
    assert "LIQUIDITY_FDV_RATIO_LIMIT" in risk.reason_codes
    assert risk.metrics.liquidity_fdv_ratio == Decimal("0.000000000000000002")


def test_a_ratio_above_one_is_unverified_evidence_not_a_token_finding(intent, market, context, now):
    """A pool worth more than every token there is refuses the entry, and only that.

    It names the evidence, not the token: no safety domain turns FAIL, the
    ratio is still recorded, and a sale against the same market passes.
    """
    risk = evaluate(intent, _supply(market, "1"), context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("LIQUIDITY_FDV_RATIO_UNVERIFIED",)
    assert not any(code.endswith("_FAIL") for code in risk.reason_codes)
    assert risk.metrics.liquidity_fdv_ratio == Decimal("10000")
    sell, holding = _holding(intent, context)
    assert evaluate(sell, _supply(market, "1"), holding, RiskLimits(), now=now).outcome == (
        RiskOutcome.APPROVE
    )


def test_a_kill_switch_still_outranks_the_ratio(intent, market, context, now):
    """The gate adds a refusal; it never turns a pause into a plain rejection."""
    risk = evaluate(intent, _supply(market, "1"), context, RiskLimits(kill_switch=True), now=now)
    assert risk.outcome == RiskOutcome.PAUSE_SYSTEM
    assert risk.reason_codes[0] == "KILL_SWITCH"


def test_a_snapshot_without_contract_decimals_keeps_its_shape(market):
    bare = _scale(market, None)
    assert "total_supply_decimals" not in bare.token.model_dump()
    assert "total_supply_decimals" not in bare.model_dump_json()


def test_a_rounded_up_micro_price_cannot_hide_a_ratio_above_one(intent, market, context, now):
    """At two ledger units the true price may be barely one: the ceiling is judged there.

    6e23 tokens at 2e-18 USD give an FDV of 1,200,000 against a 1,000,000 pool,
    a ratio of 0.83. Had the recorded price been just above 1e-18, the ratio
    was nearly 1.67.
    """
    raw = str(6 * 10**23 * 10**9)
    micro = _priced(market, "0.000000000000000002", raw, 9)
    risk = evaluate(intent, micro, context, RiskLimits(), now=now)
    assert "LIQUIDITY_FDV_RATIO_UNDECIDABLE" in risk.reason_codes
    assert risk.metrics.liquidity_fdv_ratio == Decimal("0.833333333333333333")


def test_a_price_of_one_ledger_unit_proves_no_ceiling(intent, market, context, now):
    """The lowest price it can stand for is zero, so no FDV is proven to exceed the pool."""
    raw = str(10**30 * 10**9)
    micro = _priced(market, "0.000000000000000001", raw, 9)
    risk = evaluate(intent, micro, context, RiskLimits(), now=now)
    assert "LIQUIDITY_FDV_RATIO_UNDECIDABLE" in risk.reason_codes


def test_contract_decimals_are_strict(market):
    with pytest.raises(ValidationError):
        market.token.model_validate({**market.token.model_dump(), "total_supply_decimals": True})


def _exact(market, price=True, liquidity=True):
    """Mark the price and liquidity as the recorded figures, or as rounded ones."""
    return market.model_copy(
        update={
            "price_exact": price,
            "liquidity": market.liquidity.model_copy(update={"liquidity_exact": liquidity}),
        }
    )


# Supply in raw units at the fixture's 9 decimals, against 1,000,000 of pool
# liquidity at a price of 100: each FDV is supply x 100.
@pytest.mark.parametrize(
    "raw,codes",
    [
        # 9.99990000099 %: below the floor however it was rounded.
        ("100001000000000", ("LIQUIDITY_FDV_RATIO_LIMIT",)),
        # Exactly 10 %.
        ("100000000000000", ("WITHIN_LIMITS",)),
        # Exactly 100 %.
        ("10000000000000", ("WITHIN_LIMITS",)),
        # 100.0001 %: FDV 999,999.
        ("9999990000000", ("LIQUIDITY_FDV_RATIO_UNVERIFIED",)),
    ],
)
def test_the_boundaries_on_exact_figures(intent, market, context, now, raw, codes):
    exact = _exact(_supply_raw(market, raw))
    risk = evaluate(intent, exact, context, RiskLimits(), now=now)
    assert risk.reason_codes == codes


def _supply_raw(market, raw):
    return market.model_copy(
        update={"token": market.token.model_copy(update={"total_supply_raw": raw})}
    )


@pytest.mark.parametrize("price,liquidity", [(False, True), (True, False), (None, None)])
def test_exactly_one_hundred_percent_on_rounded_figures_is_undecidable(
    intent, market, context, now, price, liquidity
):
    """A rounded price or a floored pool may hide a ratio above one: refused, not guessed."""
    rounded = _exact(_supply_raw(market, "10000000000000"), price, liquidity)
    risk = evaluate(intent, rounded, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("LIQUIDITY_FDV_RATIO_UNDECIDABLE",)
    assert risk.metrics.liquidity_fdv_ratio == Decimal("1")


def test_exactly_ten_percent_passes_on_rounded_figures(intent, market, context, now):
    """Rounding only ever understates the ratio, so a floor met is met."""
    rounded = _exact(_supply_raw(market, "100000000000000"), False, False)
    assert evaluate(intent, rounded, context, RiskLimits(), now=now).reason_codes == (
        "WITHIN_LIMITS",
    )


def test_a_ratio_in_bounds_lifts_no_other_blocker(intent, market, context, now):
    """Passing the ratio leaves every other refusal exactly where it was."""
    held = _exact(_concentrated(market))
    risk = evaluate(intent, held, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("HOLDERS_FAIL", "HOLDER_CONCENTRATION_LIMIT")


@pytest.mark.parametrize("value", [1, "true", "yes"])
def test_exactness_flags_are_strict(market, value):
    with pytest.raises(ValidationError):
        type(market).model_validate({**market.model_dump(), "price_exact": value})
    with pytest.raises(ValidationError):
        type(market.liquidity).model_validate(
            {**market.liquidity.model_dump(), "liquidity_exact": value}
        )


def test_a_snapshot_without_exactness_keeps_its_shape(market):
    dumped = market.model_dump(mode="json")
    assert "price_exact" not in dumped
    assert "liquidity_exact" not in dumped["liquidity"]


def test_a_sale_is_not_held_to_exactness(intent, market, context, now):
    sell, holding = _holding(intent, context)
    rounded = _exact(_supply_raw(market, "1"), False, False)
    assert evaluate(sell, rounded, holding, RiskLimits(), now=now).outcome == (RiskOutcome.APPROVE)


def test_stale_supply_evidence_is_refused_whatever_its_ratio(intent, market, context, now):
    """Supply travels with the holder evidence, and ages with it."""
    aged = _exact(market).model_copy(
        update={
            "holders": market.holders.model_copy(update={"created_at": now - timedelta(minutes=1)})
        }
    )
    risk = evaluate(intent, aged, context, RiskLimits(), now=now)
    assert risk.outcome == RiskOutcome.REJECT
    assert "STALE_OR_FUTURE_SAFETY_DATA" in risk.reason_codes


@pytest.mark.parametrize(
    "later,outcome",
    [
        # The pool has since grown into proportion with the token.
        ("10001", RiskOutcome.APPROVE),
        # It has not.
        ("10000000", RiskOutcome.REJECT),
    ],
)
def test_an_earlier_refusal_neither_carries_over_nor_is_reused(
    intent, market, context, now, later, outcome
):
    """SENTINEL holds no memory: each verdict is the figures in front of it.

    Whether a refused market may be asked again at all is COMMANDER's intake
    rule (`RISK_REJECTED_FOR_MARKET`), not this one.
    """
    first = evaluate(intent, _supply(market, "1"), context, RiskLimits(), now=now)
    assert first.reason_codes == ("LIQUIDITY_FDV_RATIO_UNVERIFIED",)
    again = evaluate(intent, _supply(market, later), context, RiskLimits(), now=now)
    assert again.outcome == outcome
    assert again.id != first.id
