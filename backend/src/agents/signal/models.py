"""SIGNAL contracts: what a social observation is, and what may be concluded from it.

SIGNAL is the social-attention specialist. Social input is the most adversarial
data this system consumes: unlike a block or a pool reserve, it is authored by
people who may want to be seen, and a promotional campaign is cheap to run. Three
things are therefore kept rigorously apart:

* **Observed facts** — what a source actually published, with its own timestamp,
  its author identity and how it was bound to this market;
* **Deterministic structure** — counts, duplication, author concentration and the
  manipulation indicators derived from them, computed in code from those facts;
* **Model interpretation** — what the language means, which is genuinely
  probabilistic and is recorded as advisory.

The second layer exists because the third cannot be trusted with it. A model can
be talked into calling a copy-paste campaign "broad organic support"; arithmetic
over unique authors cannot.

Sentiment is never demand. Positive language, high post counts, large engagement
and influencer amplification are four different observations, and none of them is
evidence that anyone wants to buy anything. Each is reported on its own axis so a
later FUSE can see which one actually fired.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
# Third-party post text is bounded hard. SIGNAL needs enough language to read a
# tone, never a corpus, and nothing longer is carried anywhere.
ObservationText = Annotated[str, Field(min_length=1, max_length=600)]
SafeSummary = Annotated[str, Field(min_length=1, max_length=400)]
EvmAddress = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{40}$")]
ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]

SIGNAL_OUTPUT_SCHEMA_VERSION: Literal[1] = 1


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SignalSource(StrEnum):
    """Which platform published an observation.

    Kept as provenance rather than flattened into one message list, because these
    platforms differ in how easy they are to flood, how identities work and what
    metadata they expose. A future policy may weight or exclude one; it can only
    do that if the distinction survives normalization.
    """

    X = "X"
    REDDIT = "REDDIT"
    FARCASTER = "FARCASTER"
    TELEGRAM_PUBLIC = "TELEGRAM_PUBLIC"
    NEWS = "NEWS"
    FORUM = "FORUM"


class ObservationKind(StrEnum):
    """What kind of act produced this observation.

    A repost is an attention event, not a second opinion, and a reply is a
    conversation turn rather than an independent authored position. Collapsing
    the three would let one post with a thousand shares read as a thousand people
    who agree.
    """

    ORIGINAL = "ORIGINAL"
    REPOST = "REPOST"
    REPLY = "REPLY"


class MarketBindingBasis(StrEnum):
    """How firmly an observation was tied to *this* market.

    A ticker is not an identity. ``$ABC`` is claimed by dozens of unrelated
    tokens across chains, so a symbol alone can never carry sentiment into a
    TradeCase. The bases below are ordered by how much they actually prove:

    * ``CONTRACT_ADDRESS_EXACT`` — the text contains this token's contract
      address on this chain. Deterministically checkable, and checked.
    * ``VERIFIED_PROJECT_LINK`` — the source is an account or domain the project
      itself is recorded as owning.
    * ``UNIQUE_SYMBOL_WITH_CONTEXT`` — the symbol plus corroborating context the
      adapter could resolve. Admissible but weaker, and counted separately.
    * ``AMBIGUOUS_SYMBOL`` — a bare ticker. Noise for this market.
    * ``UNRESOLVED`` — no usable binding at all.
    """

    CONTRACT_ADDRESS_EXACT = "CONTRACT_ADDRESS_EXACT"
    VERIFIED_PROJECT_LINK = "VERIFIED_PROJECT_LINK"
    UNIQUE_SYMBOL_WITH_CONTEXT = "UNIQUE_SYMBOL_WITH_CONTEXT"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    UNRESOLVED = "UNRESOLVED"


# Bindings that identify the token itself rather than a name it happens to share.
STRONG_BINDING_BASES = frozenset(
    {MarketBindingBasis.CONTRACT_ADDRESS_EXACT, MarketBindingBasis.VERIFIED_PROJECT_LINK}
)


class QualitativeLevel(StrEnum):
    """A coarse level, deliberately not a number.

    There is no calibrated probability behind any of this, and a figure like
    ``manipulation_risk = 0.83`` would invite exactly the false precision that a
    safety system must not carry.
    """

    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


LEVEL_ORDER: dict[QualitativeLevel, int] = {
    QualitativeLevel.VERY_LOW: 0,
    QualitativeLevel.LOW: 1,
    QualitativeLevel.MODERATE: 2,
    QualitativeLevel.HIGH: 3,
    QualitativeLevel.VERY_HIGH: 4,
}


class SentimentDirection(StrEnum):
    """Which way the language leans. ``UNCLEAR`` is not ``NEUTRAL``.

    Neutral means people discussed the asset without leaning either way. Unclear
    means the model could not read a direction at all. A system that maps silence
    or confusion onto "neutral" has invented an observation.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


class SentimentStrength(StrEnum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class SocialDemandIndication(StrEnum):
    """How far the language suggests people want to *own or use* the asset.

    Named for what it is. This is not order flow, not volume and not buying
    pressure; SIGNAL has no market authority and cannot observe a trade. It is a
    reading of stated interest, and it is capped by the deterministic breadth of
    who actually said it.
    """

    NONE = "NONE"
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


DEMAND_ORDER: dict[SocialDemandIndication, int] = {
    SocialDemandIndication.NONE: 0,
    SocialDemandIndication.WEAK: 1,
    SocialDemandIndication.MODERATE: 2,
    SocialDemandIndication.STRONG: 3,
}


class SignalDataQuality(StrEnum):
    """Whether the observation set can support an interpretation at all.

    Deliberately independent of sentiment. A flood of identical positive spam can
    be perfectly ``USABLE`` as data while saying nothing about broad support, and
    an empty feed is ``INSUFFICIENT`` rather than neutral.
    """

    USABLE = "USABLE"
    DEGRADED = "DEGRADED"
    INSUFFICIENT = "INSUFFICIENT"


class SignalGap(StrEnum):
    """Why the observation set is weaker than it looks. Never a sentiment value."""

    NO_OBSERVATIONS = "NO_OBSERVATIONS"
    ALL_OBSERVATIONS_OUTSIDE_WINDOW = "ALL_OBSERVATIONS_OUTSIDE_WINDOW"
    TOO_FEW_OBSERVATIONS = "TOO_FEW_OBSERVATIONS"
    TOO_FEW_AUTHORS = "TOO_FEW_AUTHORS"
    NO_STRONGLY_BOUND_OBSERVATIONS = "NO_STRONGLY_BOUND_OBSERVATIONS"
    AMBIGUOUS_REFERENCES_EXCLUDED = "AMBIGUOUS_REFERENCES_EXCLUDED"
    STALE_OBSERVATIONS_EXCLUDED = "STALE_OBSERVATIONS_EXCLUDED"
    SINGLE_SOURCE_ONLY = "SINGLE_SOURCE_ONLY"
    DUPLICATE_DOMINATED = "DUPLICATE_DOMINATED"
    AUTHOR_CONCENTRATED = "AUTHOR_CONCENTRATED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class SignalNarrative(StrEnum):
    """Bounded vocabulary for what the discussion is *about*.

    A closed set rather than free text, so a narrative can be counted and
    compared across cases instead of being reinvented by each model call.
    """

    UTILITY_OR_PRODUCT = "UTILITY_OR_PRODUCT"
    LISTING_OR_EXCHANGE = "LISTING_OR_EXCHANGE"
    PARTNERSHIP_CLAIM = "PARTNERSHIP_CLAIM"
    PRICE_SPECULATION = "PRICE_SPECULATION"
    MEME_OR_COMMUNITY = "MEME_OR_COMMUNITY"
    GIVEAWAY_OR_AIRDROP = "GIVEAWAY_OR_AIRDROP"
    PROMOTIONAL_CALL_TO_ACTION = "PROMOTIONAL_CALL_TO_ACTION"
    CRITICISM_OR_WARNING = "CRITICISM_OR_WARNING"
    SCAM_ALLEGATION = "SCAM_ALLEGATION"
    TEAM_OR_ROADMAP = "TEAM_OR_ROADMAP"


class ObservationEngagement(Immutable):
    """Counts a platform reported, when it reported any.

    Absent stays absent: a provider that does not expose reposts is not the same
    as a post nobody shared. Follower counts, verification badges and account age
    are deliberately *not* modelled — none of them is trust, and inventing fields
    no provider fills would invite a future "verified means credible" shortcut.
    """

    likes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)


class SignalObservation(Immutable):
    """One normalized public post, as a provider reported it.

    Provider-native shapes stop here: nothing downstream ever sees a vendor dict.
    Two timestamps are kept because they answer different questions —
    ``created_at`` is when the world produced this, ``received_at`` is when we
    happened to collect it, and only the first can make an observation fresh.
    """

    observation_id: UUID
    source: SignalSource
    source_native_id: Identifier
    # A stable pseudonymous handle for the author, normalized by the adapter.
    # Never a real name, never contact details, never a profile dump.
    author_id: Identifier
    kind: ObservationKind
    created_at: AwareDatetime
    received_at: AwareDatetime
    content: ObservationText
    language: Identifier | None = None
    engagement: ObservationEngagement | None = None
    # Present when this observation shares or answers another one.
    referenced_observation_id: UUID | None = None
    binding_basis: MarketBindingBasis
    # The address and chain the adapter claims this observation names. Both are
    # re-checked against the TradeCase token before the binding is believed.
    binding_address: EvmAddress | None = None
    binding_chain: Identifier | None = None
    provider: Identifier

    @model_validator(mode="after")
    def binding_matches_content(self) -> Self:
        if self.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_EXACT and (
            self.binding_address is None or self.binding_chain is None
        ):
            raise ValueError("An address binding must name the address and its chain")
        if self.kind != ObservationKind.ORIGINAL and self.referenced_observation_id is None:
            # A share or a reply that names nothing it responds to cannot be
            # distinguished from an original, which is exactly the confusion the
            # kind exists to prevent.
            raise ValueError("A repost or reply must reference the observation it responds to")
        return self


class SignalWindow(Immutable):
    """The interval SIGNAL claims to describe, anchored to source time.

    Analysing whatever a provider happened to return would let a year-old thread
    read as current sentiment, so the window is explicit, versioned by policy and
    applied to ``created_at`` alone.
    """

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("A signal window must end after it starts")
        return self

    def contains(self, moment: AwareDatetime) -> bool:
        return self.start <= moment <= self.end


class SourceBreakdown(Immutable):
    """How many admissible observations each platform contributed."""

    source: SignalSource
    observation_count: int = Field(ge=0)
    unique_author_count: int = Field(ge=0)


class DuplicateCluster(Immutable):
    """A group of observations whose normalized text is identical.

    Kept as a hash and a count, never as the repeated text itself. The
    representative is the one observation of the group a model may be shown.
    """

    content_hash: ContentHash
    observation_count: int = Field(ge=2)
    author_count: int = Field(ge=1)
    representative_id: UUID


class SignalQualityFeatures(Immutable):
    """Everything deterministic about the observation set.

    Computed in code from normalized observations, never asked of a model. These
    are the numbers a manipulation claim has to survive: if two authors wrote
    ninety per cent of the messages, no amount of confident prose makes the
    discussion broad.
    """

    window: SignalWindow
    observation_count: int = Field(ge=0)
    # Everyone who participated at all, resharers included.
    unique_author_count: int = Field(ge=0)
    # Everyone who actually wrote something. Breadth and concentration are
    # measured on this one: resharing amplifies a voice, it does not add one, so
    # counting resharers as authors is how a single account becomes a crowd.
    unique_authoring_count: int = Field(ge=0)
    original_count: int = Field(ge=0)
    repost_count: int = Field(ge=0)
    reply_count: int = Field(ge=0)
    unique_content_count: int = Field(ge=0)
    duplicate_clusters: tuple[DuplicateCluster, ...] = Field(default=(), max_length=20)
    # Share of original observations that sit inside a duplicate cluster.
    duplicate_share: Ratio = Decimal(0)
    largest_duplicate_cluster_share: Ratio = Decimal(0)
    # Shares of the *authored* observations, for the same reason.
    top1_author_share: Ratio = Decimal(0)
    top5_author_share: Ratio = Decimal(0)
    # Share of duplicate-cluster observations published inside one short
    # interval. A coordination indicator, never a proof of automation.
    burst_share: Ratio = Decimal(0)
    strong_binding_count: int = Field(ge=0)
    weak_binding_count: int = Field(ge=0)
    excluded_ambiguous_count: int = Field(ge=0)
    excluded_outside_window_count: int = Field(ge=0)
    excluded_unbound_count: int = Field(ge=0)
    received_count: int = Field(ge=0)
    sources: tuple[SourceBreakdown, ...] = Field(default=(), max_length=10)
    # The newest admissible observation. Freshness anchors here, never to a fetch.
    latest_observation_at: AwareDatetime | None = None
    oldest_observation_at: AwareDatetime | None = None
    content_hash_algorithm: Identifier

    @property
    def source_count(self) -> int:
        return len(self.sources)


class SignalStructuralAssessment(Immutable):
    """What the deterministic layer concludes, before any model is consulted.

    These four levels are policy output and stay authoritative. A model may
    explain them and may not contradict them.
    """

    policy_version: Identifier
    data_quality: SignalDataQuality
    attention_level: QualitativeLevel
    organic_breadth: QualitativeLevel
    manipulation_concern: QualitativeLevel
    gaps: tuple[SignalGap, ...] = Field(default=(), max_length=12)


class SignalRepresentative(Immutable):
    """One observation actually shown to the model, and why it was chosen.

    Recorded so the input digest covers the exact sample, and so a reviewer can
    tell a diversity pick from a duplicate-cluster representative rather than
    guessing how the sample was drawn.
    """

    observation_id: UUID
    source: SignalSource
    author_id: Identifier
    kind: ObservationKind
    created_at: AwareDatetime
    content_hash: ContentHash
    binding_basis: MarketBindingBasis
    selection_reason: Literal["DUPLICATE_CLUSTER", "AUTHOR_DIVERSITY", "RECENCY_FILL"]


class SignalTaskInput(Immutable):
    """Exactly what SIGNAL works from.

    No session, provider client, HTTP client, RPC client, wallet, signer or
    executor appears here, and there is no field through which one could arrive.
    ``supersedes_evidence_id`` is runtime bookkeeping for the envelope and is
    never shown to the model.
    """

    trade_case_id: UUID
    task_id: UUID
    chain: Identifier
    network: Identifier
    pair_id: Identifier
    base_asset_id: Identifier
    token_address: EvmAddress | None = None
    features: SignalQualityFeatures
    structure: SignalStructuralAssessment
    representatives: tuple[SignalRepresentative, ...] = Field(default=(), max_length=100)
    # The bounded excerpts the model is shown, paired to the representatives
    # above. Held for the duration of one call and never persisted as evidence.
    excerpts: tuple[ObservationText, ...] = Field(default=(), max_length=100)
    evaluated_at: AwareDatetime
    supersedes_evidence_id: UUID | None = None

    @model_validator(mode="after")
    def sample_is_coherent(self) -> Self:
        if len(self.excerpts) != len(self.representatives):
            raise ValueError("Every representative must carry exactly one excerpt")
        identifiers = [item.observation_id for item in self.representatives]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("A representative may appear only once in the sample")
        return self

    @property
    def representative_ids(self) -> frozenset[UUID]:
        return frozenset(item.observation_id for item in self.representatives)

    @property
    def author_ids(self) -> frozenset[str]:
        return frozenset(item.author_id for item in self.representatives)


class SignalAssessment(Immutable):
    """The model's bounded output.

    It cannot express a side, a size, an entry, a target, a route or an approval,
    because no such field exists. What it can express is what the language means —
    and even there, ``social_demand_indication`` is checked against the
    deterministic breadth of who actually spoke before it is believed.
    """

    schema_version: Literal[1] = SIGNAL_OUTPUT_SCHEMA_VERSION
    sentiment_direction: SentimentDirection
    sentiment_strength: SentimentStrength
    social_demand_indication: SocialDemandIndication
    narrative_tags: tuple[SignalNarrative, ...] = Field(default=(), max_length=6)
    # Advisory manipulation observations. They may add to the deterministic
    # concern; they can never talk it down.
    manipulation_observations: tuple[SignalGap, ...] = Field(default=(), max_length=6)
    cited_observation_ids: tuple[UUID, ...] = Field(default=(), max_length=12)
    summary: SafeSummary

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.narrative_tags)) != len(self.narrative_tags):
            raise ValueError("Narrative tags must be distinct")
        if len(set(self.cited_observation_ids)) != len(self.cited_observation_ids):
            raise ValueError("Cited observations must be distinct")
        if (
            self.sentiment_direction == SentimentDirection.UNCLEAR
            and self.sentiment_strength != SentimentStrength.WEAK
        ):
            # Claiming a strong reading of a direction that could not be read is
            # a contradiction in the output itself.
            raise ValueError("An unclear direction cannot carry more than weak strength")
        return self
