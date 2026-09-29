"""Discovery outcome labels: measurement, sampling budget, reuse, and no effect."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from src.data.tables import (
    DiscoveryOutcomeSampleRow,
    DiscoveryStreamOutcomeRow,
    MarketOhlcvBarRow,
    MarketOhlcvFetchRow,
)
from src.markets.fake import fixture_history
from src.markets.history import MarketHistory, MarketHistoryUnavailable
from src.markets.models import MarketIdentity
from src.scout.outcome_read import OutcomeReadService
from src.scout.outcomes import (
    HORIZONS_MINUTES,
    Bar,
    BarStore,
    Series,
    label_all,
    label_horizon,
    sample_status,
)
from tests.scout.conftest import EchoOrbit, MarketProvider, scout, scout_settings, young
from tests.scout.test_shadow import ScriptedFast, decision_state, fresh_db

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)


def bars_15m(start: datetime, prices: list[Decimal], volume: Decimal = Decimal(100)) -> list[Bar]:
    return [
        Bar(start + M15 * index, M15, price * Decimal("1.1"), price * Decimal("0.9"), price, volume)
        for index, price in enumerate(prices)
    ]


def series(bars: list[Bar], windows: list[tuple[datetime, datetime]], step=M15) -> Series:
    return Series(
        step=step,
        timeframe="minute" if step == M15 else "hour",
        aggregate=15 if step == M15 else 1,
        windows=tuple(windows),
        bars=tuple(bars),
    )


# ---------------------------------------------------------------- measuring


def test_a_covered_window_is_measured_against_the_first_price():
    prices = [Decimal("1.0"), Decimal("2.0"), Decimal("1.5"), Decimal("1.2")]
    one = series(bars_15m(T0, prices), [(T0 - H1, T0 + 2 * H1)])
    label = label_horizon(60, T0, Decimal("1.0"), [one])
    assert label.status == "LABELLED"
    assert (label.timeframe, label.aggregate, label.bars_used) == ("minute", 15, 4)
    assert label.return_pct == Decimal("20.000000")  # last close 1.2
    assert label.max_return_pct == Decimal("120.000000")  # high 2.0 * 1.1
    assert label.max_drawdown_pct == Decimal("-10.000000")  # low 1.0 * 0.9
    assert label.survived is True
    assert label.volume_usd == Decimal(400)
    assert label.second_half_volume_share == Decimal("0.500000")


def test_a_covered_window_without_trades_is_a_fact_not_a_gap():
    empty = series([], [(T0 - H1, T0 + 2 * H1)])
    label = label_horizon(60, T0, Decimal("1.0"), [empty])
    assert label.status == "LABELLED"
    assert label.survived is False and label.volume_usd == 0 and label.return_pct is None


def test_uncovered_coarse_and_unpriced_windows_are_missing_with_a_reason():
    partial = series(bars_15m(T0, [Decimal(1)]), [(T0, T0 + M15 * 2)])
    assert label_horizon(60, T0, Decimal(1), [partial]).missing_reason == "HISTORY_NOT_COVERED"
    hourly = series([], [(T0 - H1, T0 + 80 * H1)], step=H1)
    assert label_horizon(15, T0, Decimal(1), [hourly]).missing_reason == "RESOLUTION_TOO_COARSE"
    assert label_horizon(60, T0, Decimal(1), [hourly]).status == "LABELLED"
    assert label_horizon(60, T0, None, [hourly]).missing_reason == "REFERENCE_PRICE_UNAVAILABLE"


def test_windows_from_several_reads_cover_together():
    joined = series([], [(T0, T0 + 3 * H1), (T0 + 2 * H1, T0 + 6 * H1)])
    assert joined.covers(T0, T0 + 6 * H1)
    gap = series([], [(T0, T0 + 2 * H1), (T0 + 3 * H1, T0 + 6 * H1)])
    assert not gap.covers(T0, T0 + 6 * H1)


def test_one_read_of_fifteen_minute_bars_labels_every_horizon():
    long = series(bars_15m(T0, [Decimal(1)] * 300), [(T0 - M15, T0 + timedelta(hours=76))])
    labels = label_all(T0, Decimal(1), [long])
    assert [item.horizon_minutes for item in labels] == list(HORIZONS_MINUTES)
    assert sample_status(labels) == "COMPLETE"


# ------------------------------------------------------------------ the store


def history_15m(identity: MarketIdentity, now: datetime, count: int) -> MarketHistory:
    newest = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15)
    return fixture_history(
        identity,
        newest_close=newest,
        bars=count,
        timeframe="minute",
        aggregate=15,
        requested_bars=count,
        price=Decimal("0.001"),
        fetched_at=now,
    )


class OutcomeHistory:
    """15-minute series for the sampler, hourly for VECTOR checks; counts reads."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.reads: list[tuple[str, str]] = []
        self.now: datetime | None = None

    async def history(self, identity, *, timeframe: str, aggregate: int, bars: int):
        self.reads.append((identity.pair_id, timeframe))
        if self.failure is not None:
            raise self.failure
        assert self.now is not None
        if timeframe == "minute":
            return history_15m(identity, self.now, bars)
        return fixture_history(
            identity,
            newest_close=self.now.replace(minute=0, second=0, microsecond=0),
            bars=bars,
            requested_bars=bars,
            price=Decimal("0.001"),
            fetched_at=self.now,
        )


async def count(sessions, table) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(table)) or 0)


async def test_a_recorded_read_keeps_its_window_and_each_bar_once(db):
    _, sessions = db
    identity = MarketIdentity.model_validate(
        {
            "provider": "geckoterminal",
            "chain": "robinhood",
            "network": "mainnet",
            "pair_id": "robinhood:mainnet:contract_address:0x" + "c1" * 20,
            "base_asset_id": "robinhood:mainnet:0x" + "a3" * 20,
            "quote_asset_id": "robinhood:mainnet:0x" + "a4" * 20,
            "venue": "v",
            "is_fixture": False,
        }
    )
    now = T0 + timedelta(hours=10, minutes=7)
    store = BarStore(sessions)
    await store.record(history_15m(identity, now, 8), identity, source="OUTCOME_SAMPLER")
    await store.record(history_15m(identity, now, 8), identity, source="OUTCOME_SAMPLER")
    assert await count(sessions, MarketOhlcvFetchRow) == 2
    assert await count(sessions, MarketOhlcvBarRow) == 8
    (stored,) = await store.series(
        (identity.provider, identity.chain, identity.network, identity.pair_id, False)
    )
    assert (stored.timeframe, stored.aggregate, len(stored.bars)) == ("minute", 15, 8)
    # Closed bars end at the start of the interval the read was made in.
    assert stored.windows[0][1] == T0 + timedelta(hours=10)
    assert stored.windows[0][0] == T0 + timedelta(hours=8)


# ---------------------------------------------------------------- sampling


# The sampler, measured alone: scout history reads (their own transport since
# the fair queue) would share the scripted series and blur the read counts.
SAMPLER = {"outcome_sampler_enabled": True, "early_scout_history_max_requests_per_run": 0}


async def discovered(sessions, *, limit: int = 2, pools: int = 3):
    """Streams first seen at T0: `limit` watches and the rest declined."""
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(index) for index in range(pools)]),
        settings=scout_settings(early_scout_max_new_watches_per_run=limit),
    )


async def sample_run(sessions, at: datetime, history: OutcomeHistory, **settings: Any):
    history.now = at
    return await scout(
        sessions,
        at,
        provider=MarketProvider(),
        history=history,  # type: ignore[arg-type]
        settings=scout_settings(**{**SAMPLER, **settings}),
    )


async def test_watched_and_declined_candidates_are_labelled_with_one_read_each(db):
    _, sessions = db
    await discovered(sessions)
    history = OutcomeHistory()
    summary = await sample_run(sessions, T0 + timedelta(hours=73), history)

    assert (summary.outcome_eligible, summary.outcome_sampled, summary.outcome_fetched) == (
        3,
        3,
        3,
    )
    assert summary.errors == ()
    # One 15-minute read per stream labels all eight horizons.
    assert sorted(item[1] for item in history.reads) == ["minute"] * 3
    assert await count(sessions, DiscoveryOutcomeSampleRow) == 3
    assert await count(sessions, DiscoveryStreamOutcomeRow) == 3 * len(HORIZONS_MINUTES)
    async with sessions() as session:
        statuses = set((await session.scalars(select(DiscoveryOutcomeSampleRow.status))).all())
        labelled = set((await session.scalars(select(DiscoveryStreamOutcomeRow.status))).all())
    assert statuses == {"COMPLETE"} and labelled == {"LABELLED"}

    view = await OutcomeReadService(sessions).summary()
    assert (view.candidates, view.sampled) == (3, 3)
    groups = {item.group: item.labelled for item in view.watched_vs_declined}
    assert groups == {"declined": 1, "watched": 2}
    assert {item.horizon_minutes: item.labelled for item in view.horizons}[1440] == 3


async def test_a_stream_is_sampled_once_and_not_before_every_horizon_closed(db):
    _, sessions = db
    await discovered(sessions)
    history = OutcomeHistory()
    early = await sample_run(sessions, T0 + timedelta(hours=71), history)
    assert (early.outcome_eligible, early.outcome_sampled) == (0, 0)
    await sample_run(sessions, T0 + timedelta(hours=73), history)
    again = await sample_run(sessions, T0 + timedelta(hours=74), history)
    assert (again.outcome_eligible, again.outcome_sampled) == (0, 0)
    assert len(history.reads) == 3


async def test_the_request_budget_bounds_reads_and_the_rest_waits(db):
    _, sessions = db
    await discovered(sessions, limit=5, pools=6)
    history = OutcomeHistory()
    # 4 requests: one network lookup, one liquidity read per chain (1), two reads.
    first = await sample_run(
        sessions, T0 + timedelta(hours=73), history, outcome_max_requests_per_run=4
    )
    assert (first.outcome_eligible, first.outcome_fetched) == (6, 2)
    assert first.outcome_requests <= 4
    second = await sample_run(
        sessions, T0 + timedelta(hours=74), history, outcome_max_requests_per_run=4
    )
    assert (second.outcome_eligible, second.outcome_fetched) == (4, 2)


async def test_stored_hourly_reads_are_reused_without_asking_again(db):
    _, sessions = db
    await discovered(sessions, limit=1, pools=1)
    store = BarStore(sessions)
    from src.scout.repository import WatchRepository
    from tests.scout.conftest import POOLS, pair_id

    watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[0]))
    at = T0 + timedelta(hours=73)
    # An hourly read (as a VECTOR check records) reaching back past first sight.
    await store.record(
        fixture_history(
            watch.market,
            newest_close=at.replace(minute=0, second=0, microsecond=0),
            bars=80,
            requested_bars=80,
            price=Decimal("0.001"),
            fetched_at=at,
        ),
        watch.market,
        source="SCOUT_VECTOR_HISTORY",
    )
    history = OutcomeHistory()
    summary = await sample_run(sessions, at, history)
    assert (summary.outcome_reused, summary.outcome_fetched, history.reads) == (1, 0, [])
    async with sessions() as session:
        sample = await session.scalar(select(DiscoveryOutcomeSampleRow))
        fifteen = await session.scalar(
            select(DiscoveryStreamOutcomeRow).where(DiscoveryStreamOutcomeRow.horizon_minutes == 15)
        )
    assert (sample.history_source, sample.status) == ("REUSED", "PARTIAL")
    assert fifteen.missing_reason == "RESOLUTION_TOO_COARSE"


async def test_a_vector_history_check_records_its_read_for_reuse(db):
    _, sessions = db
    await discovered(sessions, limit=1, pools=1)
    history = OutcomeHistory()
    history.now = T0 + timedelta(hours=25)
    summary = await scout(
        sessions,
        T0 + timedelta(hours=25),
        provider=MarketProvider(targeted=[young(0)]),
        history=history,  # type: ignore[arg-type]
    )
    assert summary.history_checks == 1
    async with sessions() as session:
        sources = (await session.scalars(select(MarketOhlcvFetchRow.source))).all()
    assert sources == ["SCOUT_VECTOR_HISTORY"]


@pytest.mark.parametrize(
    ("code", "sampled"),
    [
        ("MARKET_HISTORY_PROVIDER_RATE_LIMITED", 0),  # stop: asked again next run
        ("MARKET_HISTORY_PROVIDER_UNAVAILABLE", 0),  # weather: asked again next run
        ("MARKET_HISTORY_PROVIDER_REJECTED", 3),  # final: recorded as unavailable
    ],
)
async def test_a_provider_failure_never_becomes_a_run_error(db, code, sampled):
    _, sessions = db
    await discovered(sessions)
    history = OutcomeHistory(failure=MarketHistoryUnavailable(code))
    summary = await sample_run(sessions, T0 + timedelta(hours=73), history)
    assert summary.errors == ()
    assert summary.outcome_sampled == sampled
    assert code in summary.outcome_failure_codes


async def test_the_sampler_is_off_by_default(db):
    _, sessions = db
    await discovered(sessions)
    history = OutcomeHistory()
    history.now = T0 + timedelta(hours=73)
    summary = await scout(
        sessions,
        T0 + timedelta(hours=73),
        provider=MarketProvider(),
        history=history,  # type: ignore[arg-type]
    )
    assert (summary.outcome_sampled, summary.outcome_requests) == (0, 0)
    assert await count(sessions, DiscoveryOutcomeSampleRow) == 0


async def test_labels_change_no_decision_input():
    """The same runs with the sampler off and on: every decision input is identical."""
    results = []
    for enabled in (False, True):
        engine, sessions = await fresh_db()
        try:
            orbit = EchoOrbit()
            history = OutcomeHistory()
            settings = scout_settings(
                outcome_sampler_enabled=enabled, early_scout_max_new_watches_per_run=2
            )
            provider = MarketProvider(
                discovery=[young(index) for index in range(4)],
                targeted=[young(index) for index in range(4)],
            )
            for at in (T0, T0 + timedelta(hours=25), T0 + timedelta(hours=73)):
                history.now = at
                await scout(
                    sessions,
                    at,
                    provider=provider,
                    orbit=orbit,
                    history=history,  # type: ignore[arg-type]
                    settings=settings,
                    fast=ScriptedFast(),
                )
            results.append(
                {
                    "state": await decision_state(sessions),
                    "orbit": [call.data["market_observation"]["pair_id"] for call in orbit.calls],
                }
            )
            if enabled:
                assert await count(sessions, DiscoveryOutcomeSampleRow) > 0
        finally:
            await engine.dispose()
    off, on = results
    assert on == off
    assert all(value == 0 for value in on["state"]["counts"].values())


def test_the_sampler_defaults_are_off_and_bounded():
    settings = scout_settings()
    assert settings.outcome_sampler_enabled is False
    assert (settings.outcome_max_requests_per_run, settings.outcome_max_streams_per_run) == (10, 40)
