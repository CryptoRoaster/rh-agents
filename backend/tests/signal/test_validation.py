"""Model output checked against the input it was actually given.

Two failures are tested here and they are different in kind. Inventing a source
puts evidence into the record that nobody published. Overruling the deterministic
layer keeps the evidence real and gets its meaning exactly backwards — a campaign
described as a movement. The second is the more dangerous of the two, because
nothing about it looks wrong.
"""

from uuid import uuid4

import pytest

from src.agents.signal.models import (
    QualitativeLevel,
    SentimentDirection,
    SentimentStrength,
    SignalAssessment,
    SignalGap,
    SignalNarrative,
    SocialDemandIndication,
)
from src.agents.signal.validation import SignalValidationError, validate_assessment
from tests.signal.conftest import campaign_set, influencer_set, organic_set
from tests.signal.test_context import read


def assessment(**overrides) -> SignalAssessment:
    defaults = dict(
        sentiment_direction=SentimentDirection.POSITIVE,
        sentiment_strength=SentimentStrength.MODERATE,
        social_demand_indication=SocialDemandIndication.MODERATE,
        narrative_tags=(SignalNarrative.UTILITY_OR_PRODUCT,),
        cited_observation_ids=(),
        summary="Several independent accounts discussed the product.",
    )
    return SignalAssessment(**{**defaults, **overrides})


# ------------------------------------------------------- invented sources


async def test_a_post_that_was_never_shown_cannot_be_cited(now):
    task_input = await read(organic_set(now), now)
    with pytest.raises(SignalValidationError) as error:
        validate_assessment(assessment(cited_observation_ids=(uuid4(),)), task_input)
    assert error.value.reason_code == "UNKNOWN_OBSERVATION_REFERENCE"


async def test_a_post_in_the_set_but_outside_the_sample_cannot_be_cited(now):
    """Being shown the metrics is not being shown the post."""
    task_input = await read(organic_set(now), now, max_model=3)
    outside = next(
        item.observation_id
        for item in organic_set(now)
        if item.observation_id not in task_input.representative_ids
    )
    with pytest.raises(SignalValidationError):
        validate_assessment(assessment(cited_observation_ids=(outside,)), task_input)


async def test_citing_the_sample_is_accepted(now):
    task_input = await read(organic_set(now), now)
    cited = tuple(item.observation_id for item in task_input.representatives[:3])
    validate_assessment(assessment(cited_observation_ids=cited), task_input)


# --------------------------------------------- contradicting the structure


async def test_a_campaign_cannot_be_described_as_broad_demand(now):
    """Scenario I. Five accounts, one sentence, and a model calling it adoption."""
    task_input = await read(campaign_set(now), now)
    assert task_input.structure.organic_breadth == QualitativeLevel.VERY_LOW
    with pytest.raises(SignalValidationError) as error:
        validate_assessment(
            assessment(
                social_demand_indication=SocialDemandIndication.STRONG,
                summary="Broad organic adoption across a wide community.",
            ),
            task_input,
        )
    assert error.value.reason_code == "DEMAND_EXCEEDS_MEASURED_BREADTH"


@pytest.mark.parametrize(
    "indication",
    [
        SocialDemandIndication.WEAK,
        SocialDemandIndication.MODERATE,
        SocialDemandIndication.STRONG,
    ],
)
async def test_no_level_of_demand_survives_the_narrowest_measured_breadth(now, indication):
    task_input = await read(campaign_set(now), now)
    with pytest.raises(SignalValidationError):
        validate_assessment(assessment(social_demand_indication=indication), task_input)


async def test_the_campaign_may_still_be_read_as_positive_language(now):
    """The tone is real data. Only the claim about breadth is refused."""
    task_input = await read(campaign_set(now), now)
    validate_assessment(
        assessment(
            sentiment_direction=SentimentDirection.POSITIVE,
            sentiment_strength=SentimentStrength.STRONG,
            social_demand_indication=SocialDemandIndication.NONE,
            narrative_tags=(SignalNarrative.PROMOTIONAL_CALL_TO_ACTION,),
            summary="Uniformly promotional language repeated by a handful of accounts.",
        ),
        task_input,
    )


async def test_one_amplified_voice_supports_no_more_than_weak_interest(now):
    task_input = await read(influencer_set(now), now)
    assert task_input.structure.organic_breadth == QualitativeLevel.LOW
    validate_assessment(
        assessment(social_demand_indication=SocialDemandIndication.WEAK), task_input
    )
    with pytest.raises(SignalValidationError):
        validate_assessment(
            assessment(social_demand_indication=SocialDemandIndication.MODERATE), task_input
        )


async def test_a_broad_conversation_supports_a_strong_reading(now):
    task_input = await read(organic_set(now), now)
    assert task_input.structure.organic_breadth == QualitativeLevel.HIGH
    validate_assessment(
        assessment(social_demand_indication=SocialDemandIndication.STRONG), task_input
    )


async def test_a_model_may_add_a_manipulation_concern_the_metrics_missed(now):
    """Raising a concern is allowed. There is no field with which to lower one."""
    task_input = await read(organic_set(now), now)
    validate_assessment(
        assessment(manipulation_observations=(SignalGap.DUPLICATE_DOMINATED,)), task_input
    )
    assert not hasattr(SignalAssessment, "manipulation_concern")


# --------------------------------------------------------- schema itself


def test_the_output_schema_cannot_express_a_trade():
    """Prompt injection has nothing to aim at: the fields simply do not exist."""
    fields = set(SignalAssessment.model_fields)
    for forbidden in ("side", "buy", "sell", "size", "entry", "target", "approve", "verdict"):
        assert forbidden not in fields


def test_an_unreadable_direction_cannot_be_claimed_strongly():
    with pytest.raises(ValueError):
        assessment(
            sentiment_direction=SentimentDirection.UNCLEAR,
            sentiment_strength=SentimentStrength.STRONG,
        )


def test_unknown_output_fields_are_refused():
    with pytest.raises(ValueError):
        SignalAssessment(
            sentiment_direction=SentimentDirection.POSITIVE,
            sentiment_strength=SentimentStrength.WEAK,
            social_demand_indication=SocialDemandIndication.NONE,
            summary="ok",
            recommendation="BUY",
        )
