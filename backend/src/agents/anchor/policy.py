"""Versioned deterministic ANCHOR execution policy.

Everything here is an execution-integrity bound. None of it is a view on whether
a trade is worth making: there is no expected return, no sentiment, no technical
signal and no probability of anything. Those belong to VECTOR and to a future
FUSE, and duplicating them here would create a second opinion on questions that
must have exactly one.

Nor is any of it portfolio risk. Cash, exposure, position limits and daily loss
are SENTINEL's, and this module cannot see them. What it bounds is narrower and
entirely mechanical: whether a quote is recent enough to mean anything, whether
it describes the market we think it does, how far execution may stray from the
reference price before the quote stops being a sane offer, and how much provider
traffic one assessment may generate.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal


@dataclass(frozen=True)
class AnchorExecutionPolicy:
    version: str
    # Quotes age faster than anything else this system consumes. A setup
    # tolerates a five-minute-old picture and a trigger two minutes; an offer to
    # trade at a price is worth very little a minute after it was made, and
    # acting on a stale one is the failure mode that costs money rather than
    # opportunity.
    max_quote_age: timedelta
    # The independent price the quote is judged against must be current too. A
    # fresh quote compared to a stale reference produces a deviation figure that
    # describes the passage of time rather than the cost of trading.
    max_reference_age: timedelta
    # How far apart the ladder's own points may be. Several quotes taken minutes
    # apart do not describe one market state, and treating them as a single
    # curve would read the market moving as the market having depth.
    max_ladder_skew: timedelta
    # How far the effective execution price may sit from the reference before the
    # quote stops being a plausible offer. This is not a slippage tolerance and
    # not a view on what price is good: it is the bound past which a quote is
    # more likely to be wrong than expensive.
    max_execution_deviation_bps: Decimal
    # The provider's own impact figure, when it publishes one, held to its own
    # separate bound. Never merged with the deviation computed here.
    max_provider_price_impact_bps: Decimal
    # A route is allowed to split — the research for this phase found eight hops
    # across seven venues for a hundred thousand dollar order — but not without
    # limit, because a route nobody can follow is a route nobody can check.
    max_route_hops: int
    # The sizes to test, in units of the payment asset. Deliberately a fixed
    # ladder rather than a search: every point is auditable afterwards, and a
    # bounded list cannot become an unbounded probe of somebody's API.
    ladder_notional: tuple[Decimal, ...]
    # The stated ceiling on provider traffic for one assessment. The ladder must
    # fit inside it or this policy refuses to exist, which is what makes the
    # bound a fact about the configuration rather than a runtime branch.
    max_quote_requests: int

    def __post_init__(self) -> None:
        if self.max_quote_age <= timedelta(0):
            raise ValueError("Quote age tolerance must be positive")
        if self.max_reference_age <= timedelta(0):
            raise ValueError("Reference age tolerance must be positive")
        if self.max_ladder_skew <= timedelta(0):
            raise ValueError("Ladder skew tolerance must be positive")
        if self.max_ladder_skew > self.max_quote_age:
            # Points further apart than a quote may be old would let a ladder be
            # built entirely from quotes that are individually too stale to use.
            raise ValueError("Ladder skew cannot exceed the quote freshness window")
        if not Decimal(0) < self.max_execution_deviation_bps <= Decimal(10000):
            raise ValueError("Execution deviation bound must be a positive basis-point figure")
        if not Decimal(0) < self.max_provider_price_impact_bps <= Decimal(10000):
            raise ValueError("Provider impact bound must be a positive basis-point figure")
        if not 1 <= self.max_route_hops <= 64:
            raise ValueError("Route complexity must stay bounded")
        if not self.ladder_notional:
            raise ValueError("A ladder must test at least one size")
        if len(self.ladder_notional) > 12:
            raise ValueError("A ladder must stay small enough to audit")
        if any(step <= Decimal(0) for step in self.ladder_notional):
            raise ValueError("Every ladder step must be a positive notional")
        if list(self.ladder_notional) != sorted(self.ladder_notional):
            raise ValueError("Ladder steps must ascend")
        if len(set(self.ladder_notional)) != len(self.ladder_notional):
            raise ValueError("Ladder steps must be distinct")
        if self.max_quote_requests < len(self.ladder_notional):
            raise ValueError("The request budget must cover the ladder it is asked to test")
        if self.max_quote_requests > 32:
            raise ValueError("One assessment may not become a provider load test")


# Provisional PAPER-mode policy.
#
# The freshness numbers come from what execution actually is. Thirty seconds for
# a quote is short because an offer is perishable; ninety for the reference
# matches the cadence at which the market layer records observations, so the
# comparison is against the freshest reference that exists rather than one this
# policy wishes existed.
#
# The deviation bound is an integrity guard, not a trading preference. Research
# against the live aggregator on both supported chains found a hundred dollar
# order deviating two basis points from the reference and a million dollar order
# forty-seven, so a hundred basis points is far outside ordinary execution and
# comfortably inside "this quote is probably wrong". It refuses nothing a person
# would defend.
#
# The ladder spans four orders of magnitude in five points. It is wide because
# capacity is what is being measured and narrow in count because every point is
# a provider request and an audit line.
ANCHOR_EXECUTION_V1 = AnchorExecutionPolicy(
    version="anchor-execution-v1",
    max_quote_age=timedelta(seconds=30),
    max_reference_age=timedelta(seconds=90),
    max_ladder_skew=timedelta(seconds=20),
    max_execution_deviation_bps=Decimal(100),
    max_provider_price_impact_bps=Decimal(300),
    max_route_hops=16,
    ladder_notional=(
        Decimal(100),
        Decimal(500),
        Decimal(2500),
        Decimal(10000),
        Decimal(50000),
    ),
    max_quote_requests=8,
)
