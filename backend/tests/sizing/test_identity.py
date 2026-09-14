"""What identifies a reading, and what deliberately does not.

The digest answers one question: *were these the same inputs?* It is not an
execution key. Nothing exactly-once rests on it, no order is deduplicated by it,
and the durable link from a case to a fill does not exist yet — so a test that
treated it as an idempotency key would be asserting a guarantee this phase has
not made.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from src.orchestration.sizing.calculator import assess_paper_sizing
from src.orchestration.sizing.models import sizing_input_digest
from src.orchestration.sizing.policy import PAPER_SIZING_V1
from tests.sizing.conftest import base_metadata, reference_price, sizing_inputs


def assess(now, **overrides):
    return assess_paper_sizing(**sizing_inputs(now, **overrides))


def test_the_same_inputs_give_the_same_digest_and_the_same_content(now):
    """Replay, as the only thing that makes a recomputation checkable."""
    inputs = sizing_inputs(now)
    first = assess_paper_sizing(**inputs)
    second = assess_paper_sizing(**inputs)
    assert first.input_digest == second.input_digest
    assert first.model_dump_json() == second.model_dump_json()


def test_reading_at_a_different_instant_changes_nothing(now):
    """No read time in the identity.

    Two readings seconds apart from one unchanged observation are the same
    reading. If the clock reached the digest, every recomputation would look
    like a different assessment and replay could never be recognised.
    """
    inputs = sizing_inputs(now)
    first = assess_paper_sizing(**inputs)
    later = assess_paper_sizing(**{**inputs, "now": now + timedelta(seconds=20)})
    assert first.input_digest == later.input_digest
    assert first.model_dump_json() == later.model_dump_json()


def test_a_changed_price_is_a_different_reading(now):
    moved = reference_price(now, value=Decimal("2345.123456789012345679"))
    assert assess(now).input_digest != assess(now, price=moved).input_digest


def test_a_price_from_a_different_observation_is_a_different_reading(now):
    """Same number, different source. The provenance is part of the identity.

    Two observations agreeing on a price are still two observations, and a
    reading that could not tell them apart would claim to have been derived from
    whichever one somebody later assumed.
    """
    first = assess(now)
    again = assess(now, price=reference_price(now, observation="second-reading"))
    assert first.quantity == again.quantity
    assert first.input_digest != again.input_digest


def test_a_changed_decimals_figure_is_a_different_reading(now):
    assert (
        assess(now).input_digest
        != assess(now, base_asset=base_metadata(now, decimals=12)).input_digest
    )


def test_a_changed_notional_is_a_different_reading(now):
    assert (
        assess(now).input_digest != assess(now, requested_notional_usd=Decimal("501")).input_digest
    )


def test_a_changed_policy_is_a_different_reading(now):
    """A different rule produced it, so it is not the same reading.

    Versioning the policy into the identity is what stops a figure computed
    under one set of bounds from being mistaken for one computed under another.
    """
    variant = replace(PAPER_SIZING_V1, version="paper-sizing-test-v2")
    first, second = assess(now), assess(now, policy=variant)
    assert first.quantity == second.quantity
    assert first.input_digest != second.input_digest
    assert second.policy_version == "paper-sizing-test-v2"


def test_a_changed_case_or_setup_is_a_different_reading(now):
    from uuid import uuid4

    base = sizing_inputs(now)
    assert (
        assess_paper_sizing(**base).input_digest
        != assess_paper_sizing(**{**base, "trade_case_id": uuid4()}).input_digest
    )
    assert (
        assess_paper_sizing(**base).input_digest
        != assess_paper_sizing(**{**base, "setup_evidence_id": uuid4()}).input_digest
    )


def test_the_digest_does_not_depend_on_how_an_instant_was_written(now):
    """One instant, one textual form, whatever offset it arrived in."""
    from datetime import timezone

    price = reference_price(now)
    shifted = price.model_copy(
        update={
            "observed_at": price.observed_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
        }
    )
    assert assess(now, price=price).input_digest == assess(now, price=shifted).input_digest


def test_the_digest_does_not_contain_the_quantity_it_explains(now):
    """The output is not part of the identity of the inputs.

    A digest that included the derived quantity could never detect a
    computation that changed while everything it reads stayed the same — which
    is the one change this identity most needs to expose.
    """
    reading = assess(now)
    recomputed = sizing_input_digest(
        policy_version=reading.policy_version,
        trade_case_id=reading.trade_case_id,
        base_asset_id=reading.base_asset_id,
        setup_evidence_id=reading.setup_evidence_id,
        side=reading.side,
        trading_mode=reading.trading_mode,
        requested_notional_usd=reading.requested_notional_usd,
        price=reading.reference_price,
        base_asset=reading.base_asset,
        quantity_decimal_places=reading.quantity_decimal_places,
    )
    assert recomputed == reading.input_digest


def test_equal_decimals_written_differently_hash_the_same(now):
    """`500` and `500.00` are one amount, so they are one reading."""
    plain = assess(now, requested_notional_usd=Decimal("500"))
    padded = assess(now, requested_notional_usd=Decimal("500.00"))
    assert plain.input_digest == padded.input_digest
