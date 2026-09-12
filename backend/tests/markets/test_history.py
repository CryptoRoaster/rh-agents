"""The market history contract itself, independent of any provider.

Every rule below exists because a series that is quietly wrong is worse than one
that is missing: VECTOR draws price levels from these bars, and a window that
misreports its own shape would produce a setup that looks grounded and is not.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.markets.history import (
    HistoryCoverage,
    MarketBar,
    MarketHistory,
    bar_from_row,
    coverage_for,
    empty_history,
    interval_seconds,
)

ANCHOR = datetime(2026, 9, 12, 6, tzinfo=UTC)
STEP = 3600
PAIR = "bsc:mainnet:contract_address:0x" + "e5" * 20


def bar(index: int, *, step: int = STEP, price: str = "1.00", **overrides) -> MarketBar:
    """A coherent bar around ``price``; ``overrides`` replace individual fields.

    The anchor is named ``price`` rather than ``close`` so that overriding the
    close field cannot silently move the whole bar with it.
    """
    anchor = Decimal(price)
    defaults: dict[str, object] = dict(
        opened_at=ANCHOR + timedelta(seconds=step * index),
        interval_seconds=step,
        open=anchor,
        high=anchor + Decimal("0.05"),
        low=anchor - Decimal("0.05"),
        close=anchor,
        volume=Decimal("100"),
    )
    defaults.update(overrides)
    return MarketBar(**defaults)  # type: ignore[arg-type]


def series(bars, *, requested=None, coverage=None, **overrides) -> MarketHistory:
    bars = tuple(bars)
    count = requested if requested is not None else len(bars)
    defaults: dict[str, object] = dict(
        pair_id=PAIR,
        provider="geckoterminal",
        chain="bsc",
        network="mainnet",
        venue="pancakeswap-v3",
        base_asset_id="bsc:mainnet:0x" + "a1" * 20,
        quote_asset_id="bsc:mainnet:0x" + "b2" * 20,
        timeframe="hour",
        aggregate=1,
        bars=bars,
        requested_bars=count,
        coverage=coverage if coverage is not None else coverage_for(bars, count, STEP),
        observed_at=bars[-1].closed_at if bars else None,
        fetched_at=(bars[-1].closed_at if bars else ANCHOR) + timedelta(minutes=1),
        is_fixture=False,
    )
    defaults.update(overrides)
    return MarketHistory(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------- one bar's rules


def test_a_coherent_bar_is_accepted():
    assert bar(0).closed_at == ANCHOR + timedelta(hours=1)


@pytest.mark.parametrize(
    "broken",
    [
        {"low": Decimal("1.50")},  # low above high
        {"open": Decimal("2.00")},  # open outside the range
        {"close": Decimal("0.10")},  # close outside the range
        {"high": Decimal("0.50")},  # high below the low
    ],
)
def test_a_bar_whose_prices_contradict_each_other_is_refused(broken):
    with pytest.raises(ValueError):
        bar(0, **broken)


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_a_bar_cannot_carry_a_non_positive_price(price):
    with pytest.raises(ValueError):
        bar(0, low=price, open=price, close=price, high=price)


def test_a_bar_cannot_carry_negative_volume():
    with pytest.raises(ValueError):
        bar(0, volume=Decimal("-1"))


def test_a_bar_may_record_that_nobody_traded_a_thing():
    """Zero volume is a measurement. It is not the same as an absent interval."""
    assert bar(0, volume=Decimal("0")).volume == Decimal("0")


@pytest.mark.parametrize("value", [1.5, True])
def test_a_float_or_a_bool_is_never_accepted_as_a_price(value):
    with pytest.raises(ValueError):
        bar(0, open=value)


# ------------------------------------------------------- a series' rules


def test_an_ascending_series_is_accepted():
    history = series([bar(index) for index in range(5)])
    assert history.coverage == HistoryCoverage.COMPLETE
    assert history.missing_intervals == 0
    assert history.window_start == ANCHOR
    assert history.observed_at == ANCHOR + timedelta(hours=5)


def test_bars_out_of_order_are_refused_rather_than_sorted():
    """A series that reorders its own input would hide a provider defect."""
    with pytest.raises(ValueError):
        series([bar(2), bar(1), bar(0)])


def test_the_same_interval_twice_is_refused():
    with pytest.raises(ValueError):
        series([bar(0), bar(1), bar(1)])


def test_a_bar_on_the_wrong_interval_is_refused():
    with pytest.raises(ValueError):
        series([bar(0), bar(1, step=900)])


def test_an_opening_off_the_interval_grid_is_refused():
    with pytest.raises(ValueError):
        series([bar(0), bar(1).model_copy(update={"opened_at": ANCHOR + timedelta(minutes=90)})])


def test_a_series_naming_one_asset_on_both_sides_is_refused():
    with pytest.raises(ValueError):
        series([bar(0)], quote_asset_id="bsc:mainnet:0x" + "a1" * 20)


def test_a_pair_id_that_does_not_name_its_own_chain_is_refused():
    with pytest.raises(ValueError):
        series([bar(0)], pair_id="ethereum:mainnet:contract_address:0x" + "e5" * 20)


def test_an_observation_time_that_is_not_the_newest_close_is_refused():
    """The series observes the market as of its last closed bar, and only that."""
    with pytest.raises(ValueError):
        series([bar(0), bar(1)], observed_at=ANCHOR + timedelta(hours=9))


def test_a_bar_cannot_close_after_it_was_retrieved():
    with pytest.raises(ValueError):
        series([bar(0)], fetched_at=ANCHOR)


def test_coverage_cannot_be_declared_against_the_bars():
    """A short window presented as whole is the one lie this type must prevent."""
    with pytest.raises(ValueError):
        series([bar(index) for index in range(3)], requested=24, coverage=HistoryCoverage.COMPLETE)
    with pytest.raises(ValueError):
        series([bar(index) for index in range(3)], coverage=HistoryCoverage.PARTIAL)


def test_empty_coverage_and_emptiness_must_agree():
    with pytest.raises(ValueError):
        series([bar(0)], coverage=HistoryCoverage.EMPTY)
    with pytest.raises(ValueError):
        series([], coverage=HistoryCoverage.PARTIAL)


# ------------------------------------------------------------- gaps


def test_a_missing_interval_is_counted():
    history = series([bar(0), bar(1), bar(3), bar(4)], requested=5)
    assert history.missing_intervals == 1
    assert history.coverage == HistoryCoverage.PARTIAL


def test_a_single_bar_has_no_gaps_to_count():
    assert series([bar(0)]).missing_intervals == 0


def test_a_short_window_is_partial_even_with_no_internal_gap():
    history = series([bar(0), bar(1)], requested=24)
    assert history.missing_intervals == 0
    assert history.coverage == HistoryCoverage.PARTIAL


def test_coverage_for_an_empty_window_is_empty():
    assert coverage_for((), 24, STEP) == HistoryCoverage.EMPTY


# --------------------------------------------------------------- range


def test_the_range_spans_every_bar_not_just_the_closes():
    history = series([bar(0, price="1.00"), bar(1, price="1.20")])
    assert history.range_low == Decimal("0.95")
    assert history.range_high == Decimal("1.25")


def test_an_empty_series_has_no_range_window_or_observation():
    blank = empty_history(
        pair_id=PAIR,
        provider="geckoterminal",
        chain="bsc",
        network="mainnet",
        venue="pancakeswap-v3",
        base_asset_id="bsc:mainnet:0x" + "a1" * 20,
        quote_asset_id="bsc:mainnet:0x" + "b2" * 20,
        timeframe="hour",
        aggregate=1,
        requested_bars=24,
        fetched_at=ANCHOR,
        is_fixture=False,
    )
    assert blank.coverage == HistoryCoverage.EMPTY
    assert (blank.range_low, blank.range_high) == (None, None)
    assert blank.window_start is None
    assert blank.observed_at is None
    assert blank.age(ANCHOR) is None
    assert blank.is_whole is False


# ------------------------------------------------------------ freshness


def test_age_is_measured_from_the_close_not_the_fetch():
    history = series([bar(0)], fetched_at=ANCHOR + timedelta(hours=9))
    assert history.age(ANCHOR + timedelta(hours=3)) == timedelta(hours=2)


def test_a_naive_clock_cannot_be_used_to_judge_freshness():
    with pytest.raises(ValueError):
        series([bar(0)]).age(datetime(2026, 9, 12, 9))


# ----------------------------------------------------------- row parsing


def test_a_well_formed_row_becomes_a_bar():
    parsed = bar_from_row([int(ANCHOR.timestamp()), "1.0", "1.1", "0.9", "1.05", "42"], step=STEP)
    assert parsed.opened_at == ANCHOR
    assert (parsed.open, parsed.high, parsed.low, parsed.close) == (
        Decimal("1.0"),
        Decimal("1.1"),
        Decimal("0.9"),
        Decimal("1.05"),
    )
    assert parsed.volume == Decimal("42")


@pytest.mark.parametrize(
    "row",
    [
        "not a row",
        {"t": 1},
        [],
        [int(ANCHOR.timestamp()), "1.0", "1.1", "0.9", "1.05"],
        [True, "1.0", "1.1", "0.9", "1.05", "42"],
        [2**35, "1.0", "1.1", "0.9", "1.05", "42"],
        [0, "1.0", "1.1", "0.9", "1.05", "42"],
    ],
)
def test_a_row_that_is_not_the_documented_shape_is_refused(row):
    with pytest.raises(ValueError):
        bar_from_row(row, step=STEP)


# ------------------------------------------------------- the timeframes


@pytest.mark.parametrize(
    ("timeframe", "aggregate"),
    [
        ("minute", 1),
        ("minute", 5),
        ("minute", 15),
        ("hour", 1),
        ("hour", 4),
        ("hour", 12),
        ("day", 1),
    ],
)
def test_every_documented_combination_resolves(timeframe, aggregate):
    assert interval_seconds(timeframe, aggregate) > 0


def test_the_documented_aggregates_are_exactly_these():
    """Taken from the live provider contract, not from memory."""
    from src.markets.history import SUPPORTED_AGGREGATES

    assert SUPPORTED_AGGREGATES == {
        "minute": frozenset({1, 5, 15}),
        "hour": frozenset({1, 4, 12}),
        "day": frozenset({1}),
    }
