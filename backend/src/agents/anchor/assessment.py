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
Capacity is monotone in intent — if the market cannot absorb a thousand dollars
it will not absorb ten — and continuing past a failure would spend provider
requests to learn nothing while inviting an interpolation nobody asked for.
"""

from datetime import datetime
from decimal import Decimal

from src.agents.anchor.models import (
    AnchorReasonCode,
    AnchorTaskInput,
    CapacitySemantics,
    ExecutionAssessment,
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
    notional: Decimal,
    quote: ExecutionQuote,
    reference: Decimal,
    now: datetime,
    policy: AnchorExecutionPolicy,
) -> QuotedPoint:
    """Judge one quote, or say precisely why it cannot be judged."""
    base = QuotedPoint(
        notional=notional,
        accepted=False,
        rejection=RejectionReason.NO_OUTPUT,
        amount_out=quote.amount_out,
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

    raw_price = quote.effective_price()
    effective = None if raw_price is None else quantize(raw_price)
    if effective is None:
        # The market offered nothing for the money. A real answer, not an error.
        return _reject(base, RejectionReason.NO_OUTPUT)
    deviation = execution_deviation_bps(effective, reference)
    priced = base.model_copy(
        update={"effective_price": effective, "execution_deviation_bps": deviation}
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
            points.append(QuotedPoint(notional=attempt.notional, accepted=False, rejection=reason))
            rejected_at = attempt.notional
            break

        quote = attempt.quote
        assert isinstance(quote, ExecutionQuote)
        problem = _identity_problem(quote, task_input)
        if problem is not None:
            points.append(
                QuotedPoint(
                    notional=attempt.notional,
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

        point = _point_from_quote(attempt.notional, quote, reference.price, now, policy)
        points.append(point)
        if point.accepted:
            supported = attempt.notional
            accepted_point = point
            continue
        rejected_at = attempt.notional
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
    return ExecutionAssessment(
        policy_version=task_input.policy_version,
        semantics=semantics,
        reason_code=reason,
        market_capacity_notional=capacity,
        first_rejected_notional=first_rejected if semantics == CapacitySemantics.BOUNDED else None,
        reference_price=reference,
        effective_price_at_capacity=None if accepted is None else accepted.effective_price,
        execution_deviation_bps_at_capacity=(
            None if accepted is None else accepted.execution_deviation_bps
        ),
        ladder=tuple(points),
        quote_requests=task_input.quote_requests,
        evaluated_at=now,
    )
