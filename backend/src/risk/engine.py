"""SENTINEL: deterministic, fail-closed policy. This module has no LLM dependencies."""

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext

from src.core.models import (
    LP_CONTROL_V4_POOL_CENSUS,
    LpControlStatus,
    MarketSnapshot,
    Record,
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
from src.core.numbers import LEDGER_UNIT, quantize

BPS = Decimal("10000")
# The order safety checks report in, unchanged from when they were one mapping.
ORDER = {"TOKEN": 0, "ROUTING": 1, "HOLDERS": 2, "ACCOUNTING": 3}
# The reason a BUY refused at the daily loss limit carries, and the one its
# caller turns into the automatic entry halt.
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
RATIO_QUANTUM = Decimal("0.000000000000000001")
# Enough digits that a 78-digit supply times a ledger price is exact.
EXACT_PRECISION = 160


def total_supply(market: MarketSnapshot) -> Decimal | None:
    """The token's on-chain total supply in whole tokens, or unknown.

    Supply and decimals are ATLAS's contract read, never a provider's market
    cap; a zero supply values nothing and is as unknown as a missing one.
    Whether the contract's decimals agree with the provider's is the caller's
    question (`FDV_DECIMALS_MISMATCH`).
    """
    raw = market.token.total_supply_raw
    decimals = market.token.total_supply_decimals
    if raw is None or decimals is None or int(raw) == 0:
        return None
    with localcontext() as arithmetic:
        # A 78-digit supply survives scaling only above the default precision.
        arithmetic.prec = EXACT_PRECISION
        return Decimal(int(raw)).scaleb(-decimals)


def lp_control_refusal(market: MarketSnapshot) -> str | None:
    """Why a purchase in this market is not covered by LP-control evidence.

    The one fact a BUY needs beyond the holder figures: who can withdraw the
    traded pool's liquidity. Holder evidence, however positive, does not stand
    in for it. Absent evidence is UNKNOWN. A PASS counts only when it names the
    one verified basis there is -- the complete V4 pool census of the market's
    own 32-byte pool, at a census block; any other PASS is not evidence.
    """
    control = market.lp_control
    if control is None:
        return "LP_CONTROL_UNKNOWN"
    if control.status != LpControlStatus.PASS:
        return f"LP_CONTROL_{control.status.value}"
    if (
        control.pool_locator_kind != "BYTES32_POOL_ID"
        or control.pool_id is None
        or control.basis != LP_CONTROL_V4_POOL_CENSUS
        or control.census_block is None
    ):
        return "LP_CONTROL_EVIDENCE_INCONSISTENT"
    return None


def _additional_buy_notional(
    context: RiskContext,
    market: MarketSnapshot,
    limits: RiskLimits,
    permitted_slippage: Decimal,
) -> Decimal:
    if (
        context.cash_usd is None
        or context.exposure_usd is None
        or context.position_quantity is None
        or market.fee_bps is None
    ):
        return Decimal("0")
    with localcontext() as arithmetic:
        arithmetic.prec = 78
        budget = max(
            Decimal("0"),
            min(
                context.cash_usd,
                limits.max_exposure_usd - context.exposure_usd,
                limits.max_position_size_usd - context.position_quantity * market.price_usd,
            ),
        )
        slippage_factor = 1 + permitted_slippage / BPS
        fee_factor = 1 + market.fee_bps / BPS
        quantum = Decimal("0.000000000000000001")
        capacity = (budget / (slippage_factor * fee_factor)).quantize(quantum, rounding=ROUND_DOWN)
        # Final validation rounds slippage and total cost separately. Keep the
        # guidance conservative even when those intermediate roundings round up.
        while capacity > 0 and quantize(quantize(capacity * slippage_factor) * fee_factor) > budget:
            capacity -= quantum
        return capacity


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
    nested: tuple[Record, ...] = (market.token, market.liquidity, market.holders)
    if intent.side == Side.BUY and market.lp_control is not None:
        # LP-control evidence binds a purchase only, its age included.
        nested = (*nested, market.lp_control)
    for snapshot in nested:
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
    # A purchase is judged on whether the market is good enough to enter; a sale
    # only on whether the holding can safely be sold there. The entry-quality
    # bars — holder concentration and minimum pool liquidity — therefore bind
    # BUY alone: a concentrated or thinning market is a reason to leave it, not
    # a reason to stay. Everything that says whether a sale is possible and
    # correctly valued binds both, and every UNKNOWN still fails closed.
    buying = intent.side == Side.BUY
    checks = {
        "TOKEN": market.token.tradable,
        "ROUTING": market.liquidity.routing,
        "ACCOUNTING": context.accounting,
    }
    if buying or market.holders.concentration_check == SafetyStatus.UNKNOWN:
        # For a sale only an unestablished holder domain vetoes; a measured
        # concentration FAIL is an entry verdict.
        checks = {**checks, "HOLDERS": market.holders.concentration_check}
    for name, status in sorted(checks.items(), key=lambda item: ORDER[item[0]]):
        if status != SafetyStatus.PASS:
            reasons.append(f"{name}_{status.value}")
    if buying:
        # Issue #92. A purchase needs the traded pool's liquidity control
        # established; a sale never does, so an exit is never trapped by it.
        lp_refusal = lp_control_refusal(market)
        if lp_refusal is not None:
            reasons.append(lp_refusal)
    liquidity = market.liquidity.liquidity_usd
    slippage = market.liquidity.estimated_slippage_bps
    if market.holders.holder_count is None or market.holders.top_ten_fraction is None:
        reasons.append("HOLDER_METRICS_UNKNOWN")
    elif buying and (
        market.holders.holder_count == 0
        or market.holders.top_ten_fraction > limits.max_top_ten_holder_fraction
    ):
        reasons.append("HOLDER_CONCENTRATION_LIMIT")
    if liquidity is None:
        reasons.append("LIQUIDITY_UNKNOWN")
    elif (liquidity < limits.min_liquidity_usd) if buying else (liquidity <= 0):
        # A sale needs a pool that exists, not one deep enough to enter.
        reasons.append("INSUFFICIENT_LIQUIDITY")
    # How deep the pool is against what the whole token is valued at: a pool
    # thin for its valuation cannot absorb an exit, however large it is in
    # dollars. An entry bar, so a purchase alone is judged on it. Liquidity
    # counts both sides of the pool, so above the FDV it is either a launch
    # whose pool still holds nearly the whole supply or wrong data. Neither
    # can be told apart here: the evidence is unverified, not a finding
    # against the token, and it is not bought on.
    fdv_ratio: Decimal | None = None
    if buying:
        supply = total_supply(market)
        if supply is None:
            reasons.append("FDV_UNKNOWN")
        elif market.token.total_supply_decimals != market.token.decimals:
            # The contract and the provider disagree on the token's scale, so
            # the price and the supply are not in the same unit.
            reasons.append("FDV_DECIMALS_MISMATCH")
        elif liquidity is not None:
            with localcontext() as arithmetic:
                arithmetic.prec = EXACT_PRECISION
                # A purchase is priced by a ceiling, up to one ledger unit
                # above the recorded price. The floor is judged at that price,
                # where the FDV can only come out larger; the ceiling at the
                # lowest price it can stand for, where the FDV can only come
                # out smaller. Each errs toward refusal.
                fdv = supply * market.price_usd
                if liquidity > supply * (market.price_usd - LEDGER_UNIT):
                    reasons.append("LIQUIDITY_FDV_RATIO_UNVERIFIED")
                elif liquidity < limits.min_liquidity_fdv_ratio * fdv:
                    reasons.append("LIQUIDITY_FDV_RATIO_LIMIT")
                fdv_ratio = (liquidity / fdv).quantize(RATIO_QUANTUM, rounding=ROUND_DOWN)
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
    # The daily loss limit binds a purchase, never a sale. A sale reduces risk
    # and is exactly what a loss-limited book still needs to be able to do;
    # every check that says whether a sale is possible and correctly valued
    # still binds it above. For a purchase it is a refusal, not a system-wide
    # stop: the caller records it as the persistent automatic entry halt
    # (`src.orchestration.entryhalt`), which stops every further BUY and no
    # SELL. Only the kill switch pauses the system.
    if (
        buying
        and context.daily_loss_usd is not None
        and context.daily_loss_usd >= limits.daily_loss_limit_usd
    ):
        reasons.append(DAILY_LOSS_LIMIT)
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
    capacity = Decimal("0")
    sizing_reasons = {"INSUFFICIENT_CASH", "MAX_EXPOSURE", "MAX_POSITION_SIZE"}
    if intent.side == Side.BUY and not (set(reasons) - sizing_reasons):
        capacity = _additional_buy_notional(context, market, limits, permitted_slippage)
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
        position_size_limit_usd=limits.max_position_size_usd,
        max_additional_notional_usd=capacity,
        max_slippage_bps=permitted_slippage,
        metrics=RiskMetrics(
            requested_notional_usd=notional,
            worst_case_notional_usd=worst_cost,
            exposure_usd=context.exposure_usd,
            daily_loss_usd=context.daily_loss_usd,
            liquidity_usd=liquidity,
            estimated_slippage_bps=slippage,
            liquidity_fdv_ratio=fdv_ratio,
        ),
        evaluated_at=now,
        expires_at=now + timedelta(seconds=limits.approval_ttl_seconds),
    )
