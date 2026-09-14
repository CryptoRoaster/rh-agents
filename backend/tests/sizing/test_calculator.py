"""The arithmetic, and every way of refusing to do it.

One theme runs through all of it: a size that cannot be computed honestly is not
computed at all. There is no fallback amount anywhere in this module's tests,
because there is none in the module.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import Side, TradingMode
from src.orchestration.sizing.calculator import assess_paper_sizing
from src.orchestration.sizing.models import SizingOutcome, SizingRefusal
from src.orchestration.sizing.policy import PAPER_SIZING_V1
from tests.sizing.conftest import (
    BASE_ASSET,
    base_metadata,
    reference_price,
    sizing_inputs,
)


def assess(now, **overrides):
    return assess_paper_sizing(**sizing_inputs(now, **overrides))


# ---------------------------------------------------------------- configuration


def test_an_unconfigured_amount_is_the_standing_gap(now):
    """The default, and the only honest one.

    Reported with the same code the control plane already reports, so the two
    cannot drift into two different names for one missing number.
    """
    reading = assess(now, requested_notional_usd=None)
    assert reading.kind == "sizing_refused"
    assert reading.reason == SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING
    assert reading.policy_version == "paper-sizing-v1"


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("-0.000000000000000001")])
def test_a_non_positive_amount_buys_nothing(now, amount):
    """Configuration refuses these at boot; the function refuses them again.

    Defence in depth on the one number that says how much money to ask for.
    """
    reading = assess(now, requested_notional_usd=amount)
    assert reading.reason == SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_amount_buys_nothing(now, amount):
    reading = assess(now, requested_notional_usd=Decimal(amount))
    assert reading.reason == SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING


def test_only_paper_mode_sizes_anything(now):
    """OBSERVE means the deployment watches. A watcher does not ask for a size."""
    for mode in (TradingMode.OBSERVE, TradingMode.LIVE_AUTONOMOUS):
        assert assess(now, trading_mode=mode).reason == SizingRefusal.SIZING_MODE_NOT_SUPPORTED


def test_a_stopped_deployment_outranks_a_missing_amount(now):
    """Order matters: fix the stop, not the knob.

    A deployment in OBSERVE with nothing configured is both stopped and
    unconfigured. Reporting the knob would send an operator to set a number that
    still changes nothing.
    """
    reading = assess(now, trading_mode=TradingMode.OBSERVE, requested_notional_usd=None)
    assert reading.reason == SizingRefusal.SIZING_MODE_NOT_SUPPORTED


def test_only_long_entries_are_sized(now):
    """VECTOR proposes BUY exclusively and the paper ledger is long-only.

    A short entry here would describe something no other component can carry out.
    """
    assert assess(now, side=Side.SELL).reason == SizingRefusal.SIZING_SIDE_NOT_SUPPORTED


# ------------------------------------------------------------- asset and price


def test_the_price_must_price_this_case_s_base_asset(now):
    """A price from the wrong side of a pair is wrong by the exchange rate.

    It is also perfectly plausible-looking, which is why identity is checked
    rather than assumed from the fact that a price arrived at all.
    """
    quote_side = reference_price(now, asset_id="ethereum:mainnet:0xfixture-usdc")
    assert assess(now, price=quote_side).reason == SizingRefusal.SIZING_PRICE_ASSET_MISMATCH


def test_metadata_must_describe_this_case_s_base_asset(now):
    other = base_metadata(now, asset_id="ethereum:mainnet:0xfixture-usdc")
    assert assess(now, base_asset=other).reason == SizingRefusal.SIZING_TOKEN_METADATA_MISSING


def test_an_absent_price_buys_nothing(now):
    assert assess(now, price=None).reason == SizingRefusal.SIZING_PRICE_UNAVAILABLE


def test_the_price_is_usd_per_whole_base_token(now):
    """The dimension, asserted rather than assumed.

    Five hundred dollars at roughly two thousand three hundred a token buys
    about a fifth of one. A per-base-unit price mistaken for a per-base-unit-of-
    account price would be off by ten to the eighteenth.
    """
    reading = assess(now)
    assert reading.kind == "sizing_assessment"
    assert reading.reference_price.price_basis == "USD_PER_BASE_UNIT"
    assert Decimal("0.2") < reading.quantity < Decimal("0.22")


# ------------------------------------------------------------------- freshness


def test_a_price_just_inside_the_boundary_is_usable(now):
    edge = PAPER_SIZING_V1.max_price_age - timedelta(microseconds=1)
    reading = assess(now, price=reference_price(now, age=edge))
    assert reading.kind == "sizing_assessment"
    assert reading.is_current_at(now)


def test_a_price_exactly_at_the_freshness_boundary_is_already_stale(now):
    """The boundary instant belongs to the expired side.

    Half-open, like every other validity in this system: an evidence envelope
    is `STALE` at `now >= valid_until`, and a COMMANDER context is current only
    while `instant < valid_until`. Succeeding here would have produced a reading
    whose own `valid_until` equalled the instant it was made at — an assessment
    and a usability check contradicting each other at the same moment.
    """
    reading = assess(now, price=reference_price(now, age=PAPER_SIZING_V1.max_price_age))
    assert reading.reason == SizingRefusal.SIZING_PRICE_STALE


def test_a_price_just_past_the_boundary_is_stale(now):
    edge = PAPER_SIZING_V1.max_price_age + timedelta(microseconds=1)
    reading = assess(now, price=reference_price(now, age=edge))
    assert reading.reason == SizingRefusal.SIZING_PRICE_STALE


@pytest.mark.parametrize("microseconds", [0, 1, 1000, 30_000_000, 89_999_999])
def test_a_successful_reading_is_always_still_usable_when_it_is_made(now, microseconds):
    """Success and usability may never disagree, at any age inside the window."""
    age = timedelta(microseconds=microseconds)
    reading = assess(now, price=reference_price(now, age=age))
    assert reading.kind == "sizing_assessment"
    assert reading.is_current_at(now)
    assert not reading.is_current_at(reading.valid_until)
    assert reading.is_current_at(reading.valid_until - timedelta(microseconds=1))


def test_a_price_observed_in_the_future_is_not_merely_stale(now):
    """A recorder that reports the future cannot be trusted about any instant.

    Kept as its own reason because the remedy differs: a stale price waits for
    the next observation, and this one waits for somebody to fix a clock.
    """
    ahead = reference_price(now, age=-timedelta(microseconds=1))
    reading = assess(now, price=ahead)
    assert reading.reason == SizingRefusal.SIZING_PRICE_NOT_YET_OBSERVED


# -------------------------------------------------------------------- metadata


def test_unknown_decimals_refuse_rather_than_assume_eighteen(now):
    """The mistake that turns a hundred dollars into a hundred trillion.

    Nothing recorded the token's decimals, so nothing is known about them, and
    a convention is not knowledge.
    """
    reading = assess(now, base_asset=None)
    assert reading.reason == SizingRefusal.SIZING_TOKEN_METADATA_MISSING


# -------------------------------------------------------- rounding and limits


@pytest.mark.parametrize(
    ("notional", "price", "decimals", "quantity"),
    [
        # A six-decimal token: the classic decimals trap, sized correctly.
        ("100", "1.5", 6, "66.666666"),
        # A zero-decimal token cannot be subdivided at all.
        ("100", "3", 0, "33"),
        # Exact division stays exact rather than acquiring a rounding artefact.
        ("100", "2.5", 18, "40"),
        # One whole unit of an expensive six-decimal token.
        ("1", "1000000", 6, "0.000001"),
    ],
)
def test_the_quantity_is_rounded_down_to_the_token_s_own_unit(
    now, notional, price, decimals, quantity
):
    reading = assess(
        now,
        requested_notional_usd=Decimal(notional),
        price=reference_price(now, value=Decimal(price)),
        base_asset=base_metadata(now, decimals=decimals),
    )
    assert reading.kind == "sizing_assessment"
    assert reading.quantity == Decimal(quantity)
    assert reading.quantity_decimal_places == decimals


@pytest.mark.parametrize(
    ("notional", "price", "decimals"),
    [
        ("500", "2345.123456789012345678", 18),
        ("100", "1.5", 6),
        ("100", "3", 0),
        ("10000", "0.000000000001", 18),
        ("1", "0.7", 12),
        ("12345.678901234567890123", "9.87654321", 9),
    ],
)
def test_the_sized_value_never_exceeds_the_requested_notional(now, notional, price, decimals):
    """The arithmetic guarantee rounding down exists to provide.

    Checked on the exact product rather than on a rounded copy of it: the
    inequality is the whole point, and a comparison that rounded first would be
    checking something weaker than the claim.
    """
    requested = Decimal(notional)
    reading = assess(
        now,
        requested_notional_usd=requested,
        price=reference_price(now, value=Decimal(price)),
        base_asset=base_metadata(now, decimals=decimals),
    )
    assert reading.kind == "sizing_assessment"
    assert reading.quantity * reading.reference_price.usd_per_base_unit <= requested
    assert reading.reference_notional_usd <= requested


def test_a_token_finer_than_the_ledger_is_rounded_to_what_can_be_stored(now):
    """Twenty-four decimals, stored in eighteen, and the difference is recorded.

    Rounding to the coarser unit is still rounding down, so the guarantee holds.
    What must not happen is doing it silently: the assessment carries both the
    token's own decimals and the places actually used, so a reader can see that
    the result is not expressed in the token's smallest unit.
    """
    reading = assess(now, base_asset=base_metadata(now, decimals=24))
    assert reading.kind == "sizing_assessment"
    assert reading.base_asset.decimals == 24
    assert reading.quantity_decimal_places == 18
    assert -reading.quantity.as_tuple().exponent == 18


def test_an_amount_below_one_unit_buys_nothing(now):
    """Zero tokens is not a size, so it is refused rather than returned."""
    reading = assess(
        now,
        requested_notional_usd=Decimal("1"),
        price=reference_price(now, value=Decimal("3")),
        base_asset=base_metadata(now, decimals=0),
    )
    assert reading.reason == SizingRefusal.SIZING_BELOW_MINIMUM_UNIT


def test_a_quantity_too_large_for_the_ledger_is_refused(now):
    """`Numeric(38, 18)` leaves twenty digits left of the point.

    A cheap enough token puts the quantity past that. Truncating the magnitude
    to make it fit would size something else entirely, so it is refused.
    """
    reading = assess(
        now,
        requested_notional_usd=Decimal("10000"),
        price=reference_price(now, value=Decimal("0.0000000000000000000001")),
    )
    assert reading.reason == SizingRefusal.SIZING_QUANTITY_NOT_REPRESENTABLE


def test_a_quantity_at_the_representable_edge_is_still_computed(now):
    """The bound refuses what cannot be stored, not what is merely large."""
    reading = assess(
        now,
        requested_notional_usd=Decimal("10000000000000000000"),
        price=Decimal("1") and reference_price(now, value=Decimal("1")),
    )
    assert reading.kind == "sizing_assessment"
    assert reading.quantity == Decimal("10000000000000000000")


def test_a_value_below_the_ledger_s_smallest_amount_is_refused(now):
    """A quantity whose worth floors to zero has no notional to report."""
    reading = assess(
        now,
        requested_notional_usd=Decimal("0.000000000000000001"),
        price=reference_price(now, value=Decimal("0.6")),
    )
    assert reading.reason == SizingRefusal.SIZING_BELOW_MINIMUM_UNIT


# ------------------------------------------------------------------- validity


def test_validity_is_anchored_to_the_observation_not_to_the_reading(now):
    reading = assess(now)
    assert (
        reading.valid_until == reading.reference_price.observed_at + PAPER_SIZING_V1.max_price_age
    )


def test_recomputing_does_not_make_a_source_younger(now):
    """Reading a price again does not refresh it.

    An earlier phase of this system found exactly this defect in ANCHOR, where a
    validity anchored to run time let a stale reference launder itself into a
    current one on every recomputation.
    """
    price = reference_price(now, age=timedelta(seconds=30))
    first = assess(now, price=price)
    later = assess(now + timedelta(seconds=45), price=price)
    assert first.kind == later.kind == "sizing_assessment"
    assert first.valid_until == later.valid_until
    assert later.valid_until < now + timedelta(seconds=45) + PAPER_SIZING_V1.max_price_age


def test_the_successful_outcome_says_only_that_an_input_exists(now):
    """`SIZING_INPUT_AVAILABLE`, and nothing that could be read as permission."""
    reading = assess(now)
    assert reading.outcome is SizingOutcome.SIZING_INPUT_AVAILABLE
    assert [item.value for item in SizingOutcome] == ["SIZING_INPUT_AVAILABLE"]


def test_a_refusal_still_names_the_case_it_refused(now):
    case_id = uuid4()
    reading = assess(now, trade_case_id=case_id, price=None)
    assert reading.trade_case_id == case_id
    assert reading.base_asset_id == BASE_ASSET
