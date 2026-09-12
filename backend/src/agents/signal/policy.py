"""Versioned deterministic SIGNAL quality policy.

This module decides whether a social observation set can carry an interpretation
at all, and what its structure means. It contains no model, reads no prompt, and
nothing a model returns can change what it concludes. Thresholds live here as
code so a later change is a reviewable diff rather than an environment variable.

The division of labour is deliberate. Whether people are talking, how many of
them there really are, and how much of it is the same text repeated are questions
arithmetic answers. What the talking *means* is not, and that part is left to a
model whose answer is recorded as advisory.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.agents.signal.models import (
    DEMAND_ORDER,
    LEVEL_ORDER,
    CollectionCoverage,
    MarketBindingBasis,
    QualitativeLevel,
    SignalDataQuality,
    SignalGap,
    SignalQualityFeatures,
    SignalStructuralAssessment,
    SocialDemandIndication,
)

LEVELS = tuple(sorted(LEVEL_ORDER, key=lambda level: LEVEL_ORDER[level]))


def _shift(level: QualitativeLevel, steps: int) -> QualitativeLevel:
    return LEVELS[max(0, min(len(LEVELS) - 1, LEVEL_ORDER[level] + steps))]


# How much stated interest a given breadth of authorship can support. A crowd of
# two cannot indicate broad demand however enthusiastic it sounds, so this is the
# ceiling a model's reading is held to rather than a value it may choose.
DEMAND_CEILING: dict[QualitativeLevel, SocialDemandIndication] = {
    QualitativeLevel.VERY_LOW: SocialDemandIndication.NONE,
    QualitativeLevel.LOW: SocialDemandIndication.WEAK,
    QualitativeLevel.MODERATE: SocialDemandIndication.MODERATE,
    QualitativeLevel.HIGH: SocialDemandIndication.STRONG,
    QualitativeLevel.VERY_HIGH: SocialDemandIndication.STRONG,
}


@dataclass(frozen=True)
class SignalQualityPolicy:
    """Thresholds that turn deterministic features into a structural verdict."""

    version: str
    window: timedelta
    burst_interval: timedelta
    min_observations: int
    min_unique_authors: int
    # Post counts at which attention moves up a level. Counts, not opinions.
    attention_steps: tuple[int, int, int, int]
    # Distinct authors at which breadth moves up a level, before any demotion.
    breadth_steps: tuple[int, int, int, int]
    duplicate_share_elevated: Decimal
    duplicate_share_severe: Decimal
    top1_author_share_severe: Decimal
    top5_author_share_elevated: Decimal
    burst_share_elevated: Decimal
    admissible_bases: frozenset[MarketBindingBasis]

    def __post_init__(self) -> None:
        if self.window <= timedelta(0) or self.burst_interval <= timedelta(0):
            raise ValueError("Window and burst interval must be positive")
        if self.min_observations < 1 or self.min_unique_authors < 1:
            raise ValueError("A usable set needs at least one observation and one author")
        for steps in (self.attention_steps, self.breadth_steps):
            if list(steps) != sorted(steps) or len(set(steps)) != len(steps):
                raise ValueError("Level steps must be strictly increasing")
        for share in (
            self.duplicate_share_elevated,
            self.duplicate_share_severe,
            self.top1_author_share_severe,
            self.top5_author_share_elevated,
            self.burst_share_elevated,
        ):
            if not Decimal(0) < share <= Decimal(1):
                raise ValueError("A share threshold must fall in (0, 1]")
        if self.duplicate_share_severe < self.duplicate_share_elevated:
            raise ValueError("The severe duplicate threshold cannot sit below the elevated one")
        if not self.admissible_bases:
            raise ValueError("At least one binding basis must be admissible")
        if MarketBindingBasis.UNRESOLVED in self.admissible_bases:
            raise ValueError("An unresolved reference can never be about this market")
        if MarketBindingBasis.AMBIGUOUS_SYMBOL in self.admissible_bases:
            # A bare ticker is claimed by unrelated tokens on unrelated chains.
            raise ValueError("A bare symbol can never bind an observation to a market")

    def demand_ceiling(self, breadth: QualitativeLevel) -> SocialDemandIndication:
        return DEMAND_CEILING[breadth]


# Provisional PAPER-mode policy. The six-hour window is deliberately short: this
# phase measures current attention, and a wider window would let last week's
# campaign read as today's interest. The share thresholds are coarse on purpose —
# they separate "a few people repeated themselves" from "this is one text posted
# a hundred times", which is the distinction that matters, and inventing finer
# gradations would imply a precision social data does not have.
SIGNAL_QUALITY_V1 = SignalQualityPolicy(
    version="signal-quality-v1",
    window=timedelta(hours=6),
    burst_interval=timedelta(minutes=15),
    min_observations=5,
    min_unique_authors=3,
    attention_steps=(1, 10, 50, 200),
    breadth_steps=(3, 8, 25, 75),
    duplicate_share_elevated=Decimal("0.25"),
    duplicate_share_severe=Decimal("0.50"),
    top1_author_share_severe=Decimal("0.50"),
    top5_author_share_elevated=Decimal("0.80"),
    burst_share_elevated=Decimal("0.50"),
    # An unscoped address and a symbol with resolvable context are both
    # admissible and both weak, counted separately so a set that rests entirely
    # on them cannot look strongly bound.
    admissible_bases=frozenset(
        {
            MarketBindingBasis.CONTRACT_ADDRESS_EXACT,
            MarketBindingBasis.VERIFIED_PROJECT_LINK,
            MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED,
            MarketBindingBasis.UNIQUE_SYMBOL_WITH_CONTEXT,
        }
    ),
)


def _level(value: int, steps: tuple[int, int, int, int]) -> QualitativeLevel:
    return LEVELS[sum(1 for step in steps if value >= step)]


def _top_five_dominates(features: SignalQualityFeatures, policy: SignalQualityPolicy) -> bool:
    """Whether five accounts dominate the discussion, where that can mean anything.

    A top-five share only carries information when there is a tail for it to
    dominate. With ``N`` authors posting evenly the share is already ``5/N``, so
    for small ``N`` the threshold is crossed by arithmetic alone: six people
    writing once each produce a top-five share of 0.83 and are not concentrated by
    any reading. The guard is therefore derived from the threshold rather than
    guessed — the term applies only where an even distribution would sit below it,
    which is exactly where exceeding it implies real skew. The author count itself
    has already measured what happens below that point.
    """
    authors = features.unique_authoring_count
    if authors < 1 or Decimal(5) / Decimal(authors) >= policy.top5_author_share_elevated:
        return False
    return features.top5_author_share >= policy.top5_author_share_elevated


def _attention(features: SignalQualityFeatures, policy: SignalQualityPolicy) -> QualitativeLevel:
    """How much was published. Loudness, explicitly not agreement or interest."""
    return _level(features.observation_count, policy.attention_steps)


def _breadth(features: SignalQualityFeatures, policy: SignalQualityPolicy) -> QualitativeLevel:
    """How many genuinely different people are behind the discussion.

    Starts from distinct authors, then pays back what duplication and
    concentration take away. Five hundred posts from four accounts repeating one
    sentence is not a wide conversation, and the demotions are what stop it from
    reading as one.
    """
    level = _level(features.unique_authoring_count, policy.breadth_steps)
    if features.duplicate_share >= policy.duplicate_share_severe:
        level = _shift(level, -2)
    elif features.duplicate_share >= policy.duplicate_share_elevated:
        level = _shift(level, -1)
    if features.top1_author_share >= policy.top1_author_share_severe:
        level = _shift(level, -2)
    elif _top_five_dominates(features, policy):
        level = _shift(level, -1)
    return level


def _manipulation(features: SignalQualityFeatures, policy: SignalQualityPolicy) -> QualitativeLevel:
    """Indicators of coordination. Never a finding of fraud, and never proof of bots.

    Each term is a structural observation about who posted what and when. People
    do sometimes share the same phrase honestly, so this is reported as a concern
    to weigh, not a verdict to act on.
    """
    level = QualitativeLevel.VERY_LOW
    if features.duplicate_share >= policy.duplicate_share_severe:
        level = _shift(level, 2)
    elif features.duplicate_share >= policy.duplicate_share_elevated:
        level = _shift(level, 1)
    if features.top1_author_share >= policy.top1_author_share_severe:
        level = _shift(level, 2)
    elif _top_five_dominates(features, policy):
        level = _shift(level, 1)
    if features.burst_share >= policy.burst_share_elevated:
        level = _shift(level, 1)
    return level


def _gaps(
    features: SignalQualityFeatures,
    policy: SignalQualityPolicy,
    breadth: QualitativeLevel,
    manipulation: QualitativeLevel,
) -> list[SignalGap]:
    gaps: list[SignalGap] = []
    if features.received_count == 0:
        gaps.append(SignalGap.NO_OBSERVATIONS)
    elif features.observation_count == 0 and features.excluded_outside_window_count > 0:
        # Data existed; none of it describes the interval we claim to report on.
        gaps.append(SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW)
    elif features.observation_count == 0:
        gaps.append(SignalGap.NO_OBSERVATIONS)
    if 0 < features.observation_count < policy.min_observations:
        gaps.append(SignalGap.TOO_FEW_OBSERVATIONS)
    if 0 < features.unique_authoring_count < policy.min_unique_authors:
        gaps.append(SignalGap.TOO_FEW_AUTHORS)
    if features.observation_count > 0 and features.strong_binding_count == 0:
        gaps.append(SignalGap.NO_STRONGLY_BOUND_OBSERVATIONS)
    if features.excluded_ambiguous_count > 0:
        gaps.append(SignalGap.AMBIGUOUS_REFERENCES_EXCLUDED)
    if features.excluded_outside_window_count > 0 and features.observation_count > 0:
        gaps.append(SignalGap.STALE_OBSERVATIONS_EXCLUDED)
    if features.source_count == 1 and features.observation_count > 0:
        gaps.append(SignalGap.SINGLE_SOURCE_ONLY)
    if features.coverage == CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET:
        # The provider had more and we stopped reading. What was collected is
        # still real, and it is the newest slice rather than the whole window.
        gaps.append(SignalGap.COLLECTION_TRUNCATED)
    if features.duplicate_share >= policy.duplicate_share_elevated:
        gaps.append(SignalGap.DUPLICATE_DOMINATED)
    if features.top1_author_share >= policy.top1_author_share_severe or _top_five_dominates(
        features, policy
    ):
        gaps.append(SignalGap.AUTHOR_CONCENTRATED)
    if breadth == QualitativeLevel.VERY_LOW and manipulation == QualitativeLevel.VERY_LOW:
        # Nothing structurally suspicious, simply too narrow to generalize from.
        gaps.append(SignalGap.TOO_FEW_AUTHORS)
    return gaps


def assess_structure(
    features: SignalQualityFeatures,
    policy: SignalQualityPolicy = SIGNAL_QUALITY_V1,
    *,
    source_unavailable: bool = False,
) -> SignalStructuralAssessment:
    """Derive the authoritative structural reading. No model input participates.

    ``INSUFFICIENT`` means no interpretation may be attempted at all — and,
    importantly, that no model is called: there is nothing to read. ``DEGRADED``
    means the set is interpretable but structurally compromised, which is the
    honest verdict on a campaign: the language is real data, the breadth is not.
    """
    attention = _attention(features, policy)
    breadth = _breadth(features, policy)
    manipulation = _manipulation(features, policy)
    gaps = _gaps(features, policy, breadth, manipulation)
    if source_unavailable:
        gaps.insert(0, SignalGap.SOURCE_UNAVAILABLE)

    if (
        source_unavailable
        or features.observation_count < policy.min_observations
        or features.unique_authoring_count < policy.min_unique_authors
    ):
        quality = SignalDataQuality.INSUFFICIENT
    elif (
        LEVEL_ORDER[manipulation] >= LEVEL_ORDER[QualitativeLevel.HIGH]
        or LEVEL_ORDER[breadth] <= LEVEL_ORDER[QualitativeLevel.LOW]
        or features.strong_binding_count == 0
        # A truncated stream is interpretable and is not a clean read of the
        # window it claims to describe, so it can never be better than degraded.
        or features.coverage == CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET
    ):
        quality = SignalDataQuality.DEGRADED
    else:
        quality = SignalDataQuality.USABLE
    return SignalStructuralAssessment(
        policy_version=policy.version,
        data_quality=quality,
        attention_level=attention,
        organic_breadth=breadth,
        manipulation_concern=manipulation,
        gaps=tuple(dict.fromkeys(gaps))[:12],
    )


def exceeds_ceiling(
    indication: SocialDemandIndication, breadth: QualitativeLevel, policy: SignalQualityPolicy
) -> bool:
    return DEMAND_ORDER[indication] > DEMAND_ORDER[policy.demand_ceiling(breadth)]
