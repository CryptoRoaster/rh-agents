"""Out-of-range outcomes: measured, audited as MISSING, never clamped — and isolated.

Reproduces the runtime failure of 2026-10-01: a BSC stream first seen at
9.32232000007788e-9 USD whose one recorded 15-minute bar carried an open and
high of 195,753,160 USD. Its maximum return, 2.0998e18 %, does not fit the
canonical NUMERIC(24, 6), the insert overflowed, and because one stream's
failure ended the whole outcome step, no stream was labelled for hours.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, DBAPIError, OperationalError

from src.data.tables import DiscoveryOutcomeSampleRow, DiscoveryStreamOutcomeRow
from src.scout.outcome_read import OutcomeReadService
from src.scout.outcomes import (
    HORIZONS_MINUTES,
    OUT_OF_RANGE,
    PERCENT_LIMIT,
    Bar,
    Label,
    OutcomeSampler,
    OutcomeStore,
    _pct,
    label_horizon,
    out_of_range,
    sample_rank,
)
from tests.scout.conftest import POOLS, MarketProvider, scout, scout_settings, young
from tests.scout.test_outcomes import (
    SAMPLER,
    OutcomeHistory,
    count,
    sample_run,
    series,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)
SEED = "rh-agents-outcomes-v1"


# ------------------------------------------------------- the runtime poison bar


RUNTIME_REFERENCE = Decimal("9.32232000007788E-9")
RUNTIME_BAR = Bar(
    T0,
    M15,
    Decimal("195753160.587417"),  # high (= the bar's open)
    Decimal("0.00000000932232000007788"),  # low
    Decimal("0.00000000932915999992226"),  # close
    Decimal("0.0009326435999984446"),  # volume
)


def test_the_runtime_poison_value_would_not_fit_the_column():
    raw = ((RUNTIME_BAR.high / RUNTIME_REFERENCE - 1) * 100).quantize(Decimal("0.000001"))
    assert raw == Decimal("2099833095042667926.688429")
    assert abs(raw) >= PERCENT_LIMIT


def test_the_runtime_poison_bar_is_missing_out_of_range_with_its_provenance():
    covering = series([RUNTIME_BAR], [(T0 - H1, T0 + M15)])

    label = label_horizon(15, T0, RUNTIME_REFERENCE, [covering])

    assert (label.status, label.missing_reason) == ("MISSING", OUT_OF_RANGE)
    assert (label.timeframe, label.aggregate, label.bars_used) == ("minute", 15, 1)
    assert (label.return_pct, label.max_return_pct, label.max_drawdown_pct) == (None, None, None)
    assert (label.survived, label.volume_usd, label.second_half_volume_share) == (None, None, None)


@pytest.mark.parametrize(
    ("high", "low", "close"),
    [
        (Decimal("2"), Decimal("1"), Decimal("1e17")),  # return_pct
        (Decimal("1e17"), Decimal("1"), Decimal("1")),  # max_return_pct
        (Decimal("2"), Decimal("1e17"), Decimal("1")),  # max_drawdown_pct
    ],
    ids=["return_pct", "max_return_pct", "max_drawdown_pct"],
)
def test_each_percent_field_out_of_range_makes_the_horizon_missing(high, low, close):
    bar = Bar(T0, M15, high, low, close, Decimal(10))
    label = label_horizon(15, T0, Decimal("1"), [series([bar], [(T0 - H1, T0 + M15)])])
    assert (label.status, label.missing_reason) == ("MISSING", OUT_OF_RANGE)


def test_a_tiny_reference_price_and_a_normal_later_price_is_missing_not_clamped():
    """The early-token case. Not a minimum-price filter: measured, then refused."""
    bars = [
        Bar(T0 + M15 * i, M15, Decimal("0.002"), Decimal("0.001"), Decimal("0.0015"), Decimal(5))
        for i in range(4)
    ]
    label = label_horizon(60, T0, Decimal("1E-20"), [series(bars, [(T0 - H1, T0 + 2 * H1)])])
    assert (label.status, label.missing_reason) == ("MISSING", OUT_OF_RANGE)
    assert label.max_return_pct is None, "no clamped value, no sentinel"


def test_decimal_arithmetic_that_cannot_be_represented_is_missing_not_a_crash():
    assert _pct(Decimal("1E+40"), Decimal("1E-40")) is None
    bar = Bar(T0, M15, Decimal("1E+40"), Decimal("1"), Decimal("1"), Decimal(1))
    label = label_horizon(15, T0, Decimal("1E-40"), [series([bar], [(T0 - H1, T0 + M15)])])
    assert (label.status, label.missing_reason) == ("MISSING", OUT_OF_RANGE)


@pytest.mark.parametrize(
    ("factor", "expected"),
    [
        (Decimal(100), Decimal("9900.000000")),
        (Decimal(1000), Decimal("99900.000000")),
        (Decimal(10) ** 9, Decimal("99999999900.000000")),
    ],
    ids=["100x", "1000x", "1e9x"],
)
def test_genuine_early_winners_stay_labelled_exactly(factor, expected):
    price = Decimal("0.001")
    bar = Bar(T0, M15, price * factor, price, price * factor, Decimal(10))
    label = label_horizon(15, T0, price, [series([bar], [(T0 - H1, T0 + M15)])])
    assert label.status == "LABELLED"
    assert label.return_pct == expected and label.max_return_pct == expected


def test_only_the_unrepresentable_horizon_is_missing():
    """15m on a spiking 15-minute read, every longer horizon on a clean hourly read."""
    spike = series([RUNTIME_BAR], [(T0 - H1, T0 + M15)])
    hourly = series(
        [
            Bar(
                T0 + H1 * i - H1,
                H1,
                Decimal("1.1e-8"),
                Decimal("0.9e-8"),
                Decimal("1e-8"),
                Decimal(1),
            )
            for i in range(80)
        ],
        [(T0 - H1, T0 + timedelta(hours=79))],
        step=H1,
    )
    labels = {m: label_horizon(m, T0, RUNTIME_REFERENCE, [spike, hourly]) for m in HORIZONS_MINUTES}

    assert (labels[15].status, labels[15].missing_reason) == ("MISSING", OUT_OF_RANGE)
    assert all(labels[m].status == "LABELLED" for m in HORIZONS_MINUTES[1:])


# -------------------------------------------------------- the sampler, end to end


TINY = "0.00000000000000000001"


def ranked_pools(n: int) -> list[int]:
    """Pool indices in the sampler's own hash order."""
    keys = {
        index: (
            "geckoterminal",
            "robinhood",
            "mainnet",
            f"robinhood:mainnet:contract_address:{POOLS[index]}",
            False,
        )
        for index in range(n)
    }
    return sorted(keys, key=lambda index: sample_rank(SEED, keys[index]))


async def discovered_with_poison(sessions, poison: int):
    """Three streams first seen at T0; one with a reference price no move fits."""
    await scout(
        sessions,
        T0,
        provider=MarketProvider(
            discovery=[young(i, price=TINY if i == poison else "0.001") for i in range(3)]
        ),
        settings=scout_settings(early_scout_max_new_watches_per_run=3),
    )


async def horizons_of(sessions, index: int):
    pair = f"robinhood:mainnet:contract_address:{POOLS[index]}"
    async with sessions() as session:
        return (
            await session.scalars(
                select(DiscoveryStreamOutcomeRow).where(DiscoveryStreamOutcomeRow.pair_id == pair)
            )
        ).all()


@pytest.mark.parametrize("position", [0, 1], ids=["poison_first", "poison_in_the_middle"])
async def test_a_poison_stream_is_audited_and_the_others_are_still_stored(db, position):
    _, sessions = db
    order = ranked_pools(3)
    poison = order[position]
    await discovered_with_poison(sessions, poison)
    history = OutcomeHistory()

    summary = await sample_run(sessions, T0 + timedelta(hours=73), history)

    assert summary.errors == ()
    assert "OUTCOME_STORE_UNAVAILABLE" not in summary.outcome_failure_codes
    assert summary.outcome_sampled == 3
    assert await count(sessions, DiscoveryOutcomeSampleRow) == 3
    rows = await horizons_of(sessions, poison)
    assert len(rows) == len(HORIZONS_MINUTES)
    assert {(r.status, r.missing_reason) for r in rows} == {("MISSING", OUT_OF_RANGE)}
    assert all(r.max_return_pct is None and r.timeframe == "minute" for r in rows)
    for good in (i for i in order if i != poison):
        assert {r.status for r in await horizons_of(sessions, good)} == {"LABELLED"}


async def test_a_finally_stored_poison_stream_is_not_sampled_again(db):
    _, sessions = db
    await discovered_with_poison(sessions, ranked_pools(3)[0])
    history = OutcomeHistory()
    await sample_run(sessions, T0 + timedelta(hours=73), history)
    reads = len(history.reads)

    again = await sample_run(sessions, T0 + timedelta(hours=74), history)

    assert (again.outcome_eligible, again.outcome_sampled) == (0, 0)
    assert len(history.reads) == reads, "no provider work for the same poison"


async def test_the_read_view_lists_the_reason_and_keeps_it_out_of_every_return(db):
    _, sessions = db
    await discovered_with_poison(sessions, ranked_pools(3)[0])
    await sample_run(sessions, T0 + timedelta(hours=73), OutcomeHistory())

    view = await OutcomeReadService(sessions).summary()

    for horizon in view.horizons:
        assert horizon.missing_reasons.get(OUT_OF_RANGE) == 1
        assert horizon.labelled == 2
        # The medians are over the two clean streams only (fixture: flat 0.001).
        assert horizon.median_max_return_pct is not None
        assert horizon.median_max_return_pct < 1e6


# ------------------------------------------------- store failures: data vs system


class PoisonedStore(OutcomeStore):
    """The real store, with one stream's labels replaced by an unchecked value.

    Simulates a value that slipped past the domain check, so the database's own
    range refusal is what the sampler meets.
    """

    def __init__(self, sessions, poison_pair: str) -> None:
        super().__init__(sessions)
        object.__setattr__(self, "poison_pair", poison_pair)

    async def save(self, candidate, labels, **kwargs):
        if candidate.key[3] == self.poison_pair:
            labels = [
                Label(
                    item.horizon_minutes,
                    "LABELLED",
                    timeframe="minute",
                    aggregate=15,
                    bars_used=1,
                    return_pct=Decimal("0"),
                    max_return_pct=Decimal("2099833095042667926.688429"),
                    max_drawdown_pct=Decimal("0"),
                    survived=True,
                    volume_usd=Decimal("1"),
                )
                for item in labels
            ]
        return await super().save(candidate, labels, **kwargs)


async def test_a_range_refusal_rolls_back_one_stream_and_the_rest_are_stored(db):
    engine, sessions = db
    if engine.dialect.name != "postgresql":
        pytest.skip("only PostgreSQL enforces NUMERIC(24, 6)")
    await discovered_with_poison(sessions, -1)  # three clean streams
    order = ranked_pools(3)
    poison_pair = f"robinhood:mainnet:contract_address:{POOLS[order[0]]}"
    history = OutcomeHistory()
    history.now = T0 + timedelta(hours=73)
    sampler = OutcomeSampler(
        bars=__import__("src.scout.outcomes", fromlist=["BarStore"]).BarStore(sessions),
        store=PoisonedStore(sessions, poison_pair),
        seed=SEED,
        max_streams=10,
        max_fetches=10,
    )

    async def fetch(candidate):
        return await history.history(
            candidate.reference.pair.market_identity, timeframe="minute", aggregate=15, bars=300
        )

    async def observe(candidates):
        return {}

    tally = await sampler.run(T0 + timedelta(hours=73), fetch, observe)

    assert tally.failure_codes == {"OUTCOME_VALUE_NOT_STORABLE": 1}
    assert tally.sampled == 2
    assert await count(sessions, DiscoveryOutcomeSampleRow) == 2, "no half-written poison sample"
    assert await horizons_of(sessions, order[0]) == []


class FailingStore(OutcomeStore):
    def __init__(self, sessions, error: Exception) -> None:
        super().__init__(sessions)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "calls", [])

    async def save(self, candidate, labels, **kwargs):
        self.calls.append(candidate.key[3])
        raise self.error


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("INSERT", {}, Exception("connection lost")),
        DataError("INSERT", {}, _Orig("22P02")),
    ],
    ids=["connection_lost", "other_data_error"],
)
async def test_a_store_failure_is_not_read_as_bad_market_data(db, error):
    """Only SQLSTATE 22003 is the stream's; anything else ends the step as before."""
    _, sessions = db
    await discovered_with_poison(sessions, -1)
    history = OutcomeHistory()
    history.now = T0 + timedelta(hours=73)
    store = FailingStore(sessions, error)
    sampler = OutcomeSampler(
        bars=__import__("src.scout.outcomes", fromlist=["BarStore"]).BarStore(sessions),
        store=store,
        seed=SEED,
        max_streams=10,
        max_fetches=10,
    )

    async def fetch(candidate):
        return await history.history(
            candidate.reference.pair.market_identity, timeframe="minute", aggregate=15, bars=300
        )

    async def observe(candidates):
        return {}

    with pytest.raises(type(error)):
        await sampler.run(T0 + timedelta(hours=73), fetch, observe)
    assert len(store.calls) == 1, "no blind retrying of the remaining streams"


def test_only_sqlstate_22003_counts_as_a_range_refusal():
    # asyncpg surfaces the overflow as a plain DBAPIError; the class is not the test.
    assert out_of_range(DBAPIError("x", {}, _Orig("22003")))
    assert out_of_range(DataError("x", {}, _Orig("22003")))
    assert not out_of_range(DBAPIError("x", {}, _Orig("08006")))
    assert not out_of_range(DataError("x", {}, _Orig("22P02")))
    assert not out_of_range(DataError("x", {}, Exception("no state")))


async def test_a_systemic_store_failure_still_reports_store_unavailable(db, monkeypatch):
    _, sessions = db
    await discovered_with_poison(sessions, -1)

    async def broken(self, *args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(OutcomeStore, "save", broken)
    summary = await sample_run(sessions, T0 + timedelta(hours=73), OutcomeHistory())
    assert summary.outcome_failure_codes == ("OUTCOME_STORE_UNAVAILABLE",) or (
        "OUTCOME_STORE_UNAVAILABLE" in summary.outcome_failure_codes
    )
    assert summary.outcome_sampled == 0


def test_settings_used_here_are_the_sampler_defaults():
    assert SAMPLER["outcome_sampler_enabled"] is True
