"""Whether enough market structure exists to ask for a setup at all.

This is the gate the phase was missing. Before it, VECTOR was shown one observed
price and returned an entry, an invalidation and two targets; the structural
validator confirmed they were ordered correctly and the evidence was recorded as
AVAILABLE and ACCEPTED. Nothing had checked whether the input could support the
output, because nothing was responsible for asking.

Three properties matter about where this check lives.

**It is deterministic.** The verdict is arithmetic over counts and timestamps. No
model participates, and nothing a model returns can change it.

**It runs before the provider call.** A market that cannot support a setup costs
no reasoning request, and — more importantly — is never given the chance to
produce one that would have to be caught afterwards, having already been paid for
and having already been written down as a proposal.

**The model does not get a vote.** VECTOR is never asked whether its input was
adequate. A model asked to judge the sufficiency of its own evidence will answer
in the direction of having an answer, and the one situation this gate exists for
is precisely the one where it would be least reliable.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from src.agents.vector.policy import VECTOR_SETUP_V1, VectorSetupPolicy
from src.markets.history import USD_PER_BASE_UNIT, MarketHistory
from src.markets.models import MarketIdentity


class VectorMarketDataSufficiency(StrEnum):
    """The verdict, and every distinct way of failing it.

    The failures are kept apart because they mean different things to the
    runtime. A thin or young market may become sufficient on its own and is worth
    retrying; a series for the wrong pool or in the wrong unit is a wiring fault
    that no amount of retrying will fix.
    """

    SUFFICIENT = "SUFFICIENT"
    # Recoverable: the market may simply not have traded enough yet.
    MARKET_HISTORY_EMPTY = "MARKET_HISTORY_EMPTY"
    MARKET_HISTORY_TOO_SHORT = "MARKET_HISTORY_TOO_SHORT"
    MARKET_HISTORY_TOO_STALE = "MARKET_HISTORY_TOO_STALE"
    MARKET_HISTORY_TOO_GAPPED = "MARKET_HISTORY_TOO_GAPPED"
    # Not recoverable by retrying: the series does not describe this market.
    MARKET_HISTORY_IDENTITY_MISMATCH = "MARKET_HISTORY_IDENTITY_MISMATCH"
    MARKET_HISTORY_PRICE_BASIS_MISMATCH = "MARKET_HISTORY_PRICE_BASIS_MISMATCH"
    MARKET_HISTORY_TIMEFRAME_MISMATCH = "MARKET_HISTORY_TIMEFRAME_MISMATCH"
    MARKET_HISTORY_IN_FUTURE = "MARKET_HISTORY_IN_FUTURE"


# Which failures a later attempt could plausibly resolve. Used by the handler to
# categorize the task outcome, and stated here so the distinction lives with the
# verdicts rather than being re-derived somewhere it could drift.
RECOVERABLE = frozenset(
    {
        VectorMarketDataSufficiency.MARKET_HISTORY_EMPTY,
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_SHORT,
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_STALE,
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_GAPPED,
    }
)


def assess(
    history: MarketHistory,
    identity: MarketIdentity,
    now: datetime,
    policy: VectorSetupPolicy = VECTOR_SETUP_V1,
) -> VectorMarketDataSufficiency:
    """Decide whether this series can support a setup for this market.

    Order is deliberate: identity and units first, because a series describing
    another market or another unit is not a thin series — it is the wrong series,
    and reporting it as "too short" would send the runtime off retrying a fault
    that retrying cannot reach.
    """
    verdict = VectorMarketDataSufficiency
    if (
        history.pair_id != identity.pair_id
        or history.chain != identity.chain
        or history.network != identity.network
        or history.base_asset_id != identity.base_asset_id
        or history.quote_asset_id != identity.quote_asset_id
    ):
        return verdict.MARKET_HISTORY_IDENTITY_MISMATCH
    if history.price_basis != USD_PER_BASE_UNIT:
        # Every level VECTOR proposes is USD per base unit. A series in any other
        # orientation would invert comparisons while still looking like numbers.
        return verdict.MARKET_HISTORY_PRICE_BASIS_MISMATCH
    if (
        history.timeframe != policy.history_timeframe
        or history.aggregate != policy.history_aggregate
    ):
        # The policy binds the timeframe to the permitted setup horizon, so a
        # series on a different one is not the structure that binding assumed.
        return verdict.MARKET_HISTORY_TIMEFRAME_MISMATCH

    if not history.bars:
        return verdict.MARKET_HISTORY_EMPTY
    if len(history.bars) < policy.min_closed_bars:
        return verdict.MARKET_HISTORY_TOO_SHORT

    age = history.age(now)
    assert age is not None  # guaranteed by the emptiness check above
    if age.total_seconds() < 0:
        return verdict.MARKET_HISTORY_IN_FUTURE
    if age > policy.max_history_age:
        # Measured from when the newest bar closed. A series retrieved a moment
        # ago that ends four hours back is four hours old, whatever the fetch
        # timestamp says.
        return verdict.MARKET_HISTORY_TOO_STALE

    span = len(history.bars) + history.missing_intervals
    if Decimal(history.missing_intervals) / Decimal(span) > policy.max_missing_fraction:
        return verdict.MARKET_HISTORY_TOO_GAPPED
    return verdict.SUFFICIENT


def grounding_band(
    range_low: Decimal, range_high: Decimal, reference: Decimal, policy: VectorSetupPolicy
) -> tuple[Decimal, Decimal]:
    """The band a proposed level must fall inside to count as grounded.

    The observed range, widened by a multiple of itself. Wide enough that a
    breakout above every recorded high and an invalidation below every recorded
    low both remain proposable — refusing those would make the whole setup
    vocabulary unusable — and narrow enough that a level with no relationship to
    anything observed is refused.

    The floor matters in a market that has barely moved. A range of nearly zero
    would otherwise collapse the band onto a point and reject every level
    including the sensible ones, so a small fraction of the reference price keeps
    it open.
    """
    observed = range_high - range_low
    width = max(observed * policy.range_extension, reference * policy.flat_market_floor)
    return range_low - width, range_high + width
