"""Three contract defects an independent review reproduced against 42b002c.

All three share a shape: the assessment looked right and was not. A digest that
could not tell two different prices apart, a freshness check measured at the
wrong instant, and an identity that bound a policy's *name* while its *content*
decided the answer.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from src.orchestration.sizing.calculator import assess_paper_sizing
from src.orchestration.sizing.canonical import lossless_decimal
from src.orchestration.sizing.models import SizingPolicySnapshot, SizingRefusal
from src.orchestration.sizing.policy import PAPER_SIZING_V1
from tests.sizing.conftest import (
    DelayedMarkets,
    MovingClock,
    RecordedMarkets,
    base_metadata,
    build_reader,
    open_case,
    record_setup,
    recorded_snapshot,
    reference_price,
    sizing_inputs,
)


def assess(now, **overrides):
    return assess_paper_sizing(**sizing_inputs(now, **overrides))


def wide(digits: int, last: str = "1") -> Decimal:
    """A price with exactly `digits` coefficient digits, ending as asked.

    The point sits after the first digit so the magnitude stays ordinary and
    only the precision is unusual, and the last digit is never a zero — one
    would be stripped as notation and shorten the coefficient.
    """
    body = ("1234567890" * (digits // 10 + 1))[: digits - 1] + last
    return Decimal(body) if digits == 1 else Decimal(f"{body[0]}.{body[1:]}")


# ============================================================ A. canonical form


def test_two_prices_that_size_differently_no_longer_share_a_digest(now):
    """The counterexample, verbatim.

    One dollar, one token costing a hair under and a hair over a dollar. The
    quantities differ in the eighteenth decimal place — a real difference the
    ledger stores — while the shared helper's precision-seventy-eight
    normalisation rounded both prices to `1` and produced one identity.
    """
    with localcontext() as arithmetic:
        arithmetic.prec = 200
        low = Decimal(1) - Decimal(10) ** -90
        high = Decimal(1) + Decimal(10) ** -90

    under = assess(now, requested_notional_usd=Decimal("1"), price=reference_price(now, value=low))
    over = assess(now, requested_notional_usd=Decimal("1"), price=reference_price(now, value=high))

    assert under.quantity == Decimal("1.000000000000000000")
    assert over.quantity == Decimal("0.999999999999999999")
    assert under.quantity != over.quantity
    assert under.input_digest != over.input_digest


@pytest.mark.parametrize("digits", [1, 2, 18, 28, 29, 78, 79, 99, 100])
def test_no_two_distinct_prices_collide_up_to_a_hundred_digits(now, digits):
    """The market layer permits a hundred coefficient digits, so all hundred count."""
    first = assess(now, price=reference_price(now, value=wide(digits, last="1")))
    second = assess(now, price=reference_price(now, value=wide(digits, last="2")))
    assert first.reference_price.usd_per_base_unit != second.reference_price.usd_per_base_unit
    assert first.input_digest != second.input_digest


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("500", "500.00"),
        ("500", "5E+2"),
        ("5E+2", "500.000000"),
        ("0.1", "0.10"),
        ("0.1", "1E-1"),
        ("1234.5", "1.2345E+3"),
    ],
)
def test_one_value_written_two_ways_is_one_reading(now, left, right):
    """Notation is not value. Stripping trailing zeros must stay lossless."""
    assert lossless_decimal(Decimal(left)) == lossless_decimal(Decimal(right))
    first = assess(now, price=reference_price(now, value=Decimal(left)))
    second = assess(now, price=reference_price(now, value=Decimal(right)))
    assert first.input_digest == second.input_digest


@pytest.mark.parametrize("precision", [1, 7, 28, 78, 200])
def test_the_identity_does_not_depend_on_the_caller_s_decimal_context(now, precision):
    """The old helper normalised inside a context; this one performs no arithmetic.

    A global precision a caller happened to set must not decide what two prices
    hash to — that is a rounding rule reaching the identity from outside it.
    """
    with localcontext() as arithmetic:
        arithmetic.prec = 200
        value = Decimal(1) + Decimal(10) ** -90
    expected = lossless_decimal(value)

    with localcontext() as arithmetic:
        arithmetic.prec = precision
        assert lossless_decimal(value) == expected
        assert (
            assess(now, price=reference_price(now, value=value)).input_digest
            == assess(now, price=reference_price(now, value=value)).input_digest
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", "0"), ("-0", "0"), ("0.00", "0"), ("-1.500", "-1.5"), ("1E+3", "1000")],
)
def test_the_formatter_writes_one_plain_form_per_value(value, expected):
    assert lossless_decimal(Decimal(value)) == expected


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_the_formatter_refuses_what_has_no_canonical_form(value):
    with pytest.raises(ValueError, match="finite"):
        lossless_decimal(Decimal(value))


# ====================================================== B. freshness after reads


async def test_time_passing_during_the_reads_is_counted(worker_db, now, trace):
    """The reproduction: an eighty-nine-second price and a two-second read.

    `now` used to be taken before the case read and the market read, so a price
    that was fresh when the work started stayed "fresh" after the work had
    carried it past the policy window — and the assessment came back already
    past its own `valid_until`.
    """
    _, sessions = worker_db
    clock = MovingClock(now)
    snapshot = recorded_snapshot(now, age=timedelta(seconds=89))
    feed = DelayedMarkets(snapshot, clock, timedelta(seconds=2))
    reader = build_reader(sessions, now, feed=feed, clock=clock)
    trade_case = await open_case(reader.cases, now, trace)
    await record_setup(reader.cases, trade_case, now)

    reading = await reader.sizing(trade_case.id)

    assert clock.instant == now + timedelta(seconds=2)
    assert reading.reason == SizingRefusal.SIZING_PRICE_STALE


async def test_a_reading_that_survives_the_reads_is_current_when_it_returns(worker_db, now, trace):
    """The other half: success must still be usable at the instant of return."""
    _, sessions = worker_db
    clock = MovingClock(now)
    snapshot = recorded_snapshot(now, age=timedelta(seconds=80))
    feed = DelayedMarkets(snapshot, clock, timedelta(seconds=2))
    reader = build_reader(sessions, now, feed=feed, clock=clock)
    trade_case = await open_case(reader.cases, now, trace)
    await record_setup(reader.cases, trade_case, now)

    reading = await reader.sizing(trade_case.id)

    assert reading.kind == "sizing_assessment"
    assert reading.is_current_at(clock.instant)
    assert clock.instant > now


@pytest.mark.parametrize(
    ("age_seconds", "delay_seconds", "expected"),
    [
        # Lands one microsecond inside the window.
        (89, 0, "sizing_assessment"),
        # Lands exactly on `valid_until`, which belongs to the expired side.
        (88, 2, "stale"),
        # Lands past it.
        (88, 3, "stale"),
    ],
)
async def test_the_boundary_is_measured_at_the_instant_of_use(
    worker_db, now, trace, age_seconds, delay_seconds, expected
):
    _, sessions = worker_db
    clock = MovingClock(now)
    feed = DelayedMarkets(
        recorded_snapshot(now, age=timedelta(seconds=age_seconds)),
        clock,
        timedelta(seconds=delay_seconds),
    )
    reader = build_reader(sessions, now, feed=feed, clock=clock)
    trade_case = await open_case(reader.cases, now, trace)
    await record_setup(reader.cases, trade_case, now)

    reading = await reader.sizing(trade_case.id)

    if expected == "stale":
        assert reading.reason == SizingRefusal.SIZING_PRICE_STALE
    else:
        assert reading.kind == "sizing_assessment"
        assert reading.is_current_at(clock.instant)


async def test_an_observation_that_moves_into_the_future_is_refused(worker_db, now, trace):
    """Backwards time is not staleness, and is not silently tolerated either."""
    _, sessions = worker_db
    clock = MovingClock(now)
    feed = RecordedMarkets(recorded_snapshot(now, age=timedelta(seconds=-5)))
    reader = build_reader(sessions, now, feed=feed, clock=clock)
    trade_case = await open_case(reader.cases, now, trace)
    await record_setup(reader.cases, trade_case, now)

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_PRICE_NOT_YET_OBSERVED


async def test_the_reader_takes_no_clock_from_its_caller(worker_db, now, trace):
    """One trusted clock, injected at construction, never passed in per call."""
    import inspect

    from src.orchestration.sizing.context import PaperSizingReader

    parameters = set(inspect.signature(PaperSizingReader.sizing).parameters)
    assert parameters == {"self", "trade_case_id"}


# ========================================================= C. the actual policy


def test_the_same_version_with_a_different_window_is_a_different_reading(now):
    """The reproduction: one label, two freshness bounds, two validity windows.

    Binding only the version made those identical readings with different
    expiries — a policy change nothing could detect.
    """
    longer = replace(PAPER_SIZING_V1, max_price_age=timedelta(seconds=180))
    short = assess(now)
    long = assess(now, policy=longer)

    assert short.policy_version == long.policy_version == "paper-sizing-v1"
    assert short.valid_until != long.valid_until
    assert short.input_digest != long.input_digest


@pytest.mark.parametrize(
    "change",
    [
        {"version": "paper-sizing-v2"},
        {"max_price_age": timedelta(seconds=91)},
        {"max_price_age": timedelta(seconds=90, microseconds=1)},
        {"max_quantity_decimal_places": 17},
        {"max_quantity_total_digits": 37},
    ],
)
def test_every_bound_policy_parameter_changes_the_identity(now, change):
    """Each parameter that can change a result or a validity is in the digest."""
    variant = replace(PAPER_SIZING_V1, **change)
    assert assess(now).input_digest != assess(now, policy=variant).input_digest


def test_the_side_and_mode_sets_are_bound_too(now):
    """They decide whether a reading happens at all, so they are part of it."""
    from src.core.models import Side, TradingMode

    variants = (
        replace(PAPER_SIZING_V1, supported_sides=frozenset({Side.BUY, Side.SELL})),
        replace(
            PAPER_SIZING_V1, supported_modes=frozenset({TradingMode.PAPER, TradingMode.OBSERVE})
        ),
    )
    for variant in variants:
        assert assess(now).input_digest != assess(now, policy=variant).input_digest


def test_the_policy_travels_on_the_reading_itself(now):
    """Recorded, not merely hashed: a reader can check what produced this."""
    reading = assess(now)
    assert reading.policy == SizingPolicySnapshot.of(PAPER_SIZING_V1)
    assert reading.policy.max_price_age_microseconds == 90_000_000
    assert (
        reading.valid_until - reading.reference_price.observed_at == PAPER_SIZING_V1.max_price_age
    )


def test_a_refusal_records_what_it_was_measured_against(now):
    """A stale price is stale against a tolerance, so the tolerance travels."""
    refusal = assess(now, price=reference_price(now, age=timedelta(seconds=120)))
    assert refusal.reason == SizingRefusal.SIZING_PRICE_STALE
    assert refusal.policy.max_price_age_microseconds == 90_000_000


def test_the_bound_policy_is_ordered_rather_than_set_shaped(now):
    """Frozensets iterate in whatever order they like; the hash must not."""
    from src.core.models import Side, TradingMode

    forward = replace(
        PAPER_SIZING_V1,
        supported_sides=frozenset({Side.BUY, Side.SELL}),
        supported_modes=frozenset({TradingMode.PAPER, TradingMode.OBSERVE}),
    )
    snapshot = SizingPolicySnapshot.of(forward)
    assert snapshot.supported_sides == (Side.BUY, Side.SELL)
    assert snapshot.supported_modes == (TradingMode.OBSERVE, TradingMode.PAPER)


def test_read_time_and_the_derived_quantity_stay_out_of_the_identity(now):
    """Unchanged by this round, and asserted again because it is easy to lose."""
    inputs = sizing_inputs(now)
    first = assess_paper_sizing(**inputs)
    later = assess_paper_sizing(**{**inputs, "now": now + timedelta(seconds=10)})
    assert first.input_digest == later.input_digest

    assert set(SizingPolicySnapshot.of(PAPER_SIZING_V1).canonical) == {
        "version",
        "max_price_age_microseconds",
        "supported_sides",
        "supported_modes",
        "max_quantity_decimal_places",
        "max_quantity_total_digits",
    }


def test_a_differently_configured_amount_is_still_a_different_reading(now):
    """The pre-existing binding, re-checked beside the new policy binding."""
    assert (
        assess(now).input_digest
        != assess(now, requested_notional_usd=Decimal("500.000000000000000001")).input_digest
    )


def test_metadata_provenance_is_still_bound(now):
    other = base_metadata(now, observation="different-source")
    assert assess(now).input_digest != assess(now, base_asset=other).input_digest
