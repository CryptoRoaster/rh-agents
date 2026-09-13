"""The deterministic comparison, and every way it can refuse to make one.

This is the load-bearing file of the phase. PULSE has exactly one job and the
whole question is whether it does that job exactly — inclusive boundaries where
the grammar says inclusive, exact Decimals with no epsilon anywhere, and a firm
refusal to compare numbers that do not belong together.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.pulse.evaluator import condition_met, evaluate
from src.agents.pulse.models import PulseReasonCode, TriggerOutcome
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.agents.vector.models import TriggerType
from tests.pulse.conftest import LEVEL, observed, task_input, watched


def check(now, **kwargs):
    return evaluate(task_input(now, **kwargs), now, PULSE_TRIGGER_V1)


# ------------------------------------------------- A–C: the upward threshold


def test_scenario_a_a_price_below_the_level_is_not_triggered(now):
    """The ordinary answer, for possibly hours. Not an error of any kind."""
    result = check(now, observation=observed(now, price=Decimal("1.19")))
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED
    assert result.reason_code == PulseReasonCode.CONDITION_NOT_MET
    assert result.is_waiting is True
    # The price it looked at is recorded even when it did not fire, so a wait is
    # explicable rather than merely quiet.
    assert result.observed_price == Decimal("1.19")


def test_scenario_b_a_price_exactly_at_the_level_triggers(now):
    """GTE means at or above. An exclusive comparison would silently mean
    something the setup did not say."""
    result = check(now, observation=observed(now, price=LEVEL))
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.reason_code == PulseReasonCode.CONDITION_MET
    assert result.observed_price == LEVEL


def test_scenario_c_a_price_above_the_level_triggers(now):
    result = check(now, observation=observed(now, price=Decimal("1.21")))
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.is_waiting is False


# ------------------------------------------------- D–F: the downward threshold


def downward(now, price):
    return check(
        now,
        trigger=watched(now, type=TriggerType.PRICE_LTE),
        observation=observed(now, price=price),
    )


def test_scenario_d_a_price_above_a_downward_level_waits(now):
    assert downward(now, Decimal("1.21")).outcome == TriggerOutcome.NOT_TRIGGERED


def test_scenario_e_a_price_exactly_at_a_downward_level_triggers(now):
    assert downward(now, LEVEL).outcome == TriggerOutcome.TRIGGERED


def test_scenario_f_a_price_below_a_downward_level_triggers(now):
    assert downward(now, Decimal("1.19")).outcome == TriggerOutcome.TRIGGERED


# ----------------------------------------------------------- the range form


def zone(now, price):
    return check(
        now,
        trigger=watched(
            now,
            type=TriggerType.PRICE_IN_RANGE,
            reference_price=None,
            zone_low=Decimal("0.90"),
            zone_high=Decimal("0.95"),
        ),
        observation=observed(now, price=price),
    )


@pytest.mark.parametrize(
    ("price", "triggered"),
    [
        (Decimal("0.89"), False),
        (Decimal("0.90"), True),
        (Decimal("0.925"), True),
        (Decimal("0.95"), True),
        (Decimal("0.96"), False),
    ],
)
def test_the_range_form_is_inclusive_at_both_ends(now, price, triggered):
    """Exactly the semantics VECTOR's own grammar tests pin, re-checked here."""
    expected = TriggerOutcome.TRIGGERED if triggered else TriggerOutcome.NOT_TRIGGERED
    assert zone(now, price).outcome == expected


def test_the_grammar_is_exactly_the_one_vector_writes():
    """No prose triggers, and nothing invented. The three VECTOR defines."""
    assert {item.value for item in TriggerType} == {"PRICE_GTE", "PRICE_LTE", "PRICE_IN_RANGE"}
    for invented in ("MOMENTUM_CONFIRMED", "BREAKOUT_LOOKS_GOOD", "STRONG_VOLUME"):
        assert invented not in {item.value for item in TriggerType}


# --------------------------------------------------------- exactness


@pytest.mark.parametrize(
    ("level", "price"),
    [
        (Decimal("1.234500"), Decimal("1.2345")),
        (Decimal("1.2345"), Decimal("1.234500")),
        (Decimal("1.23450000"), Decimal("1.2345000000")),
    ],
)
def test_trailing_zeros_do_not_change_the_comparison(now, level, price):
    """Canonical serialization must not move a boundary.

    Decimal equality is by value, not by representation, and both threshold
    forms fire at the boundary — so the same number written two ways triggers
    both, which is what a reader of the evidence would expect.
    """
    for kind in (TriggerType.PRICE_GTE, TriggerType.PRICE_LTE):
        result = check(
            now,
            trigger=watched(now, type=kind, reference_price=level),
            observation=observed(now, price=price),
        )
        assert result.outcome == TriggerOutcome.TRIGGERED


@pytest.mark.parametrize(
    ("level", "price", "triggered"),
    [
        # A token priced in the tenth-of-a-nanodollar range still compares exactly.
        (Decimal("0.000000000123"), Decimal("0.000000000124"), True),
        (Decimal("0.000000000123"), Decimal("0.000000000122"), False),
        (Decimal("0.000000000123"), Decimal("0.000000000123"), True),
        # And so does one priced in the tens of thousands.
        (Decimal("64000.50"), Decimal("64000.51"), True),
        (Decimal("64000.50"), Decimal("64000.49"), False),
    ],
)
def test_realistic_token_price_scales_compare_exactly(now, level, price, triggered):
    """No float ever participates, so no scale loses a comparison."""
    result = check(
        now,
        trigger=watched(now, reference_price=level),
        observation=observed(now, price=price),
    )
    expected = TriggerOutcome.TRIGGERED if triggered else TriggerOutcome.NOT_TRIGGERED
    assert result.outcome == expected


def test_a_price_a_hair_below_the_level_does_not_trigger(now):
    """There is no epsilon. One unit in the last place is still below."""
    result = check(
        now,
        trigger=watched(now, reference_price=Decimal("1.000000000000000001")),
        observation=observed(now, price=Decimal("1.000000000000000000")),
    )
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_price_cannot_be_constructed(now, bad):
    """Invalid prices are refused at the type boundary, before any comparison."""
    with pytest.raises(ValueError):
        observed(now, price=bad)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_a_nonfinite_price_cannot_be_constructed(now, bad):
    with pytest.raises(ValueError):
        observed(now, price=Decimal(bad))


def test_no_smoothing_or_confirmation_exists(now):
    """One fresh valid observation is enough, and nothing remembers the last one.

    Any averaging, debounce or "two consecutive ticks" rule would be a trading
    policy dressed as a safety measure, and none of it is in the contract VECTOR
    wrote. The evaluator is a pure function of one input.
    """
    import inspect

    from src.agents.pulse import evaluator

    source = inspect.getsource(evaluator).lower()
    for forbidden in ("average", "ema", "smooth", "debounce", "consecutive", "epsilon"):
        # Mentioned only in the module docstring's statement of what is absent.
        assert source.count(forbidden) <= 2
    signature = inspect.signature(evaluator.evaluate)
    assert set(signature.parameters) == {"task_input", "now", "policy"}


# --------------------------------------------------- G: the price is stale


def test_scenario_g_a_stale_price_cannot_trigger(now):
    """The condition would hold. The price is too old to say that it does."""
    result = check(now, observation=observed(now, price=Decimal("1.50"), seconds_ago=600))
    assert result.outcome == TriggerOutcome.OBSERVATION_STALE
    assert result.reason_code == PulseReasonCode.OBSERVATION_TOO_STALE
    assert result.is_waiting is True
    assert result.observed_price is None


def test_the_freshness_window_is_pulses_own_and_tighter_than_vectors(now):
    """A setup generator tolerates an older picture; a monitor asserts "now"."""
    from src.agents.vector.policy import VECTOR_SETUP_V1

    assert PULSE_TRIGGER_V1.max_observation_age == timedelta(minutes=2)
    assert PULSE_TRIGGER_V1.max_observation_age < VECTOR_SETUP_V1.max_input_age
    edge = PULSE_TRIGGER_V1.max_observation_age
    at_edge = check(
        now, observation=observed(now, price=LEVEL, seconds_ago=int(edge.total_seconds()))
    )
    assert at_edge.outcome == TriggerOutcome.TRIGGERED
    past = check(
        now, observation=observed(now, price=LEVEL, seconds_ago=int(edge.total_seconds()) + 1)
    )
    assert past.outcome == TriggerOutcome.OBSERVATION_STALE


def test_freshness_is_measured_from_source_time(now):
    """Not from when the row was read. A stale price read now is still stale."""
    stale = observed(now, price=LEVEL, seconds_ago=600)
    assert check(now, observation=stale).outcome == TriggerOutcome.OBSERVATION_STALE
    # The same observation, judged ten minutes earlier, was fresh.
    assert (
        evaluate(
            task_input(now - timedelta(minutes=10), observation=stale),
            now - timedelta(minutes=10),
            PULSE_TRIGGER_V1,
        ).outcome
        == TriggerOutcome.TRIGGERED
    )


def test_an_unavailable_price_produces_no_trigger(now):
    result = check(now, observation=None)
    assert result.outcome == TriggerOutcome.OBSERVATION_STALE
    assert result.reason_code == PulseReasonCode.PRICE_UNAVAILABLE


# ------------------------------------------- H, I: wrong unit, wrong market


def test_scenario_h_a_mismatched_price_basis_is_refused(now):
    """Numerically satisfying and semantically meaningless.

    Refused rather than waited on: a unit mismatch will not resolve itself, and
    reporting it as "not yet" would schedule a patient wait for an answer that
    cannot arrive.
    """
    result = check(
        now,
        observation=observed(now, price=Decimal("1.50")).model_copy(
            update={"price_basis": "USD_PER_QUOTE"}
        ),
    )
    assert result.outcome == TriggerOutcome.OBSERVATION_INVALID
    assert result.reason_code == PulseReasonCode.PRICE_BASIS_MISMATCH
    assert result.is_waiting is False


def test_no_reciprocal_or_conversion_is_attempted(now):
    """There is no arithmetic that could rescue the wrong unit, and none is tried."""
    import inspect

    from src.agents.pulse import evaluator

    source = inspect.getsource(evaluator).lower()
    for forbidden in ("1 /", "convert", "reciprocal", "invert"):
        assert source.count(forbidden) <= 1


def test_scenario_i_a_price_from_another_market_is_refused(now):
    """Same token, different pool, is a different market."""
    result = check(
        now,
        observation=observed(
            now, price=Decimal("1.50"), pair_id="robinhood:mainnet:contract_address:0x" + "ff" * 20
        ),
    )
    assert result.outcome == TriggerOutcome.OBSERVATION_INVALID
    assert result.reason_code == PulseReasonCode.MARKET_IDENTITY_MISMATCH


def test_identity_and_unit_are_checked_before_the_value(now):
    """So a wrong-market price never reads as "the level was not reached"."""
    elsewhere = observed(
        now, price=Decimal("0.01"), pair_id="robinhood:mainnet:contract_address:0x" + "ff" * 20
    )
    assert check(now, observation=elsewhere).outcome == TriggerOutcome.OBSERVATION_INVALID


# --------------------------------------- J, K: the setup's own window


def test_scenario_j_an_expired_setup_cannot_trigger(now):
    """The window closed before the condition became true."""
    expired = watched(
        now, valid_from=now - timedelta(hours=3), expires_at=now - timedelta(minutes=1)
    )
    result = check(now, trigger=expired, observation=observed(now, price=Decimal("1.50")))
    assert result.outcome == TriggerOutcome.SETUP_EXPIRED
    assert result.reason_code == PulseReasonCode.SETUP_EXPIRED


def test_scenario_k_the_expiry_boundary_is_exact(now):
    """Pinned deliberately: expiry is exclusive, so `now == expires_at` is over.

    A setup that expires at 10:00 is not valid at 10:00. Leaving this ambiguous
    would put a trade's authority in a rounding question.
    """
    expires_at = now + timedelta(minutes=30)
    crossing = observed(now, price=Decimal("1.50"))

    def at(instant):
        return evaluate(
            task_input(
                instant,
                trigger=watched(now, expires_at=expires_at),
                observation=crossing.model_copy(
                    update={"observed_at": instant - timedelta(seconds=1)}
                ),
            ),
            instant,
            PULSE_TRIGGER_V1,
        ).outcome

    assert at(expires_at - timedelta(microseconds=1)) == TriggerOutcome.TRIGGERED
    assert at(expires_at) == TriggerOutcome.SETUP_EXPIRED
    assert at(expires_at + timedelta(microseconds=1)) == TriggerOutcome.SETUP_EXPIRED


def test_a_setup_that_has_not_begun_cannot_trigger(now):
    future = watched(
        now, valid_from=now + timedelta(minutes=5), expires_at=now + timedelta(hours=1)
    )
    result = check(now, trigger=future, observation=observed(now, price=Decimal("1.50")))
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED
    assert result.reason_code == PulseReasonCode.SETUP_NOT_YET_VALID


# -------------------------------- Q, R: the observation's own timestamp


def test_scenario_q_a_price_from_before_the_setup_cannot_trigger_it(now):
    """A setup is a statement about what happens next, not a test on the past.

    Otherwise a level the market crossed an hour before anyone proposed watching
    it would fire the moment the setup was written.
    """
    trigger = watched(now, valid_from=now - timedelta(minutes=1))
    before = observed(now, price=Decimal("1.50"), seconds_ago=90)
    assert before.observed_at < trigger.valid_from
    result = check(now, trigger=trigger, observation=before)
    assert result.outcome == TriggerOutcome.OBSERVATION_PRECEDES_SETUP
    assert result.reason_code == PulseReasonCode.OBSERVATION_BEFORE_SETUP
    # A wait, not a fault: immediately after a setup is published the newest
    # recorded snapshot is older than it, and the next one will not be.
    assert result.is_waiting is True


def test_a_price_from_after_the_window_cannot_trigger_it(now):
    """The race the delayed-processing case creates, refused explicitly."""
    trigger = watched(
        now, valid_from=now - timedelta(hours=1), expires_at=now - timedelta(seconds=1)
    )
    late = observed(now, price=Decimal("1.50"), seconds_ago=0)
    # The setup check fires first; the observation rule exists for the case where
    # the window is still open but the reading sits outside it.
    assert check(now, trigger=trigger, observation=late).outcome == TriggerOutcome.SETUP_EXPIRED


def test_an_observation_at_the_exact_expiry_instant_is_outside_the_window(now):
    expires_at = now + timedelta(minutes=10)
    trigger = watched(now, expires_at=expires_at)
    at_expiry = observed(now, price=Decimal("1.50")).model_copy(update={"observed_at": expires_at})
    result = evaluate(
        task_input(expires_at - timedelta(microseconds=1), trigger=trigger, observation=at_expiry),
        expires_at - timedelta(microseconds=1),
        PULSE_TRIGGER_V1,
    )
    assert result.outcome == TriggerOutcome.OBSERVATION_INVALID
    assert result.reason_code == PulseReasonCode.OBSERVATION_AFTER_SETUP


def test_scenario_r_a_future_dated_price_is_refused(now):
    """Beyond the skew tolerance, a future timestamp is a fault, not a clock."""
    ahead = observed(now, price=Decimal("1.50")).model_copy(
        update={"observed_at": now + timedelta(minutes=1)}
    )
    result = check(now, observation=ahead)
    assert result.outcome == TriggerOutcome.OBSERVATION_INVALID
    assert result.reason_code == PulseReasonCode.OBSERVATION_IN_FUTURE


def test_ordinary_clock_skew_is_tolerated(now):
    """Two clocks at an instant boundary may disagree by a moment."""
    assert PULSE_TRIGGER_V1.max_clock_skew == timedelta(seconds=5)
    slight = observed(now, price=Decimal("1.50")).model_copy(
        update={"observed_at": now + timedelta(seconds=4)}
    )
    assert check(now, observation=slight).outcome == TriggerOutcome.TRIGGERED
    beyond = observed(now, price=Decimal("1.50")).model_copy(
        update={"observed_at": now + timedelta(seconds=6)}
    )
    assert check(now, observation=beyond).outcome == TriggerOutcome.OBSERVATION_INVALID


# ----------------------------------------------------- nothing to watch


def test_no_current_setup_is_a_wait_rather_than_an_end(now):
    """A new setup may still arrive; the monitor keeps its place."""
    result = check(now, trigger=None)
    assert result.outcome == TriggerOutcome.NO_CURRENT_SETUP
    assert result.is_waiting is True


# ------------------------------------------------------- determinism


def test_the_same_input_always_gives_the_same_answer(now):
    context = task_input(now, observation=observed(now, price=LEVEL))
    answers = {evaluate(context, now, PULSE_TRIGGER_V1).outcome for _ in range(25)}
    assert answers == {TriggerOutcome.TRIGGERED}


def test_the_comparison_alone_is_a_pure_function(now):
    trigger = watched(now)
    assert condition_met(trigger, LEVEL) is True
    assert condition_met(trigger, Decimal("1.19")) is False
    assert condition_met(trigger, LEVEL) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_observation_age", timedelta(0)),
        ("poll_interval", timedelta(0)),
        ("max_clock_skew", timedelta(seconds=-1)),
        # Skew at or beyond the freshness window would make the two rules
        # disagree about the same timestamp.
        ("max_clock_skew", timedelta(minutes=2)),
        # Polling less often than data goes stale guarantees refused checks.
        ("poll_interval", timedelta(minutes=5)),
    ],
)
def test_an_incoherent_policy_refuses_to_exist(field, value):
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(PULSE_TRIGGER_V1, **{field: value})


def test_the_policy_is_versioned():
    assert PULSE_TRIGGER_V1.version == "pulse-trigger-v1"


# --------------------------------------- the condition's own shape, re-checked


@pytest.mark.parametrize(
    "broken",
    [
        # A threshold form carrying a zone, or no reference at all.
        {"zone_low": Decimal("1.0"), "zone_high": Decimal("1.5")},
        {"reference_price": None},
    ],
)
def test_a_malformed_threshold_condition_is_refused(now, broken):
    """PULSE re-validates the grammar rather than trusting its producer.

    A condition that arrived malformed would otherwise be evaluated by whichever
    branch happened to match, which is the worst possible way to be wrong about
    what the system was waiting for.
    """
    with pytest.raises(ValueError):
        watched(now, **broken)


@pytest.mark.parametrize(
    "broken",
    [
        {"reference_price": LEVEL, "zone_low": Decimal("1.0"), "zone_high": Decimal("1.5")},
        {"reference_price": None, "zone_low": None, "zone_high": Decimal("1.5")},
        {"reference_price": None, "zone_low": Decimal("1.5"), "zone_high": Decimal("1.0")},
    ],
)
def test_a_malformed_range_condition_is_refused(now, broken):
    with pytest.raises(ValueError):
        watched(now, type=TriggerType.PRICE_IN_RANGE, **broken)


def test_a_condition_that_expires_before_it_begins_is_refused(now):
    with pytest.raises(ValueError):
        watched(now, valid_from=now, expires_at=now - timedelta(minutes=1))


def test_a_trigger_result_must_record_what_satisfied_it(now):
    """No trigger without the observation that caused it."""
    from src.agents.pulse.models import TriggerEvaluation

    with pytest.raises(ValueError):
        TriggerEvaluation(
            outcome=TriggerOutcome.TRIGGERED,
            reason_code=PulseReasonCode.CONDITION_MET,
            evaluated_at=now,
        )


def test_a_price_and_its_source_time_travel_together(now):
    """One without the other would be a number with no moment attached."""
    from src.agents.pulse.models import TriggerEvaluation

    with pytest.raises(ValueError):
        TriggerEvaluation(
            outcome=TriggerOutcome.NOT_TRIGGERED,
            reason_code=PulseReasonCode.CONDITION_NOT_MET,
            evaluated_at=now,
            observed_price=LEVEL,
        )
