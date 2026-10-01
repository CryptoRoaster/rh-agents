"""Objective outcomes for discovery candidates. Labels only; nothing decides from them.

For every discovery stream — watched or declined by the watch limit — measure
what its market actually did after it was first seen, over fixed horizons:
return, maximum return, maximum drawdown, survival and volume persistence,
plus liquidity persistence at sampling time. The labels exist to calibrate
JEV, ORBIT and, later, entry strategies against reality instead of against
each other.

**One read serves every horizon.** A stream is sampled once, when all horizons
have closed (first seen + 72h + one bar): a single 15-minute OHLCV read
reaching back to first sight yields all eight labels. Before asking the
provider, stored bars are tried first — the scout's VECTOR history check
records its hourly read in the same bar store — so a stream whose window is
already covered is labelled without a request.

**Budgeted and last.** The sampler runs at the end of a scout run with its own
GeckoTerminal transport and its own request cap, after discovery, ORBIT and
history have used theirs, so it can never displace them. Which eligible
streams are sampled first is decided by a keyed hash of the stream (the same
signal-blind idea as the calibration sample), never by price, liquidity, JEV
or ORBIT.

**Measurement rules.** The reference is the stream's first recorded
observation (its instant and USD price). A horizon is labelled only when a
recorded OHLCV read covered the whole window [first seen, first seen + h] at
a resolution no coarser than h; otherwise it is MISSING with a reason. The
provider omits intervals without trades, so a covered window without bars is
a real answer: no trades, not survived. Returns are measured to the close of
the last bar that opened inside the window, so a label is exact to within one
bar (15 minutes for sampler reads).
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import (
    DiscoveryOutcomeSampleRow,
    DiscoveryStreamDeclineRow,
    DiscoveryStreamOutcomeRow,
    DiscoveryWatchRow,
    MarketObservationRow,
    MarketOhlcvBarRow,
    MarketOhlcvFetchRow,
)
from src.markets.history import MarketHistory, interval_seconds
from src.markets.models import Availability, MarketIdentity, MarketSnapshot

SCHEMA_VERSION = 1
HORIZONS_MINUTES = (15, 60, 180, 360, 720, 1440, 2880, 4320)
SAMPLE_TIMEFRAME = "minute"
SAMPLE_AGGREGATE = 15
SAMPLE_STEP = timedelta(minutes=15)
# Every horizon closed, plus one bar so the last one is settled.
ELIGIBLE_AFTER = timedelta(minutes=HORIZONS_MINUTES[-1]) + SAMPLE_STEP
# A single read returns at most 1,000 bars: 250 hours of 15-minute bars.
# Older streams can no longer be labelled from one read and are left alone.
ELIGIBLE_UNTIL = timedelta(hours=240)
PERCENT = Decimal("0.000001")
# The largest magnitude a percent outcome may have to be a label at all: what
# the canonical column type, NUMERIC(24, 6), can hold — eighteen integer digits.
# A technical bound, not a market judgement: a 1000x is 99,900 %, a 10^9-fold
# move is still about 10^11 %, and both are labels. Only a value that cannot be
# stored exactly — in practice a provider bar whose price is off by many orders
# of magnitude — is recorded as MISSING with `OUT_OF_RANGE` instead, and never
# clamped, rounded to a bound or replaced by a sentinel.
PERCENT_LIMIT = Decimal(10) ** 18
OUT_OF_RANGE = "OUTCOME_PERCENT_OUT_OF_RANGE"

StreamKey = tuple[str, str, str, str, bool]


def stream_of(identity: MarketIdentity) -> StreamKey:
    return (
        identity.provider,
        identity.chain,
        identity.network,
        identity.pair_id,
        identity.is_fixture,
    )


def sample_rank(seed: str, key: StreamKey) -> str:
    """Signal-blind priority: a keyed hash of the stream identity and nothing else."""
    return hashlib.sha256(f"{seed}:{':'.join(map(str, key))}".encode()).hexdigest()


# ------------------------------------------------------------------ labels


@dataclass(frozen=True)
class Bar:
    opened_at: datetime
    step: timedelta
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Series:
    """Bars of one resolution and the windows recorded reads covered."""

    step: timedelta
    timeframe: str
    aggregate: int
    windows: tuple[tuple[datetime, datetime], ...]
    bars: tuple[Bar, ...]

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether recorded reads, taken together, saw every instant of [start, end]."""
        reach = start
        for window_start, window_end in sorted(self.windows):
            if window_start > reach:
                return False
            reach = max(reach, window_end)
            if reach >= end:
                return True
        return reach >= end


@dataclass(frozen=True)
class Label:
    horizon_minutes: int
    status: str
    missing_reason: str | None = None
    timeframe: str | None = None
    aggregate: int | None = None
    bars_used: int = 0
    return_pct: Decimal | None = None
    max_return_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    survived: bool | None = None
    volume_usd: Decimal | None = None
    second_half_volume_share: Decimal | None = None


def _pct(value: Decimal, reference: Decimal) -> Decimal | None:
    """The move from `reference` to `value` in percent, or None if not storable.

    None when the exact percentage, at the stored scale, does not fit the
    canonical column — including when decimal arithmetic itself cannot represent
    it. Never clamped.
    """
    try:
        pct = ((value / reference - 1) * 100).quantize(PERCENT)
    except ArithmeticError:
        # InvalidOperation (quantize beyond the context precision), Overflow,
        # DivisionByZero: a number no column could hold.
        return None
    if not pct.is_finite() or abs(pct) >= PERCENT_LIMIT:
        return None
    return pct


def label_horizon(
    horizon_minutes: int, first_seen: datetime, price: Decimal | None, series: list[Series]
) -> Label:
    """One horizon's label from the finest recorded series that covers it."""
    if price is None or price <= 0:
        return Label(horizon_minutes, "MISSING", "REFERENCE_PRICE_UNAVAILABLE")
    horizon = timedelta(minutes=horizon_minutes)
    end = first_seen + horizon
    usable = [item for item in series if item.step <= horizon]
    chosen = next(
        (item for item in sorted(usable, key=lambda s: s.step) if item.covers(first_seen, end)),
        None,
    )
    if chosen is None:
        coarse = any(item.covers(first_seen, end) for item in series)
        return Label(
            horizon_minutes,
            "MISSING",
            "RESOLUTION_TOO_COARSE" if coarse else "HISTORY_NOT_COVERED",
        )
    inside = [
        bar for bar in chosen.bars if bar.opened_at + bar.step > first_seen and bar.opened_at < end
    ]
    base = dict(
        horizon_minutes=horizon_minutes,
        status="LABELLED",
        timeframe=chosen.timeframe,
        aggregate=chosen.aggregate,
        bars_used=len(inside),
    )
    if not inside:
        # Covered, and nobody traded: a fact, recorded as such.
        return Label(**base, survived=False, volume_usd=Decimal(0))  # type: ignore[arg-type]
    total = sum((bar.volume for bar in inside), Decimal(0))
    tail = max(chosen.step, horizon / 4)
    late = sum(
        (bar.volume for bar in inside if bar.opened_at >= first_seen + horizon / 2), Decimal(0)
    )
    moves = (
        _pct(inside[-1].close, price),
        _pct(max(bar.high for bar in inside), price),
        _pct(min(bar.low for bar in inside), price),
    )
    if any(item is None for item in moves):
        # Measured, and not representable. Kept as an audited gap with the
        # measurement's provenance — never a clamped or estimated label, and
        # nothing else of this horizon is kept beside it.
        return Label(
            horizon_minutes,
            "MISSING",
            OUT_OF_RANGE,
            timeframe=chosen.timeframe,
            aggregate=chosen.aggregate,
            bars_used=len(inside),
        )
    return_pct, max_return_pct, max_drawdown_pct = moves
    return Label(
        **base,  # type: ignore[arg-type]
        return_pct=return_pct,
        max_return_pct=max_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        survived=any(bar.volume > 0 and bar.opened_at + bar.step > end - tail for bar in inside),
        volume_usd=total,
        second_half_volume_share=(late / total).quantize(PERCENT) if total > 0 else None,
    )


def label_all(first_seen: datetime, price: Decimal | None, series: list[Series]) -> list[Label]:
    return [label_horizon(minutes, first_seen, price, series) for minutes in HORIZONS_MINUTES]


def sample_status(labels: list[Label]) -> str:
    labelled = sum(1 for item in labels if item.status == "LABELLED")
    if labelled == len(labels):
        return "COMPLETE"
    return "PARTIAL" if labelled else "UNAVAILABLE"


# -------------------------------------------------------------------- bars


def _key_where(model: Any, key: StreamKey) -> tuple[Any, ...]:
    provider, chain, network, pair_id, fixture = key
    return (
        model.provider == provider,
        model.chain == chain,
        model.network == network,
        model.pair_id == pair_id,
        model.is_fixture.is_(fixture),
    )


@dataclass(frozen=True)
class BarStore:
    """Closed OHLCV bars per stream, with the window each read covered."""

    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self, history: MarketHistory, identity: MarketIdentity, *, source: str
    ) -> None:
        """Keep a read's closed bars and its window. A bar already held is kept as is.

        Stored under the stream that was asked about, which is the key every
        discovery table uses; a read of another pair is refused.
        """
        if history.pair_id != identity.pair_id:
            raise ValueError("A history read must belong to the stream it is recorded for")
        step = timedelta(seconds=interval_seconds(history.timeframe, history.aggregate))
        fetched = history.fetched_at
        epoch = datetime(1970, 1, 1, tzinfo=fetched.tzinfo)
        # The forming bar was dropped: closed bars end at the start of the
        # interval the read was made in, and reach back `requested_bars` bars.
        window_end = epoch + ((fetched - epoch) // step) * step
        window_start = window_end - step * history.requested_bars
        fetch_id = uuid4()
        key = (identity.provider, identity.chain, identity.network, identity.pair_id)
        async with self.sessions.begin() as session:
            session.add(
                MarketOhlcvFetchRow(
                    id=fetch_id,
                    provider=key[0],
                    chain=key[1],
                    network=key[2],
                    pair_id=key[3],
                    is_fixture=identity.is_fixture,
                    timeframe=history.timeframe,
                    aggregate=history.aggregate,
                    window_start=window_start,
                    window_end=window_end,
                    fetched_at=fetched,
                    source=source,
                    bars_returned=len(history.bars),
                )
            )
            await session.flush()
            if not history.bars:
                return
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            await session.execute(
                insert(MarketOhlcvBarRow)
                .values(
                    [
                        dict(
                            id=uuid4(),
                            fetch_id=fetch_id,
                            provider=key[0],
                            chain=key[1],
                            network=key[2],
                            pair_id=key[3],
                            is_fixture=identity.is_fixture,
                            timeframe=history.timeframe,
                            aggregate=history.aggregate,
                            opened_at=bar.opened_at,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            volume=bar.volume,
                        )
                        for bar in history.bars
                    ]
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "provider",
                        "chain",
                        "network",
                        "pair_id",
                        "is_fixture",
                        "timeframe",
                        "aggregate",
                        "opened_at",
                    ]
                )
            )

    async def series(self, key: StreamKey) -> list[Series]:
        """Every recorded resolution of this stream, with its covered windows."""
        async with self.sessions() as session:
            fetches = (
                await session.execute(
                    select(
                        MarketOhlcvFetchRow.timeframe,
                        MarketOhlcvFetchRow.aggregate,
                        MarketOhlcvFetchRow.window_start,
                        MarketOhlcvFetchRow.window_end,
                    ).where(*_key_where(MarketOhlcvFetchRow, key))
                )
            ).all()
            bars = (
                await session.scalars(
                    select(MarketOhlcvBarRow)
                    .where(*_key_where(MarketOhlcvBarRow, key))
                    .order_by(MarketOhlcvBarRow.opened_at)
                )
            ).all()
        windows: dict[tuple[str, int], list[tuple[datetime, datetime]]] = defaultdict(list)
        for timeframe, aggregate, start, end in fetches:
            windows[(timeframe, aggregate)].append((aware(start), aware(end)))
        grouped: dict[tuple[str, int], list[Bar]] = defaultdict(list)
        for row in bars:
            step = timedelta(seconds=interval_seconds(row.timeframe, row.aggregate))
            grouped[(row.timeframe, row.aggregate)].append(
                Bar(aware(row.opened_at), step, row.high, row.low, row.close, row.volume)
            )
        return [
            Series(
                step=timedelta(seconds=interval_seconds(timeframe, aggregate)),
                timeframe=timeframe,
                aggregate=aggregate,
                windows=tuple(spans),
                bars=tuple(grouped.get((timeframe, aggregate), ())),
            )
            for (timeframe, aggregate), spans in windows.items()
        ]


# --------------------------------------------------------------- candidates


@dataclass(frozen=True)
class Candidate:
    key: StreamKey
    reference: MarketSnapshot
    reference_id: UUID

    @property
    def first_seen(self) -> datetime:
        return self.reference.observed_at

    @property
    def price(self) -> Decimal | None:
        price = self.reference.price
        return price.value_usd if price.status == Availability.AVAILABLE else None

    @property
    def liquidity(self) -> Decimal | None:
        item = self.reference.liquidity
        return item.value_usd if item.status == Availability.AVAILABLE else None


@dataclass(frozen=True)
class OutcomeStore:
    sessions: async_sessionmaker[AsyncSession]

    async def eligible(self, now: datetime) -> list[tuple[StreamKey, datetime]]:
        """Discovery streams (watched or declined), all horizons closed, not yet sampled.

        Keys and first-seen instants only; the caller ranks them before any
        reference is loaded, so the database order decides nothing.
        """
        row = MarketObservationRow
        watched = exists(
            select(DiscoveryWatchRow.id).where(
                DiscoveryWatchRow.provider == row.provider,
                DiscoveryWatchRow.chain == row.chain,
                DiscoveryWatchRow.network == row.network,
                DiscoveryWatchRow.pair_id == row.pair_id,
                DiscoveryWatchRow.is_fixture.is_(False),
            )
        )
        declined = exists(
            select(DiscoveryStreamDeclineRow.id).where(
                DiscoveryStreamDeclineRow.provider == row.provider,
                DiscoveryStreamDeclineRow.chain == row.chain,
                DiscoveryStreamDeclineRow.network == row.network,
                DiscoveryStreamDeclineRow.pair_id == row.pair_id,
                DiscoveryStreamDeclineRow.is_fixture.is_(False),
            )
        )
        sampled = exists(
            select(DiscoveryOutcomeSampleRow.id).where(
                DiscoveryOutcomeSampleRow.provider == row.provider,
                DiscoveryOutcomeSampleRow.chain == row.chain,
                DiscoveryOutcomeSampleRow.network == row.network,
                DiscoveryOutcomeSampleRow.pair_id == row.pair_id,
                DiscoveryOutcomeSampleRow.is_fixture.is_(False),
                DiscoveryOutcomeSampleRow.schema_version == SCHEMA_VERSION,
            )
        )
        first = func.min(row.observed_at)
        async with self.sessions() as session:
            streams = (
                await session.execute(
                    select(row.provider, row.chain, row.network, row.pair_id, first)
                    .where(row.is_fixture.is_(False), watched | declined, ~sampled)
                    .group_by(row.provider, row.chain, row.network, row.pair_id)
                    .having(and_(first <= now - ELIGIBLE_AFTER, first >= now - ELIGIBLE_UNTIL))
                )
            ).all()
        return [
            ((provider, chain, network, pair_id, False), aware(first_seen))
            for provider, chain, network, pair_id, first_seen in streams
        ]

    async def candidate(self, key: StreamKey, first_seen: datetime) -> Candidate | None:
        """The stream's first recorded observation: its reference instant and price."""
        row = MarketObservationRow
        async with self.sessions() as session:
            reference = await session.scalar(
                select(row)
                .where(*_key_where(row, key), row.observed_at == first_seen)
                .order_by(row.recorded_at, row.id)
                .limit(1)
            )
        if reference is None:
            return None
        return Candidate(
            key=key,
            reference=MarketSnapshot.model_validate(reference.payload),
            reference_id=reference.id,
        )

    async def save(
        self,
        candidate: Candidate,
        labels: list[Label],
        *,
        now: datetime,
        history_source: str,
        requests: int,
        reason: str | None,
        liquidity_now: Decimal | None,
    ) -> None:
        """Store one stream's sample and horizons in one transaction, or nothing.

        A value the database refuses as out of range (SQLSTATE 22003) is a fact
        about this stream's data: the transaction rolls back as a whole and
        `OutcomeValueNotStorable` is raised for the caller to count and move
        on. Every other database error is the store's and propagates.
        """
        try:
            await self._save(
                candidate,
                labels,
                now=now,
                history_source=history_source,
                requests=requests,
                reason=reason,
                liquidity_now=liquidity_now,
            )
        except DBAPIError as error:
            # asyncpg reaches here as a plain DBAPIError rather than DataError,
            # so the class says little; the SQLSTATE says exactly what happened.
            if not out_of_range(error):
                raise
            raise OutcomeValueNotStorable() from None

    async def _save(
        self,
        candidate: Candidate,
        labels: list[Label],
        *,
        now: datetime,
        history_source: str,
        requests: int,
        reason: str | None,
        liquidity_now: Decimal | None,
    ) -> None:
        provider, chain, network, pair_id, fixture = candidate.key
        sample_id = uuid4()
        async with self.sessions.begin() as session:
            session.add(
                DiscoveryOutcomeSampleRow(
                    id=sample_id,
                    provider=provider,
                    chain=chain,
                    network=network,
                    pair_id=pair_id,
                    is_fixture=fixture,
                    schema_version=SCHEMA_VERSION,
                    reference_observation_id=candidate.reference_id,
                    reference_at=candidate.first_seen,
                    reference_price_usd=candidate.price,
                    sampled_at=now,
                    status=sample_status(labels),
                    reason_code=reason,
                    history_source=history_source,
                    provider_requests=requests,
                    liquidity_at_reference_usd=candidate.liquidity,
                    liquidity_at_sample_usd=liquidity_now,
                )
            )
            await session.flush()
            for item in labels:
                session.add(
                    DiscoveryStreamOutcomeRow(
                        id=uuid4(),
                        sample_id=sample_id,
                        provider=provider,
                        chain=chain,
                        network=network,
                        pair_id=pair_id,
                        is_fixture=fixture,
                        horizon_minutes=item.horizon_minutes,
                        schema_version=SCHEMA_VERSION,
                        status=item.status,
                        missing_reason=item.missing_reason,
                        timeframe=item.timeframe,
                        aggregate=item.aggregate,
                        bars_used=item.bars_used,
                        return_pct=item.return_pct,
                        max_return_pct=item.max_return_pct,
                        max_drawdown_pct=item.max_drawdown_pct,
                        survived=item.survived,
                        volume_usd=item.volume_usd,
                        second_half_volume_share=item.second_half_volume_share,
                        computed_at=now,
                    )
                )


class OutcomeValueNotStorable(Exception):
    """One stream's outcome was refused by the database as out of range."""


def out_of_range(error: DBAPIError) -> bool:
    """Whether a database error is exactly a numeric range refusal (22003).

    Narrow on purpose: only this SQLSTATE says the data, not the store, is the
    problem. Anything else — a lost connection, a broken transaction, a schema
    mismatch — is not reinterpreted as bad market data.
    """
    original = getattr(error, "orig", None)
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code == "22003"


@dataclass
class OutcomeTally:
    eligible: int = 0
    sampled: int = 0
    reused: int = 0
    fetched: int = 0
    requests: int = 0
    failure_codes: dict[str, int] = field(default_factory=dict)

    def failure(self, code: str) -> None:
        self.failure_codes[code] = self.failure_codes.get(code, 0) + 1


# ------------------------------------------------------------------ sampler

# After these the run asks the provider nothing more: it is refusing, or this
# run's own request budget is spent.
STOP_CODES = frozenset(
    {
        "MARKET_HISTORY_PROVIDER_RATE_LIMITED",
        "MARKET_HISTORY_REQUEST_BUDGET_EXHAUSTED",
        "MARKET_HISTORY_PROVIDER_NOT_AUTHORIZED",
    }
)
# These cannot change on a retry; the stream is recorded as UNAVAILABLE instead
# of being asked again every run. Anything else is weather and is retried later.
FINAL_CODES = frozenset(
    {
        "MARKET_HISTORY_PROVIDER_IDENTITY",
        "MARKET_HISTORY_NETWORK_UNSUPPORTED",
        "MARKET_HISTORY_PROVIDER_REJECTED",
    }
)


def classify_history_failure(code: str) -> "SamplerStop | SamplerSkip":
    if code in STOP_CODES:
        return SamplerStop(code)
    return SamplerSkip(code, final=code in FINAL_CODES)


@dataclass(frozen=True)
class OutcomeSampler:
    """Label as many eligible streams as the budget allows, reused bars first."""

    bars: BarStore
    store: OutcomeStore
    seed: str
    max_streams: int
    max_fetches: int

    def reusable(self, labels: list[Label]) -> bool:
        """Stored reads suffice when every horizon is labelled but, at most, the finest.

        Hourly VECTOR reads cannot resolve 15 minutes; they still label the
        other seven horizons, and one missing 15-minute label is not worth a read.
        """
        # A horizon measured and found unstorable needs no new read: the bars
        # are recorded, and asking again would only measure them again.
        missing = [
            item
            for item in labels
            if item.status == "MISSING" and item.missing_reason != OUT_OF_RANGE
        ]
        return not missing or (
            len(missing) == 1
            and missing[0].horizon_minutes == HORIZONS_MINUTES[0]
            and missing[0].missing_reason == "RESOLUTION_TOO_COARSE"
        )

    async def run(self, now: datetime, fetch: Any, observe: Any) -> OutcomeTally:
        """`fetch(candidate) -> MarketHistory` and `observe(candidates) -> {pair: liquidity}`.

        Both raise `SamplerStop(code)` to end the run's provider work and
        `SamplerSkip(code, final)` to give up on one stream.
        """
        tally = OutcomeTally()
        eligible = await self.store.eligible(now)
        tally.eligible = len(eligible)
        ranked = sorted(eligible, key=lambda item: sample_rank(self.seed, item[0]))
        done: list[tuple[Candidate, list[Label], str, int, str | None]] = []
        fetches = 0
        stopped = False
        for key, first_seen in ranked:
            if len(done) >= self.max_streams:
                break
            candidate = await self.store.candidate(key, first_seen)
            if candidate is None:
                continue
            labels = label_all(candidate.first_seen, candidate.price, await self.bars.series(key))
            if self.reusable(labels):
                done.append((candidate, labels, "REUSED", 0, None))
                tally.reused += 1
                continue
            if stopped or fetches >= self.max_fetches:
                continue
            fetches += 1
            try:
                history = await fetch(candidate)
            except SamplerStop as stop:
                tally.failure(stop.code)
                stopped = True
                continue
            except SamplerSkip as skip:
                tally.failure(skip.code)
                if skip.final:
                    done.append((candidate, labels, "NONE", 1, skip.code))
                continue
            await self.bars.record(
                history, candidate.reference.pair.market_identity, source="OUTCOME_SAMPLER"
            )
            labels = label_all(candidate.first_seen, candidate.price, await self.bars.series(key))
            done.append((candidate, labels, "FETCHED", 1, None))
            tally.fetched += 1
        liquidity: dict[str, Decimal] = {}
        if done and not stopped:
            try:
                liquidity = await observe([item[0] for item in done])
            except SamplerStop as stop:
                tally.failure(stop.code)
        for candidate, labels, source, requests, reason in done:
            # Each stream is its own transaction, and one stream's data cannot
            # stop the rest: a range refusal is counted and the next stream is
            # stored. A store failure of any other kind still ends the run.
            try:
                await self.store.save(
                    candidate,
                    labels,
                    now=now,
                    history_source=source,
                    requests=requests,
                    reason=reason,
                    liquidity_now=liquidity.get(candidate.key[3]),
                )
            except OutcomeValueNotStorable:
                tally.failure("OUTCOME_VALUE_NOT_STORABLE")
                continue
            tally.sampled += 1
        return tally


class SamplerStop(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SamplerSkip(Exception):
    def __init__(self, code: str, *, final: bool) -> None:
        self.code = code
        self.final = final
        super().__init__(code)
