"""The deterministic capacity assessment. No model, no provider, no judgement.

Given a ladder of quotes and one reference price, this decides which sizes the
market will support and reports the answer in terms that cannot over-claim. It
is a pure function: the same ladder always yields the same assessment, and
nothing it can be told changes what it concludes.

The ordering of checks is deliberate. Identity comes before economics, because a
quote for the wrong asset or the wrong chain is not an expensive quote — it is a
quote about something else, and measuring its deviation would produce a number
with no meaning. Freshness comes next, because a stale quote's price says what
the market *was*. Only then does the cost of trading matter.

The ladder is walked from the smallest size up and stops at the first rejection.
Continuing past a failure would spend provider requests on a curve nobody asked
for, and the conservative reading — that support was demonstrated up to the last
size that passed, and not beyond — needs no further evidence. That reading does
not claim the market's true maximum was found, nor that every untested size in
between behaves the same way; it claims only what was tested.

**Everything economic is compared in USD.** A quote's effective price is in
payment-asset units per unit bought; the reference is USD per unit bought. Those
are different quantities, and subtracting one from the other would produce a
figure measuring the payment asset's own price rather than the cost of trading.
The quote-asset valuation converts the first into the second before any
comparison happens, so the deviation means what its name says.
"""

from datetime import datetime
from decimal import Decimal

from src.agents.anchor.models import (
    AnchorReasonCode,
    AnchorTaskInput,
    CapacitySemantics,
    ExecutionAssessment,
    QuoteAttempt,
    QuotedPoint,
    RejectionReason,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, AnchorExecutionPolicy
from src.core.numbers import quantize
from src.markets.quotes import ExecutionQuote, QuoteFailure

BPS = Decimal(10000)

# How a provider's stated market fact becomes a rejection of one ladder point.
# Only these two describe the market; every other failure is an absence of
# evidence and stops the assessment rather than counting against a size.
MARKET_REJECTIONS: dict[QuoteFailure, RejectionReason] = {
    QuoteFailure.NO_ROUTE: RejectionReason.NO_ROUTE,
    QuoteFailure.INSUFFICIENT_LIQUIDITY: RejectionReason.INSUFFICIENT_LIQUIDITY,
}


def valuation_skew_bps(ours: Decimal, theirs: Decimal) -> Decimal:
    """Absolute disagreement between two USD valuations of the same order."""
    if ours <= 0:
        raise ValueError("An intended notional must be positive to compare against")
    return quantize(abs(theirs - ours) / ours * BPS)


def execution_deviation_bps(effective: Decimal, reference: Decimal) -> Decimal:
    """How far the quoted execution price sits from the current reference.

    Signed, and positive means worse for a buyer. This is deliberately *not*
    called slippage: it mixes depth, fees, spread and the gap between two
    readings, and no part of it is a prediction of what a future trade would
    realise. Naming it honestly is the point.
    """
    if reference <= 0:
        raise ValueError("A reference price must be positive to compare against")
    # Quantized to the storage scale used everywhere else, so a derived figure
    # never carries more precision than the system can record or compare.
    return quantize((effective - reference) / reference * BPS)


def _reject(point: "QuotedPoint", reason: RejectionReason) -> QuotedPoint:
    return point.model_copy(update={"accepted": False, "rejection": reason})


def _point_from_quote(
    attempt: QuoteAttempt,
    quote: ExecutionQuote,
    reference: Decimal,
    usd_per_token: Decimal,
    now: datetime,
    policy: AnchorExecutionPolicy,
) -> QuotedPoint:
    """Judge one quote, or say precisely why it cannot be judged."""
    base = QuotedPoint(
        notional_usd=attempt.notional_usd,
        amount_in_tokens=attempt.amount_in_tokens,
        accepted=False,
        rejection=RejectionReason.NO_OUTPUT,
        amount_out=quote.amount_out,
        provider_amount_in_usd=quote.provider_amount_in_usd,
        route_hops=quote.route.hop_count,
        venues=tuple(sorted(quote.route.venues))[:32],
        quoted_at=quote.quoted_at,
        source_block_number=quote.source_block_number,
        provider_price_impact_bps=quote.provider_price_impact_bps,
    )
    age = quote.age(now)
    if age < -policy.max_ladder_skew:
        return _reject(base, RejectionReason.QUOTE_IN_FUTURE)
    if age > policy.max_quote_age:
        return _reject(base, RejectionReason.QUOTE_TOO_STALE)
    if quote.route.hop_count > policy.max_route_hops:
        return _reject(base, RejectionReason.ROUTE_TOO_COMPLEX)

    # Our valuation of the order against the provider's own, when it published
    # one. They should agree closely; a material gap means one of us is wrong
    # about decimals, about which token this is, or about what it costs — and a
    # quote whose size we cannot agree on has no readable economics.
    theirs = quote.provider_amount_in_usd
    if theirs is not None:
        skew = valuation_skew_bps(attempt.notional_usd, theirs)
        if skew > policy.max_usd_valuation_skew_bps:
            return _reject(base, RejectionReason.USD_VALUATION_DISAGREEMENT)

    raw_price = quote.effective_price()
    if raw_price is None:
        # The market offered nothing for the money. A real answer, not an error.
        return _reject(base, RejectionReason.NO_OUTPUT)
    # Payment-asset units per unit bought, converted into USD per unit bought so
    # the comparison below is between two of the same kind of number.
    effective = quantize(raw_price * usd_per_token)
    if effective <= 0:
        return _reject(base, RejectionReason.NO_OUTPUT)
    deviation = execution_deviation_bps(effective, reference)
    priced = base.model_copy(
        update={"effective_price_usd": effective, "execution_deviation_bps": deviation}
    )
    if deviation > policy.max_execution_deviation_bps:
        return _reject(priced, RejectionReason.EXECUTION_DEVIATION_TOO_HIGH)
    impact = quote.provider_price_impact_bps
    if impact is not None and impact > policy.max_provider_price_impact_bps:
        # The provider's own figure, held to its own bound. Never merged with the
        # deviation above, because the two measure different things.
        return _reject(priced, RejectionReason.PROVIDER_IMPACT_TOO_HIGH)
    return priced.model_copy(update={"accepted": True, "rejection": None})


def _identity_problem(quote: ExecutionQuote, task_input: AnchorTaskInput) -> RejectionReason | None:
    market = task_input.market
    if quote.chain != market.chain or quote.network != market.network:
        return RejectionReason.CHAIN_MISMATCH
    if quote.token_in != market.quote_token or quote.token_out != market.base_token:
        # Buying the wrong asset, or paying with one. The most expensive
        # possible mistake to miss, so it is checked before anything economic.
        return RejectionReason.ASSET_MISMATCH
    if (
        quote.token_in_decimals != market.quote_decimals
        or quote.token_out_decimals != market.base_decimals
    ):
        return RejectionReason.ASSET_MISMATCH
    return None


def _ladder_coherent(points: list[QuotedPoint], policy: AnchorExecutionPolicy) -> bool:
    """Whether the quoted points plausibly describe one market state.

    Quotes taken far enough apart are separate observations of a moving market,
    and reading them as a single depth curve would mistake the market moving for
    the market having depth.
    """
    stamps = [point.quoted_at for point in points if point.quoted_at is not None]
    if len(stamps) < 2:
        return True
    return max(stamps) - min(stamps) <= policy.max_ladder_skew


def assess(
    task_input: AnchorTaskInput,
    now: datetime,
    policy: AnchorExecutionPolicy = ANCHOR_EXECUTION_V1,
) -> ExecutionAssessment | AnchorReasonCode:
    """Decide what the current executable market supports.

    Returns an assessment, or a bare reason code when no assessment can honestly
    be made — the caller turns the latter into a retry rather than into evidence
    that the market is empty.
    """
    reference = task_input.reference
    if reference is None:
        return AnchorReasonCode.REFERENCE_UNAVAILABLE
    if reference.age_seconds > policy.max_reference_age.total_seconds():
        # A fresh quote against a stale reference yields a deviation that
        # measures elapsed time rather than the cost of trading.
        return AnchorReasonCode.REFERENCE_TOO_STALE
    valuation = task_input.quote_asset_valuation
    if valuation is None:
        # Without it nothing here is denominated in anything, so nothing here
        # can be compared or reported.
        return AnchorReasonCode.QUOTE_ASSET_USD_VALUE_UNAVAILABLE
    if valuation.age_seconds > policy.max_reference_age.total_seconds():
        return AnchorReasonCode.QUOTE_ASSET_USD_VALUE_UNAVAILABLE
    if not task_input.ladder:
        return AnchorReasonCode.QUOTES_UNAVAILABLE

    points: list[QuotedPoint] = []
    supported: Decimal | None = None
    rejected_at: Decimal | None = None
    accepted_point: QuotedPoint | None = None

    for attempt in task_input.ladder:
        if attempt.failure is not None:
            reason = MARKET_REJECTIONS.get(attempt.failure)
            if reason is None:
                # Not a statement about the market. Nothing here can distinguish
                # a rate limit from an empty order book, so nothing here tries.
                return AnchorReasonCode.QUOTES_UNAVAILABLE
            points.append(
                QuotedPoint(
                    notional_usd=attempt.notional_usd,
                    amount_in_tokens=attempt.amount_in_tokens,
                    accepted=False,
                    rejection=reason,
                )
            )
            rejected_at = attempt.notional_usd
            break

        quote = attempt.quote
        assert isinstance(quote, ExecutionQuote)
        problem = _identity_problem(quote, task_input)
        if problem is not None:
            points.append(
                QuotedPoint(
                    notional_usd=attempt.notional_usd,
                    amount_in_tokens=attempt.amount_in_tokens,
                    accepted=False,
                    rejection=problem,
                    amount_out=quote.amount_out,
                    route_hops=quote.route.hop_count,
                    quoted_at=quote.quoted_at,
                )
            )
            # A quote about the wrong market says nothing about this one, so the
            # ladder is abandoned rather than bracketed.
            return _unusable(
                task_input, reference.price, points, now, AnchorReasonCode.LADDER_INCOHERENT
            )

        point = _point_from_quote(
            attempt, quote, reference.price, valuation.usd_per_token, now, policy
        )
        points.append(point)
        if point.accepted:
            supported = attempt.notional_usd
            accepted_point = point
            continue
        rejected_at = attempt.notional_usd
        break

    if not _ladder_coherent(points, policy):
        return _unusable(
            task_input, reference.price, points, now, AnchorReasonCode.LADDER_INCOHERENT
        )

    if supported is None:
        # Even the smallest tested size failed on its merits. The market is
        # observable and will not support this.
        empty_reason: AnchorReasonCode = (
            AnchorReasonCode.NO_ROUTE
            if points and points[0].rejection == RejectionReason.NO_ROUTE
            else AnchorReasonCode.NO_EXECUTABLE_CAPACITY
        )
        return _assessment(
            task_input,
            reference.price,
            points,
            now,
            CapacitySemantics.NONE,
            empty_reason,
            capacity=None,
            first_rejected=rejected_at,
        )

    if rejected_at is None:
        # Every size tried passed. What has been learned is a floor, and saying
        # so is the difference between a measurement and a guess.
        return _assessment(
            task_input,
            reference.price,
            points,
            now,
            CapacitySemantics.AT_LEAST,
            AnchorReasonCode.CAPACITY_AT_LEAST_TESTED_CEILING,
            capacity=supported,
            first_rejected=None,
            accepted=accepted_point,
        )
    return _assessment(
        task_input,
        reference.price,
        points,
        now,
        CapacitySemantics.BOUNDED,
        AnchorReasonCode.CAPACITY_BRACKETED,
        capacity=supported,
        first_rejected=rejected_at,
        accepted=accepted_point,
    )


def _unusable(
    task_input: AnchorTaskInput,
    reference: Decimal,
    points: list[QuotedPoint],
    now: datetime,
    reason: AnchorReasonCode,
) -> ExecutionAssessment:
    return _assessment(
        task_input, reference, points, now, CapacitySemantics.UNKNOWN, reason, capacity=None
    )


def _assessment(
    task_input: AnchorTaskInput,
    reference: Decimal,
    points: list[QuotedPoint],
    now: datetime,
    semantics: CapacitySemantics,
    reason: AnchorReasonCode,
    *,
    capacity: Decimal | None,
    first_rejected: Decimal | None = None,
    accepted: QuotedPoint | None = None,
) -> ExecutionAssessment:
    valuation = task_input.quote_asset_valuation
    assert valuation is not None
    return ExecutionAssessment(
        policy_version=task_input.policy_version,
        semantics=semantics,
        reason_code=reason,
        largest_tested_acceptable_notional_usd=capacity,
        first_tested_rejected_notional_usd=(
            first_rejected if semantics == CapacitySemantics.BOUNDED else None
        ),
        reference_price=reference,
        quote_asset_usd_price=valuation.usd_per_token,
        effective_price_usd_at_capacity=None if accepted is None else accepted.effective_price_usd,
        execution_deviation_bps_at_capacity=(
            None if accepted is None else accepted.execution_deviation_bps
        ),
        ladder=tuple(points),
        quote_requests=task_input.quote_requests,
        evaluated_at=now,
    )
