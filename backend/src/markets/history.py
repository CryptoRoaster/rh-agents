"""Recorded market structure: bounded series of closed bars for one pool.

This module exists because of a defect found auditing Phase 2H. VECTOR could
propose an entry at 1.10, an invalidation at 0.92 and targets at 1.25 and 1.45
having been shown exactly one number — an observed price of 1.00. The structural
validator confirmed the geometry held together and the evidence was recorded as
AVAILABLE and ACCEPTED. Nothing in the supplied data said 1.10 was resistance or
that 0.92 was support, because nothing in the supplied data said anything about
either level at all. Plausible geometry is not a supported trade setup.

What is modelled here is deliberately narrow. These are **pool-specific DEX
bars** from one provider, not exchange-wide market history, and they are facts
rather than conclusions: no indicator is computed, no trend is named, no level is
selected. The series exists so that a setup can be *grounded* in observed
structure, and so that the system can decide — deterministically, before any
model is asked — whether enough structure exists to ask at all.

Three distinctions are load-bearing:

* **A bar is not a price.** The current price is the latest snapshot; the bars
  are closed history. One never overwrites the other.
* **A closed bar is not a forming one.** The provider returns the in-progress
  interval alongside the closed ones and does not mark it, so the adapter drops
  it rather than presenting a partial interval as settled structure.
* **A gap is not a flat bar.** An interval in which nobody traded is a fact about
  the market. It is counted and reported, never filled in.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, Field, model_validator

from src.core.models import Contract
from src.markets.models import (
    Amount,
    Name,
    Namespace,
    PairId,
    exact_number,
    market_decimal_bounds,
)

# The single orientation this system records and reasons in: how many US dollars
# one unit of the base asset costs. Declared here as well as in VECTOR so a
# series and a setup can be compared rather than assumed to agree.
USD_PER_BASE_UNIT: Literal["USD_PER_BASE_UNIT"] = "USD_PER_BASE_UNIT"

BarPrice = Annotated[
    Decimal,
    BeforeValidator(exact_number),
    Field(gt=0, allow_inf_nan=False),
    AfterValidator(market_decimal_bounds),
]

# Provider timeframes and the seconds one unit of each covers. The provider
# accepts only a fixed set of aggregates per timeframe; both are validated here
# so an unsupported combination cannot be requested or normalized.
TIMEFRAME_SECONDS: dict[str, int] = {"minute": 60, "hour": 3600, "day": 86400}
SUPPORTED_AGGREGATES: dict[str, frozenset[int]] = {
    "minute": frozenset({1, 5, 15}),
    "hour": frozenset({1, 4, 12}),
    "day": frozenset({1}),
}


def interval_seconds(timeframe: str, aggregate: int) -> int:
    if timeframe not in TIMEFRAME_SECONDS or aggregate not in SUPPORTED_AGGREGATES[timeframe]:
        raise ValueError("Unsupported provider timeframe and aggregate combination")
    return TIMEFRAME_SECONDS[timeframe] * aggregate


class HistoryCoverage(StrEnum):
    """What the provider actually returned, never whether it was enough.

    Sufficiency is a policy verdict and belongs to the consumer. These three
    values describe the stream: whether the requested window came back whole,
    came back with intervals missing or short, or came back with no closed bar
    at all.
    """

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"


def coverage_for(
    bars: "list[MarketBar] | tuple[MarketBar, ...]", requested: int, step: int
) -> "HistoryCoverage":
    """Classify a window from the bars themselves, before one is constructed.

    Derived rather than declared, and derived in one place, so a short or gapped
    window can never be presented as a whole one by a caller that forgot to look.
    """
    if not bars:
        return HistoryCoverage.EMPTY
    span = int((bars[-1].opened_at - bars[0].opened_at).total_seconds()) // step
    missing = span + 1 - len(bars)
    whole = len(bars) == requested and missing == 0
    return HistoryCoverage.COMPLETE if whole else HistoryCoverage.PARTIAL


class MarketBar(Contract):
    """One closed interval. Decimal throughout; a float never reaches this type.

    The timestamp is the interval's **opening** instant, matching the provider,
    so the interval a bar describes is [opened_at, opened_at + interval).
    """

    opened_at: AwareDatetime
    interval_seconds: int = Field(strict=True, gt=0, le=86400)
    open: BarPrice
    high: BarPrice
    low: BarPrice
    close: BarPrice
    volume: Amount

    @model_validator(mode="after")
    def coherent_bar(self) -> Self:
        if self.low > self.high:
            raise ValueError("A bar cannot trade lower than its low or higher than its high")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("Open and close must sit inside the bar's own range")
        if self.opened_at.second or self.opened_at.microsecond:
            # Provider intervals start on an exact boundary. A sub-minute offset
            # means the timestamp is not the interval start it is taken to be.
            raise ValueError("A bar must open on an exact interval boundary")
        return self

    @property
    def closed_at(self) -> datetime:
        return self.opened_at + timedelta(seconds=self.interval_seconds)


class MarketHistory(Contract):
    """A bounded ascending series of closed bars for exactly one pool.

    Carries its own provenance because a series is only meaningful against the
    market it came from: another pool's structure would be a different market
    wearing this one's name, and a different price orientation would invert every
    comparison drawn from it while still looking like numbers.
    """

    pair_id: PairId
    provider: Name
    chain: Namespace
    network: Namespace
    venue: Name
    base_asset_id: Name
    quote_asset_id: Name
    price_basis: Literal["USD_PER_BASE_UNIT"] = USD_PER_BASE_UNIT
    timeframe: Literal["minute", "hour", "day"]
    aggregate: int = Field(strict=True, ge=1, le=15)
    # Ascending, oldest first, regardless of the order the provider used.
    bars: tuple[MarketBar, ...] = Field(max_length=1000)
    requested_bars: int = Field(strict=True, ge=1, le=1000)
    coverage: HistoryCoverage
    # When the newest closed bar closed. This is the series' own observation
    # time and what freshness is measured against.
    observed_at: AwareDatetime | None = None
    # When this was retrieved. Deliberately excluded from every digest: it says
    # when we looked, never what the market did.
    fetched_at: AwareDatetime
    is_fixture: bool = Field(strict=True)

    @model_validator(mode="after")
    def coherent_series(self) -> Self:
        step = interval_seconds(self.timeframe, self.aggregate)
        previous: MarketBar | None = None
        for bar in self.bars:
            if bar.interval_seconds != step:
                raise ValueError("Every bar must cover the series' own interval")
            if int(bar.opened_at.timestamp()) % step:
                raise ValueError("Bar openings must align to the series interval")
            if previous is not None:
                if bar.opened_at == previous.opened_at:
                    raise ValueError("A series cannot contain the same interval twice")
                if bar.opened_at < previous.opened_at:
                    raise ValueError("Bars must be ordered oldest first")
            previous = bar
        if self.base_asset_id == self.quote_asset_id:
            raise ValueError("A series must name distinct base and quote assets")
        prefix = f"{self.chain}:{self.network}:"
        if not self.pair_id.startswith(prefix):
            raise ValueError("pair_id must be chain:network:pair")
        if (self.coverage == HistoryCoverage.EMPTY) != (not self.bars):
            raise ValueError("EMPTY coverage must mean no bars, and no bars must mean EMPTY")
        if self.observed_at != (None if not self.bars else self.bars[-1].closed_at):
            raise ValueError("A series observes the market as of its newest closed bar")
        if self.observed_at is not None and self.observed_at > self.fetched_at:
            raise ValueError("A bar cannot close after it was retrieved")
        expected = HistoryCoverage.COMPLETE if self.is_whole else HistoryCoverage.PARTIAL
        if self.bars and self.coverage != expected:
            raise ValueError("Declared coverage must follow from the bars themselves")
        return self

    @property
    def is_whole(self) -> bool:
        """The full requested window arrived with no interval missing inside it."""
        return len(self.bars) == self.requested_bars and self.missing_intervals == 0

    @property
    def missing_intervals(self) -> int:
        """Intervals inside the observed window in which nobody traded.

        The provider omits empty intervals rather than synthesising flat bars,
        which is the honest behaviour and the one this system asks for. The count
        is recovered from the timestamps so the gaps stay visible.
        """
        if len(self.bars) < 2:
            return 0
        step = interval_seconds(self.timeframe, self.aggregate)
        span = int((self.bars[-1].opened_at - self.bars[0].opened_at).total_seconds()) // step
        return span + 1 - len(self.bars)

    @property
    def window_start(self) -> datetime | None:
        return self.bars[0].opened_at if self.bars else None

    @property
    def range_low(self) -> Decimal | None:
        return min(bar.low for bar in self.bars) if self.bars else None

    @property
    def range_high(self) -> Decimal | None:
        return max(bar.high for bar in self.bars) if self.bars else None

    def age(self, now: datetime) -> timedelta | None:
        """How long ago the newest bar closed, by source time rather than fetch time."""
        if self.observed_at is None:
            return None
        if now.utcoffset() is None:
            raise ValueError("Freshness requires timezone-aware time")
        return now - self.observed_at


def empty_history(
    *,
    pair_id: str,
    provider: str,
    chain: str,
    network: str,
    venue: str,
    base_asset_id: str,
    quote_asset_id: str,
    timeframe: str,
    aggregate: int,
    requested_bars: int,
    fetched_at: datetime,
    is_fixture: bool,
) -> MarketHistory:
    """A structurally valid series stating that no closed bar exists.

    A young pool that has never traded is a real answer, and it is recorded as
    one rather than as a failure to look.
    """
    return MarketHistory(
        pair_id=pair_id,
        provider=provider,
        chain=chain,
        network=network,
        venue=venue,
        base_asset_id=base_asset_id,
        quote_asset_id=quote_asset_id,
        timeframe=timeframe,  # type: ignore[arg-type]
        aggregate=aggregate,
        bars=(),
        requested_bars=requested_bars,
        coverage=HistoryCoverage.EMPTY,
        observed_at=None,
        fetched_at=fetched_at,
        is_fixture=is_fixture,
    )


def bar_from_row(row: object, *, step: int) -> MarketBar:
    """Normalize one provider row into a typed bar, or refuse it.

    The provider sends `[timestamp, open, high, low, close, volume]`. Anything
    that is not exactly that — a short row, a negative price, a low above a high,
    a non-integer timestamp — is a malformed bar and the whole series is refused
    rather than repaired, for the same reason VECTOR refuses a malformed setup.
    """
    if not isinstance(row, (list, tuple)) or len(row) != 6:
        raise ValueError("A bar row must hold exactly timestamp, open, high, low, close, volume")
    stamp, *values = row
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        raise ValueError("A bar timestamp must be an integer number of seconds")
    if not 0 < stamp < 2**34:
        raise ValueError("A bar timestamp must be a plausible epoch second")
    return MarketBar(
        opened_at=datetime.fromtimestamp(stamp, UTC),
        interval_seconds=step,
        open=values[0],
        high=values[1],
        low=values[2],
        close=values[3],
        volume=values[4],
    )


class MarketHistoryUnavailable(Exception):
    """No series could be obtained. Carries a safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class MarketHistorySource(Protocol):
    """The read a context assembler performs. Never handed to a worker."""

    async def history(
        self, identity: object, *, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory: ...


class UnconfiguredHistorySource:
    """The default. No provider is wired, and that is said rather than implied.

    Returning an empty series here would let a market with no obtainable
    structure look like a market with no trading activity, and VECTOR would draw
    the same blank conclusion from two very different facts.
    """

    async def history(
        self, identity: object, *, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory:
        raise MarketHistoryUnavailable("MARKET_HISTORY_SOURCE_NOT_CONFIGURED")
