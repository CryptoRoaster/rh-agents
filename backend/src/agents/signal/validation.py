"""Semantic validation of SIGNAL output against the input it was actually given.

Schema parsing proves the shape. This proves the content is consistent with the
observations SIGNAL saw and with the structure already measured from them.

Two failures matter most here, and they are different. A model may *invent* a
source — a post identifier or an author it was never shown — which would put
fabricated social evidence into the record. And a model may *overrule* the
deterministic layer, describing a campaign run by three accounts as broad organic
interest. Neither is a matter of degree, so neither is tolerated: contradicted
output is an invalid result and is never persisted, not even as unknown evidence.

Note the asymmetry in how manipulation is treated. Raising a concern the
measurements missed is allowed and recorded; talking a measured concern down is
not expressible at all, because the deterministic levels are computed before the
model is called and the model has no field with which to lower them.
"""

from src.agents.signal.models import (
    QualitativeLevel,
    SentimentDirection,
    SignalAssessment,
    SignalTaskInput,
    SocialDemandIndication,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1, SignalQualityPolicy, exceeds_ceiling

# Directions that assert people were positive about the asset. Claiming one while
# the measured structure shows a narrow, duplicated campaign is the specific
# contradiction this validator exists to catch.
POSITIVE_DIRECTIONS = frozenset({SentimentDirection.POSITIVE})


class SignalValidationError(Exception):
    """A safe reason code for output that contradicts its own input."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def validate_assessment(
    assessment: SignalAssessment,
    task_input: SignalTaskInput,
    policy: SignalQualityPolicy = SIGNAL_QUALITY_V1,
) -> None:
    """Reject output that strays from the sample or from the measured structure."""
    if not set(assessment.cited_observation_ids) <= task_input.representative_ids:
        # A model may only cite posts it was actually shown. Anything else is an
        # invented source, however plausible it reads.
        raise SignalValidationError("UNKNOWN_OBSERVATION_REFERENCE")

    breadth = task_input.structure.organic_breadth
    if exceeds_ceiling(assessment.social_demand_indication, breadth, policy):
        # Stated interest cannot be broader than the set of people who stated it.
        raise SignalValidationError("DEMAND_EXCEEDS_MEASURED_BREADTH")

    if (
        assessment.social_demand_indication != SocialDemandIndication.NONE
        and task_input.features.observation_count == 0
    ):
        raise SignalValidationError("DEMAND_WITHOUT_OBSERVATIONS")

    if (
        assessment.sentiment_direction in POSITIVE_DIRECTIONS
        and breadth == QualitativeLevel.VERY_LOW
        and assessment.social_demand_indication != SocialDemandIndication.NONE
    ):
        # The narrowest possible discussion supports a reading of its tone, never
        # a claim that anyone beyond those few voices wants the asset.
        raise SignalValidationError("DEMAND_EXCEEDS_MEASURED_BREADTH")
