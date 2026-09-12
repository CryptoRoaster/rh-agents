"""The deterministic setup validator: what a proposal must survive to become one.

This is the load-bearing file of the phase. VECTOR is the first specialist whose
output describes an action, so the question is not whether a model can produce a
plausible setup but what happens when it produces an implausible one — and the
answer everywhere below is that the proposal is refused, never quietly corrected.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.vector.models import (
    ObservedMeasurement,
    SetupKind,
    TriggerCondition,
    TriggerType,
    VectorReasonCode,
    VectorSetupProposal,
)
from src.agents.vector.policy import REQUIRED_TRIGGER, VECTOR_SETUP_V1
from src.agents.vector.validation import (
    VectorValidationError,
    trigger_for,
    validate_proposal,
)
from src.core.models import Side
from src.markets.models import Availability
from tests.vector.conftest import SPOT, breakout, pullback, stable_id, task_input

# ---------------------------------------------------------- coherent setups


def test_a_coherent_breakout_is_accepted(now):
    validate_proposal(breakout(now), task_input(now))


def test_a_coherent_pullback_is_accepted(now):
    validate_proposal(pullback(now), task_input(now))


def test_the_trigger_follows_from_the_geometry(now):
    """A setup cannot describe one thing and be watched for another."""
    breakout_trigger = trigger_for(breakout(now), task_input(now))
    assert breakout_trigger.type == TriggerType.PRICE_GTE
    assert breakout_trigger.reference_price == Decimal("1.10")
    assert breakout_trigger.zone_low is None

    zone_trigger = trigger_for(pullback(now), task_input(now))
    assert zone_trigger.type == TriggerType.PRICE_IN_RANGE
    assert (zone_trigger.zone_low, zone_trigger.zone_high) == (Decimal("0.90"), Decimal("0.95"))
    assert zone_trigger.reference_price is None


@pytest.mark.parametrize(
    ("price", "met"),
    [(Decimal("1.09"), False), (Decimal("1.10"), True), (Decimal("1.50"), True)],
)
def test_a_breakout_trigger_is_a_plain_comparison(now, price, met):
    """Evaluable by a future PULSE without a model, which is the whole point."""
    assert trigger_for(breakout(now), task_input(now)).is_met(price) is met


@pytest.mark.parametrize(
    ("price", "met"),
    [
        (Decimal("0.89"), False),
        (Decimal("0.90"), True),
        (Decimal("0.95"), True),
        (Decimal("0.96"), False),
    ],
)
def test_a_zone_trigger_is_a_plain_comparison(now, price, met):
    assert trigger_for(pullback(now), task_input(now)).is_met(price) is met


# ------------------------------------------------------------ geometry


def test_an_invalidation_above_the_entry_is_refused_not_reordered(now):
    """Scenario B. Silent repair would attribute a setup to a model that never made it."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, invalidation_price=Decimal("1.20")), task_input(now))
    assert error.value.reason_code == "INVALIDATION_NOT_BELOW_ENTRY"


def test_an_invalidation_equal_to_the_entry_is_refused(now):
    with pytest.raises(VectorValidationError):
        validate_proposal(breakout(now, invalidation_price=Decimal("1.10")), task_input(now))


def test_a_pullback_invalidation_must_sit_below_the_whole_band(now):
    """Inside the band the idea has not failed; it has merely been entered."""
    with pytest.raises(VectorValidationError):
        validate_proposal(pullback(now, invalidation_price=Decimal("0.92")), task_input(now))


def test_unordered_targets_are_refused_not_sorted(now):
    """Scenario C. Sorting would be a different proposal wearing the same name."""
    with pytest.raises(ValueError):
        breakout(now, targets=(Decimal("1.20"), Decimal("1.10"), Decimal("1.30")))


def test_duplicate_targets_are_refused(now):
    with pytest.raises(ValueError):
        breakout(now, targets=(Decimal("1.20"), Decimal("1.20")))


def test_a_target_below_the_entry_is_refused(now):
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, targets=(Decimal("1.05"),)), task_input(now))
    assert error.value.reason_code == "TARGET_NOT_ABOVE_ENTRY"


def test_a_breakout_with_a_band_is_not_a_breakout(now):
    """One level being crossed, not a range being entered."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(
            breakout(now, entry_low=Decimal("1.05"), entry_high=Decimal("1.10")),
            task_input(now),
        )
    assert error.value.reason_code == "BREAKOUT_REQUIRES_A_SINGLE_LEVEL"


def test_an_inverted_entry_band_cannot_be_constructed(now):
    with pytest.raises(ValueError):
        pullback(now, entry_low=Decimal("0.95"), entry_high=Decimal("0.90"))


def test_more_targets_than_policy_allows_are_refused(now):
    with pytest.raises(ValueError):
        breakout(
            now,
            targets=(Decimal("1.2"), Decimal("1.3"), Decimal("1.4"), Decimal("1.5")),
        )


# --------------------------------------------------------- price envelope


def test_an_absurd_entry_is_refused(now):
    """Scenario D. Observed 1.00, proposed 1000000 — a different asset, not a view."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(
            breakout(
                now,
                entry_low=Decimal("1000000"),
                entry_high=Decimal("1000000"),
                targets=(Decimal("2000000"),),
            ),
            task_input(now),
        )
    assert error.value.reason_code == "LEVEL_OUTSIDE_PRICE_ENVELOPE"


def test_a_lost_decimal_point_downward_is_refused(now):
    with pytest.raises(VectorValidationError):
        validate_proposal(
            breakout(
                now,
                entry_low=Decimal("0.011"),
                entry_high=Decimal("0.011"),
                invalidation_price=Decimal("0.001"),
                targets=(Decimal("0.02"),),
            ),
            task_input(now),
        )


def test_an_aggressive_but_sane_setup_is_not_refused(now):
    """The envelope is a typo filter, not a view on what price is reasonable."""
    validate_proposal(
        breakout(
            now,
            entry_low=Decimal("1.10"),
            entry_high=Decimal("1.10"),
            invalidation_price=Decimal("0.80"),
            targets=(Decimal("2.00"), Decimal("3.50")),
        ),
        task_input(now),
    )


def test_the_envelope_is_computed_from_the_observed_price(now):
    low, high = VECTOR_SETUP_V1.envelope(SPOT)
    assert low == Decimal("0.25")
    assert high == Decimal("4")


def test_a_level_exactly_on_the_envelope_edge_is_allowed(now):
    validate_proposal(
        breakout(
            now,
            entry_low=Decimal("1.10"),
            entry_high=Decimal("1.10"),
            invalidation_price=Decimal("0.25"),
            targets=(Decimal("4"),),
        ),
        task_input(now),
    )


# ----------------------------------------------------------------- expiry


def test_an_expiry_beyond_the_horizon_is_refused_not_clamped(now):
    """Scenario J. A setup that wanted a week is not the same as one bounded to four hours."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, expires_at=now + timedelta(days=7)), task_input(now))
    assert error.value.reason_code == "SETUP_LIFETIME_TOO_LONG"


def test_an_expiry_already_upon_us_is_refused(now):
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, expires_at=now + timedelta(seconds=30)), task_input(now))
    assert error.value.reason_code == "SETUP_LIFETIME_TOO_SHORT"


def test_every_setup_expires(now):
    """No immortal proposals: the field is required and bounded on both sides."""
    assert "expires_at" in VectorSetupProposal.model_fields
    assert VECTOR_SETUP_V1.min_setup_lifetime == timedelta(minutes=5)
    assert VECTOR_SETUP_V1.max_setup_lifetime == timedelta(hours=4)


# ------------------------------------------------------------ references


def test_an_observation_that_was_never_shown_cannot_be_cited(now):
    """Scenario I. Invented market data, however plausible the identifier."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, cited_observation_ids=(uuid4(),)), task_input(now))
    assert error.value.reason_code == "UNKNOWN_OBSERVATION_REFERENCE"


def test_evidence_that_was_never_supplied_cannot_be_cited(now):
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, cited_evidence_ids=(uuid4(),)), task_input(now))
    assert error.value.reason_code == "UNKNOWN_EVIDENCE_REFERENCE"


def test_citing_what_was_shown_is_accepted(now):
    validate_proposal(
        breakout(now, cited_observation_ids=(stable_id("price"), stable_id("liquidity"))),
        task_input(now),
    )


# ----------------------------------------------------------------- scope


def test_a_short_setup_is_refused_because_the_system_cannot_take_one(now):
    """The paper execution service is long-only; a short would describe a fiction."""
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now, side=Side.SELL), task_input(now))
    assert error.value.reason_code == "UNSUPPORTED_SIDE"


def test_the_policy_supports_exactly_the_two_long_shapes():
    assert VECTOR_SETUP_V1.supported_sides == frozenset({Side.BUY})
    assert VECTOR_SETUP_V1.supported_kinds == frozenset(
        {SetupKind.BREAKOUT_LONG, SetupKind.PULLBACK_LONG}
    )


def test_the_policy_is_versioned():
    assert VECTOR_SETUP_V1.version == "vector-setup-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("supported_sides", frozenset()),
        ("supported_kinds", frozenset()),
        ("min_setup_lifetime", timedelta(0)),
        ("max_setup_lifetime", timedelta(minutes=1)),
        ("max_targets", 0),
        ("max_targets", 9),
        ("max_level_multiple", Decimal("0.5")),
        ("min_level_fraction", Decimal("2")),
        ("max_input_age", timedelta(0)),
    ],
)
def test_an_incoherent_policy_refuses_to_exist(field, value):
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(VECTOR_SETUP_V1, **{field: value})


# --------------------------------------------- what the schema cannot say


@pytest.mark.parametrize(
    "forbidden",
    [
        "position_size",
        "notional_usd",
        "quantity",
        "portfolio_fraction",
        "slippage_bps",
        "route",
        "venue_preference",
        "gas_price",
        "risk_outcome",
        "approved",
    ],
)
def test_the_schema_has_no_field_for_execution_or_approval(forbidden):
    """Scenario H, expressed as a schema rather than an instruction."""
    assert forbidden not in VectorSetupProposal.model_fields


def test_a_proposal_carrying_a_position_size_is_a_parse_error(now):
    """Not a field somebody downstream might read — an error at the boundary."""
    with pytest.raises(ValueError):
        VectorSetupProposal(
            kind=SetupKind.BREAKOUT_LONG,
            side=Side.BUY,
            entry_low=Decimal("1.10"),
            entry_high=Decimal("1.10"),
            invalidation_price=Decimal("0.92"),
            targets=(Decimal("1.25"),),
            expires_at=now + timedelta(hours=1),
            reason_codes=(VectorReasonCode.PRICE_AVAILABLE,),
            summary="ok",
            notional_usd=Decimal("100000"),
        )


def test_the_model_does_not_mint_the_setup_identity():
    """Identity is derived from the geometry, so it cannot be chosen."""
    for owned in ("setup_id", "setup_fingerprint", "trigger"):
        assert owned not in VectorSetupProposal.model_fields


# ------------------------------------------- the trigger grammar itself


def test_a_threshold_trigger_cannot_carry_a_zone(now):
    with pytest.raises(ValueError):
        TriggerCondition(
            type=TriggerType.PRICE_GTE,
            reference_price=Decimal("1.10"),
            zone_low=Decimal("1.00"),
            zone_high=Decimal("1.10"),
            valid_from=now,
            expires_at=now + timedelta(hours=1),
        )


def test_a_threshold_trigger_must_name_its_reference(now):
    with pytest.raises(ValueError):
        TriggerCondition(
            type=TriggerType.PRICE_GTE,
            valid_from=now,
            expires_at=now + timedelta(hours=1),
        )


def test_a_range_trigger_cannot_carry_a_single_reference(now):
    with pytest.raises(ValueError):
        TriggerCondition(
            type=TriggerType.PRICE_IN_RANGE,
            reference_price=Decimal("1.10"),
            zone_low=Decimal("1.00"),
            zone_high=Decimal("1.10"),
            valid_from=now,
            expires_at=now + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    "zone", [(Decimal("1.00"), None), (None, Decimal("1.10")), (Decimal("1.10"), Decimal("1.00"))]
)
def test_a_range_trigger_needs_two_ordered_bounds(now, zone):
    low, high = zone
    with pytest.raises(ValueError):
        TriggerCondition(
            type=TriggerType.PRICE_IN_RANGE,
            zone_low=low,
            zone_high=high,
            valid_from=now,
            expires_at=now + timedelta(hours=1),
        )


def test_a_trigger_cannot_expire_before_it_becomes_valid(now):
    with pytest.raises(ValueError):
        TriggerCondition(
            type=TriggerType.PRICE_GTE,
            reference_price=Decimal("1.10"),
            valid_from=now,
            expires_at=now,
        )


@pytest.mark.parametrize(
    ("price", "met"),
    [(Decimal("0.89"), True), (Decimal("0.90"), True), (Decimal("0.91"), False)],
)
def test_the_downward_threshold_grammar_is_defined_though_no_kind_uses_it(now, price, met):
    """PRICE_LTE exists for the exit and short shapes a later phase may add.

    It is defined and tested rather than left as an untried branch that a future
    phase would discover the behaviour of at the worst possible moment.
    """
    condition = TriggerCondition(
        type=TriggerType.PRICE_LTE,
        reference_price=Decimal("0.90"),
        valid_from=now,
        expires_at=now + timedelta(hours=1),
    )
    assert condition.is_met(price) is met
    assert TriggerType.PRICE_LTE not in set(REQUIRED_TRIGGER.values())


# --------------------------------------------- measurement and proposal invariants


def test_an_available_measurement_must_carry_a_value(now):
    with pytest.raises(ValueError):
        ObservedMeasurement(
            observation_id=stable_id("price"),
            status=Availability.AVAILABLE,
            value_usd=None,
            observed_at=now,
        )


def test_an_unknown_measurement_must_not_carry_one(now):
    """The pairing is enforced both ways, so a zero can never impersonate a gap."""
    with pytest.raises(ValueError):
        ObservedMeasurement(
            observation_id=stable_id("price"),
            status=Availability.UNKNOWN,
            value_usd=Decimal("0"),
            observed_at=now,
        )


def test_repeated_reason_codes_are_refused(now):
    with pytest.raises(ValueError):
        breakout(
            now,
            reason_codes=(VectorReasonCode.PRICE_AVAILABLE, VectorReasonCode.PRICE_AVAILABLE),
        )


def test_a_citation_repeated_to_look_like_support_is_refused(now):
    with pytest.raises(ValueError):
        breakout(now, cited_observation_ids=(stable_id("price"), stable_id("price")))


# ------------------------------------------------- a narrower policy binds


def narrowed(**changes):
    from dataclasses import replace

    return replace(VECTOR_SETUP_V1, **changes)


def test_a_policy_that_drops_a_kind_refuses_that_kind(now):
    """The supported set is the policy's to decide, not the model's."""
    policy = narrowed(supported_kinds=frozenset({SetupKind.BREAKOUT_LONG}))
    validate_proposal(breakout(now), task_input(now), policy)
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(pullback(now), task_input(now), policy)
    assert error.value.reason_code == "UNSUPPORTED_SETUP_KIND"


def test_a_policy_that_allows_fewer_targets_refuses_the_extra_ones(now):
    policy = narrowed(max_targets=1)
    with pytest.raises(VectorValidationError) as error:
        validate_proposal(breakout(now), task_input(now), policy)
    assert error.value.reason_code == "TOO_MANY_TARGETS"
    validate_proposal(breakout(now, targets=(Decimal("1.25"),)), task_input(now), policy)
