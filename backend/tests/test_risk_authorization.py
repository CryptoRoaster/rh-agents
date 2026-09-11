from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.models import (
    RiskDecision,
    RiskLimits,
    RiskMetrics,
    RiskOutcome,
    SafetyStatus,
)
from src.risk.authorization import (
    RESIZABLE_SIZING_REASON_CODES,
    RiskAuthorization,
    classify_decision,
    classify_risk_authorization,
)
from src.risk.engine import evaluate

# Every reason code src.risk.engine can emit that is not a resizable sizing
# shortfall. The nested safety checks expand over SafetyStatus, so build those
# from the enum instead of transcribing them.
NON_SIZING_REASON_CODES = [
    "KILL_SWITCH",
    "MODE_NOT_EXECUTABLE",
    "ASSET_MISMATCH",
    "STALE_OR_FUTURE_MARKET",
    "STALE_OR_FUTURE_SAFETY_DATA",
    "INVALID_OR_STALE_INTENT_TIMING",
    "HOLDER_METRICS_UNKNOWN",
    "HOLDER_CONCENTRATION_LIMIT",
    "LIQUIDITY_UNKNOWN",
    "INSUFFICIENT_LIQUIDITY",
    "SLIPPAGE_UNKNOWN",
    "SLIPPAGE_LIMIT",
    "FEES_UNKNOWN",
    "INVALID_FEES",
    "PORTFOLIO_DATA_UNKNOWN",
    "DAILY_LOSS_LIMIT",
    "INSUFFICIENT_POSITION",
    "WITHIN_LIMITS",
] + [
    f"{check}_{status.value}"
    for check in ("TOKEN", "ROUTING", "HOLDERS", "ACCOUNTING")
    for status in SafetyStatus
    if status != SafetyStatus.PASS
]


def decision(
    now,
    trace,
    outcome=RiskOutcome.REJECT,
    reason_codes=("MAX_POSITION_SIZE",),
    *,
    capacity="150",
    position_limit="2500",
    slippage="100",
):
    return RiskDecision(
        source="SENTINEL",
        correlation_id=trace,
        created_at=now,
        updated_at=now,
        intent_id=uuid4(),
        intent_fingerprint="intent",
        market_snapshot_id=uuid4(),
        market_fingerprint="market",
        outcome=outcome,
        reason_codes=reason_codes,
        position_size_limit_usd=Decimal(position_limit),
        max_additional_notional_usd=Decimal(capacity),
        max_slippage_bps=Decimal(slippage),
        metrics=RiskMetrics(
            requested_notional_usd=Decimal("200"),
            worst_case_notional_usd=Decimal("202.202"),
            exposure_usd=Decimal("0"),
            daily_loss_usd=Decimal("0"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
        ),
        evaluated_at=now,
        expires_at=now + timedelta(minutes=1),
    )


def test_approve_is_the_only_path_to_approved(now, trace):
    approved = decision(now, trace, RiskOutcome.APPROVE, ("WITHIN_LIMITS",), capacity="900")
    assert classify_decision(approved) == RiskAuthorization.APPROVED


@pytest.mark.parametrize("capacity", ["0", "150", "999999"])
@pytest.mark.parametrize("reasons", [("KILL_SWITCH",), ("DAILY_LOSS_LIMIT",), ("MAX_EXPOSURE",)])
def test_pause_system_never_becomes_limited(now, trace, capacity, reasons):
    # A pause must fail closed even if a future risk engine leaves sizing
    # guidance populated and every reason code looks resizable.
    paused = decision(now, trace, RiskOutcome.PAUSE_SYSTEM, reasons, capacity=capacity)
    assert classify_decision(paused) == RiskAuthorization.REJECTED


def test_unhandled_future_outcome_fails_closed():
    assert (
        classify_risk_authorization(
            cast(RiskOutcome, "FUTURE_OUTCOME"),
            ("MAX_POSITION_SIZE",),
            position_size_limit_usd=Decimal("2500"),
            max_additional_notional_usd=Decimal("150"),
        )
        == RiskAuthorization.REJECTED
    )


@pytest.mark.parametrize("code", sorted(RESIZABLE_SIZING_REASON_CODES))
def test_single_resizable_sizing_rejection_with_capacity_is_limited(now, trace, code):
    assert (
        classify_decision(decision(now, trace, reason_codes=(code,))) == RiskAuthorization.LIMITED
    )


def test_combined_resizable_sizing_rejections_are_limited(now, trace):
    every = tuple(sorted(RESIZABLE_SIZING_REASON_CODES))
    assert classify_decision(decision(now, trace, reason_codes=every)) == RiskAuthorization.LIMITED


@pytest.mark.parametrize("blocker", NON_SIZING_REASON_CODES)
def test_any_non_sizing_reason_rejects_even_beside_sizing_and_capacity(now, trace, blocker):
    assert (
        classify_decision(decision(now, trace, reason_codes=("MAX_POSITION_SIZE", blocker)))
        == RiskAuthorization.REJECTED
    )
    assert (
        classify_decision(decision(now, trace, reason_codes=(blocker,)))
        == RiskAuthorization.REJECTED
    )


def test_unrecognised_reason_code_rejects(now, trace):
    assert (
        classify_decision(decision(now, trace, reason_codes=("SOME_FUTURE_RISK_RULE",)))
        == RiskAuthorization.REJECTED
    )


@pytest.mark.parametrize("capacity,position_limit", [("0", "2500"), ("150", "0"), ("0", "0")])
def test_limit_requires_both_caps_strictly_positive(now, trace, capacity, position_limit):
    assert (
        classify_decision(decision(now, trace, capacity=capacity, position_limit=position_limit))
        == RiskAuthorization.REJECTED
    )


def test_empty_reason_codes_cannot_be_constructed_or_classified(now, trace):
    with pytest.raises(ValidationError):
        decision(now, trace, reason_codes=())
    assert (
        classify_risk_authorization(
            RiskOutcome.REJECT,
            (),
            position_size_limit_usd=Decimal("2500"),
            max_additional_notional_usd=Decimal("150"),
        )
        == RiskAuthorization.REJECTED
    )


@pytest.mark.parametrize("capacity", ["-1", "NaN", "Infinity"])
def test_invalid_capacity_fails_model_validation(now, trace, capacity):
    with pytest.raises(ValidationError):
        decision(now, trace, capacity=capacity)


def test_real_engine_sizing_only_rejection_classifies_limited(intent, market, context, now):
    risk = evaluate(
        intent, market, context, RiskLimits(max_position_size_usd=Decimal("200")), now=now
    )
    assert risk.outcome == RiskOutcome.REJECT
    assert risk.reason_codes == ("MAX_POSITION_SIZE",)
    assert risk.max_additional_notional_usd > 0
    assert classify_decision(risk) == RiskAuthorization.LIMITED


def test_real_engine_mixed_rejection_classifies_rejected(intent, market, context, now):
    risk = evaluate(
        intent,
        market,
        context,
        RiskLimits(max_position_size_usd=Decimal("200"), min_liquidity_usd=Decimal("2000000")),
        now=now,
    )
    assert risk.outcome == RiskOutcome.REJECT
    assert "MAX_POSITION_SIZE" in risk.reason_codes
    assert "INSUFFICIENT_LIQUIDITY" in risk.reason_codes
    assert classify_decision(risk) == RiskAuthorization.REJECTED


def test_real_engine_pause_classifies_rejected(intent, market, context, now):
    risk = evaluate(intent, market, context, RiskLimits(kill_switch=True), now=now)
    assert risk.outcome == RiskOutcome.PAUSE_SYSTEM
    assert classify_decision(risk) == RiskAuthorization.REJECTED
