"""The deterministic structural verdict: what the counts are allowed to mean.

The central claim under test is that data quality and sentiment are independent.
A flood of identical praise is perfectly real data about a campaign and says
nothing about broad support, and no threshold here is permitted to blur the two.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.signal.models import (
    MarketBindingBasis,
    QualitativeLevel,
    SignalDataQuality,
    SignalGap,
    SocialDemandIndication,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1, assess_structure, exceeds_ceiling
from tests.signal.conftest import (
    bound,
    campaign_set,
    collision_set,
    influencer_set,
    organic_set,
    stale_set,
)
from tests.signal.test_quality import features_for


def structure_for(observations, now, **kwargs):
    return assess_structure(features_for(observations, now), SIGNAL_QUALITY_V1, **kwargs)


# ----------------------------------------------------------- the four levels


def test_a_real_conversation_is_usable_and_broad(now):
    structure = structure_for(organic_set(now), now)
    assert structure.data_quality == SignalDataQuality.USABLE
    assert structure.organic_breadth == QualitativeLevel.HIGH
    assert structure.manipulation_concern == QualitativeLevel.VERY_LOW
    assert structure.gaps == ()


def test_a_copy_campaign_is_loud_narrow_and_suspicious_at_once(now):
    """The scenario the whole deterministic layer exists for.

    Nothing here says the language was not positive. It says the positivity came
    from five accounts posting one sentence, which is a different claim entirely.
    """
    structure = structure_for(campaign_set(now), now)
    assert structure.attention_level == QualitativeLevel.HIGH
    assert structure.organic_breadth == QualitativeLevel.VERY_LOW
    assert structure.manipulation_concern == QualitativeLevel.HIGH
    # Still interpretable data, and explicitly not a clean set.
    assert structure.data_quality == SignalDataQuality.DEGRADED
    assert SignalGap.DUPLICATE_DOMINATED in structure.gaps


def test_one_amplified_voice_is_loud_and_not_broad(now):
    structure = structure_for(influencer_set(now), now)
    assert structure.attention_level == QualitativeLevel.HIGH
    assert structure.organic_breadth == QualitativeLevel.LOW
    # Amplification is not coordination; nothing here claims a campaign.
    assert structure.manipulation_concern == QualitativeLevel.VERY_LOW
    assert structure.data_quality == SignalDataQuality.DEGRADED


def test_attention_counts_posts_and_says_nothing_about_agreement(now):
    quiet = structure_for(organic_set(now)[:6], now)
    loud = structure_for(campaign_set(now), now)
    assert quiet.attention_level == QualitativeLevel.LOW
    assert loud.attention_level == QualitativeLevel.HIGH
    # The louder set is the narrower one, which is the whole point: volume and
    # breadth move independently and a single number could not say both.
    assert loud.organic_breadth == QualitativeLevel.VERY_LOW
    assert quiet.organic_breadth == QualitativeLevel.LOW


# ----------------------------------------------------------- insufficiency


def test_no_observations_is_insufficient_and_never_neutral(now):
    structure = structure_for((), now)
    assert structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.NO_OBSERVATIONS in structure.gaps


def test_a_dead_window_is_distinguishable_from_a_quiet_market(now):
    """Both yield nothing to read. They call for entirely different responses."""
    empty = structure_for((), now)
    stale = structure_for(stale_set(now), now)
    assert stale.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW in stale.gaps
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW not in empty.gaps


def test_an_unavailable_source_is_never_an_empty_feed(now):
    structure = structure_for((), now, source_unavailable=True)
    assert structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.SOURCE_UNAVAILABLE in structure.gaps


def test_a_set_lost_entirely_to_ticker_collisions_records_why(now):
    structure = structure_for(collision_set(now), now)
    assert structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.AMBIGUOUS_REFERENCES_EXCLUDED in structure.gaps


def test_too_few_voices_cannot_support_a_market_reading(now):
    two = tuple(
        bound(
            f"pair-{index}", now=now, minutes_ago=10, author=f"a-{index % 2}", text=f"DEMO {index}"
        )
        for index in range(6)
    )
    structure = structure_for(two, now)
    assert structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.TOO_FEW_AUTHORS in structure.gaps


def test_a_set_with_no_strong_binding_is_degraded_rather_than_clean(now):
    contextual = tuple(
        item.model_copy(
            update={
                "binding_basis": MarketBindingBasis.UNIQUE_SYMBOL_WITH_CONTEXT,
                "binding_address": None,
                "binding_chain": None,
            }
        )
        for item in organic_set(now)
    )
    structure = structure_for(contextual, now)
    assert structure.data_quality == SignalDataQuality.DEGRADED
    assert SignalGap.NO_STRONGLY_BOUND_OBSERVATIONS in structure.gaps


def test_six_people_writing_once_each_are_not_a_concentrated_set(now):
    """An even small set must not trip the concentration term by arithmetic.

    With six authors the top-five share is 0.83 whatever they wrote, so a naive
    threshold would label every small honest conversation as dominated.
    """
    structure = structure_for(organic_set(now)[:6], now)
    assert SignalGap.AUTHOR_CONCENTRATED not in structure.gaps
    assert structure.manipulation_concern == QualitativeLevel.VERY_LOW


def test_a_genuine_tail_being_drowned_out_does_trip_it(now):
    """Twenty authors, five of whom produce almost everything. Real skew."""
    loud = tuple(
        bound(
            f"loud-{index}",
            now=now,
            minutes_ago=10 + index,
            author=f"loud-{index % 5}",
            text=f"DEMO thought number {index}",
        )
        for index in range(60)
    )
    tail = tuple(
        bound(
            f"tail-{index}",
            now=now,
            minutes_ago=20 + index,
            author=f"quiet-{index}",
            text=f"DEMO tail note {index}",
        )
        for index in range(15)
    )
    structure = structure_for(loud + tail, now)
    assert SignalGap.AUTHOR_CONCENTRATED in structure.gaps
    assert structure.manipulation_concern != QualitativeLevel.VERY_LOW


# -------------------------------------------------------- the demand ceiling


@pytest.mark.parametrize(
    ("breadth", "ceiling"),
    [
        (QualitativeLevel.VERY_LOW, SocialDemandIndication.NONE),
        (QualitativeLevel.LOW, SocialDemandIndication.WEAK),
        (QualitativeLevel.MODERATE, SocialDemandIndication.MODERATE),
        (QualitativeLevel.HIGH, SocialDemandIndication.STRONG),
    ],
)
def test_stated_interest_cannot_exceed_the_people_who_stated_it(breadth, ceiling):
    assert SIGNAL_QUALITY_V1.demand_ceiling(breadth) == ceiling
    assert not exceeds_ceiling(ceiling, breadth, SIGNAL_QUALITY_V1)


def test_a_campaign_supports_no_demand_claim_at_all(now):
    structure = structure_for(campaign_set(now), now)
    assert SIGNAL_QUALITY_V1.demand_ceiling(structure.organic_breadth) == (
        SocialDemandIndication.NONE
    )
    assert exceeds_ceiling(
        SocialDemandIndication.WEAK, structure.organic_breadth, SIGNAL_QUALITY_V1
    )


# ------------------------------------------------------------ policy itself


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window", timedelta(0)),
        ("burst_interval", timedelta(0)),
        ("min_observations", 0),
        ("min_unique_authors", 0),
        ("attention_steps", (5, 5, 10, 20)),
        ("breadth_steps", (20, 10, 5, 1)),
        ("duplicate_share_elevated", Decimal("1.5")),
        ("duplicate_share_severe", Decimal("0.1")),
        ("admissible_bases", frozenset()),
    ],
)
def test_an_incoherent_policy_refuses_to_exist(field, value):
    with pytest.raises(ValueError):
        replace(SIGNAL_QUALITY_V1, **{field: value})


@pytest.mark.parametrize(
    "basis", [MarketBindingBasis.AMBIGUOUS_SYMBOL, MarketBindingBasis.UNRESOLVED]
)
def test_a_policy_can_never_admit_an_unbound_reference(basis):
    """The one rule a future tuning pass must not be able to relax by accident."""
    with pytest.raises(ValueError):
        replace(SIGNAL_QUALITY_V1, admissible_bases=frozenset({basis}))


def test_the_shipped_policy_is_versioned(now):
    assert SIGNAL_QUALITY_V1.version == "signal-quality-v1"
    assert structure_for(organic_set(now), now).policy_version == "signal-quality-v1"
