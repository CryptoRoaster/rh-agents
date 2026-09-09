"""SENTINEL: deterministic, fail-closed policy. This module has no LLM dependencies."""

from datetime import datetime, timedelta
from decimal import Decimal

from src.core.models import (
    MarketSnapshot,
    RiskContext,
    RiskDecision,
    RiskLimits,
    RiskMetrics,
    RiskOutcome,
    SafetyStatus,
    Side,
    TradeIntent,
    TradingMode,
)
from src.core.numbers import quantize

BPS = Decimal("10000")


def evaluate(
    intent: TradeIntent,
    market: MarketSnapshot,
    context: RiskContext,
    limits: RiskLimits,
    *,
    now: datetime,
) -> RiskDecision:
    # Revalidate Python callers too: model_copy/model_construct can bypass validators.
    intent = TradeIntent.model_validate_json(intent.model_dump_json())
    market = MarketSnapshot.model_validate_json(market.model_dump_json())
    context = RiskContext.model_validate_json(context.model_dump_json())
    limits = RiskLimits.model_validate_json(limits.model_dump_json())
    reasons: list[str] = []
    pause = False
    notional = quantize(intent.quantity * market.price_usd)
    permitted_slippage = min(intent.max_slippage_bps, limits.max_slippage_bps)
    worst = quantize(notional * (1 + permitted_slippage / BPS))
    fee_rate = (market.fee_bps or Decimal("0")) / BPS
    worst_cost = quantize(worst * (1 + fee_rate))
    if limits.kill_switch:
        reasons.append("KILL_SWITCH")
        pause = True
    if intent.mode != TradingMode.PAPER:
        reasons.append("MODE_NOT_EXECUTABLE")
    if intent.asset_id != market.asset_id:
        reasons.append("ASSET_MISMATCH")
    age = (now - market.observed_at).total_seconds()
    if age < 0 or age > limits.max_snapshot_age_seconds:
        reasons.append("STALE_OR_FUTURE_MARKET")
    for snapshot in (market.token, market.liquidity, market.holders):
        nested_age = (now - snapshot.created_at).total_seconds()
        if nested_age < 0 or nested_age > limits.max_snapshot_age_seconds:
            reasons.append("STALE_OR_FUTURE_SAFETY_DATA")
            break
    timing = intent.timing
    if (
        timing.decision_at is None
        or not timing.detected_at <= timing.decision_at <= now
        or (now - timing.detected_at).total_seconds() > limits.max_snapshot_age_seconds
        or any((timing.tx_signed_at, timing.tx_sent_at, timing.tx_confirmed_at))
    ):
        reasons.append("INVALID_OR_STALE_INTENT_TIMING")
    checks = {
        "TOKEN": market.token.tradable,
        "ROUTING": market.liquidity.routing,
        "HOLDERS": market.holders.concentration_check,
        "ACCOUNTING": context.accounting,
    }
    for name, status in checks.items():
        if status != SafetyStatus.PASS:
            reasons.append(f"{name}_{status.value}")
    liquidity = market.liquidity.liquidity_usd
    slippage = market.liquidity.estimated_slippage_bps
    if market.holders.holder_count is None or market.holders.top_ten_fraction is None:
        reasons.append("HOLDER_METRICS_UNKNOWN")
    elif (
        market.holders.holder_count == 0
        or market.holders.top_ten_fraction > limits.max_top_ten_holder_fraction
    ):
        reasons.append("HOLDER_CONCENTRATION_LIMIT")
    if liquidity is None:
        reasons.append("LIQUIDITY_UNKNOWN")
    elif liquidity < limits.min_liquidity_usd:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    if slippage is None:
        reasons.append("SLIPPAGE_UNKNOWN")
    elif slippage > permitted_slippage or slippage >= BPS:
        reasons.append("SLIPPAGE_LIMIT")
    if market.fee_bps is None:
        reasons.append("FEES_UNKNOWN")
    elif market.fee_bps >= BPS:
        reasons.append("INVALID_FEES")
    if any(
        value is None
        for value in (
            context.cash_usd,
            context.exposure_usd,
            context.position_quantity,
            context.daily_loss_usd,
        )
    ):
        reasons.append("PORTFOLIO_DATA_UNKNOWN")
    if context.daily_loss_usd is not None and context.daily_loss_usd >= limits.daily_loss_limit_usd:
        reasons.append("DAILY_LOSS_LIMIT")
        pause = True
    if intent.side == Side.BUY:
        if context.cash_usd is not None and worst_cost > context.cash_usd:
            reasons.append("INSUFFICIENT_CASH")
        if context.exposure_usd is not None:
            if context.exposure_usd + worst_cost > limits.max_exposure_usd:
                reasons.append("MAX_EXPOSURE")
        if context.position_quantity is not None:
            if (
                context.position_quantity * market.price_usd + worst_cost
                > limits.max_position_size_usd
            ):
                reasons.append("MAX_POSITION_SIZE")
    elif context.position_quantity is not None and intent.quantity > context.position_quantity:
        reasons.append("INSUFFICIENT_POSITION")
    outcome = (
        RiskOutcome.PAUSE_SYSTEM
        if pause
        else (RiskOutcome.REJECT if reasons else RiskOutcome.APPROVE)
    )
    return RiskDecision(
        source="SENTINEL",
        correlation_id=intent.correlation_id,
        created_at=now,
        updated_at=now,
        intent_id=intent.id,
        intent_fingerprint=intent.fingerprint(),
        market_snapshot_id=market.id,
        market_fingerprint=market.fingerprint(),
        outcome=outcome,
        reason_codes=tuple(reasons) or ("WITHIN_LIMITS",),
        max_allowed_position_size_usd=limits.max_position_size_usd,
        max_slippage_bps=permitted_slippage,
        metrics=RiskMetrics(
            requested_notional_usd=notional,
            worst_case_notional_usd=worst_cost,
            exposure_usd=context.exposure_usd,
            daily_loss_usd=context.daily_loss_usd,
            liquidity_usd=liquidity,
            estimated_slippage_bps=slippage,
        ),
        evaluated_at=now,
        expires_at=now + timedelta(seconds=limits.approval_ttl_seconds),
    )
