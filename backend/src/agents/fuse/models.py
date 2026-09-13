"""FUSE contracts: what a synthesis is, and the three things it can never become.

FUSE reads the evidence a case has accumulated and states, in one place, what it
adds up to. It is the only specialist that looks at other specialists, which is
exactly why its limits have to be structural rather than careful.

**It is not a vote.** There is no count, no average, no weight and no score. Four
specialists answering four different questions do not have commensurable
opinions: ORBIT's interest, SIGNAL's attention, VECTOR's geometry and ATLAS's
contract findings are not four readings of one quantity, and reducing them to
one would invent a number nobody measured. Positive sentiment cannot outvote a
holder-concentration blocker, because they are not on the same scale — they are
not on a scale at all.

**It cannot clear anything.** Hard blockers and unresolved gaps are derived here
from the sources' own structured verdicts. Nothing in this module can remove
one, and the shape of the output is the reason: blockers are computed, never
supplied, and a synthesis carrying none is a synthesis that found none.

**It has no authority.** No approval, no size, no notional, no slippage, no
route, no risk outcome. SENTINEL sizes and authorises from the canonical safety
evidence directly; FUSE summarising that evidence changes nothing about what
SENTINEL reads. A synthesis is a reading, and readings do not permit trades.

There is no model here. Every input is already a structured verdict produced by
a specialist that did the interpreting — a second interpretation layer would put
a probabilistic opinion on top of answers that are already settled, and leave
nobody able to say afterwards which of the two the system acted on.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.core.models import AgentRole
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
)

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Statement = Annotated[str, Field(min_length=1, max_length=300)]

FUSE_OUTPUT_SCHEMA_VERSION: Literal[1] = 1


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FuseDisposition(StrEnum):
    """What the currently admissible evidence adds up to.

    Deliberately its own vocabulary, sharing no member with `RiskOutcome` or any
    authorization enum. A reader who sees `COHERENT` must not be able to mistake
    it for an approval, and a reader who sees `BLOCKED` must not think SENTINEL
    has spoken. These four words describe evidence, and nothing else.
    """

    # Everything required is present, usable, and nothing contradicts anything.
    COHERENT = "COHERENT"
    # Usable, but something in it deserves to be said out loud before anyone
    # acts — degraded data quality, a thin setup, a tension between sources.
    CAUTION = "CAUTION"
    # At least one source states a fact that stops the case on its own merits.
    BLOCKED = "BLOCKED"
    # Something required is missing, stale or unknown. Not a judgement about the
    # market: a statement that the question cannot yet be answered.
    INSUFFICIENT = "INSUFFICIENT"


class BlockerOrigin(StrEnum):
    """Why a hard blocker exists, kept explicit so none can appear from nowhere."""

    # The source's own payload says so, through its acceptance semantics.
    SOURCE_ACCEPTANCE = "SOURCE_ACCEPTANCE"
    # The source's own structured verdict names a blocking finding.
    SOURCE_VERDICT = "SOURCE_VERDICT"


class GapOrigin(StrEnum):
    """Why something required is not usable."""

    MISSING = "MISSING"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    STALE = "STALE"
    INSUFFICIENT = "INSUFFICIENT"


class HardBlocker(Immutable):
    """A fact from one source that stops the case, with the source named.

    Every field is derived from the source evidence. There is no constructor
    path that invents one and none that removes one, so a synthesis reporting no
    blockers is reporting a measurement rather than an opinion.
    """

    code: Code
    role: AgentRole
    evidence_type: EvidenceType
    evidence_id: UUID
    origin: BlockerOrigin
    statement: Statement


class UnresolvedGap(Immutable):
    """Something required that could not be used, and why.

    Distinct from a blocker in the way the whole system keeps them distinct: a
    blocker is "we looked and it is bad", a gap is "we could not look". Both stop
    a case; only one is a statement about the asset.
    """

    code: Code
    role: AgentRole
    evidence_type: EvidenceType
    origin: GapOrigin
    safety_critical: bool = Field(strict=True)
    evidence_id: UUID | None = None
    statement: Statement


class SynthesisFactor(Immutable):
    """One observation about the evidence, supporting or cautioning.

    Factors are qualitative and carry no weight, because a weight would be the
    first step toward a total, and a total would be a vote. They are things
    worth saying, each attributed to the evidence that says it.
    """

    code: Code
    role: AgentRole
    evidence_type: EvidenceType
    evidence_id: UUID
    statement: Statement


class SourceReference(Immutable):
    """Exactly which envelope a synthesis was built from.

    Identity, fingerprint and freshness together, because a synthesis is only
    meaningful against the specific evidence it read. The fingerprint is what
    makes "this synthesis is about that evidence" checkable rather than assumed.
    """

    role: AgentRole
    evidence_type: EvidenceType
    evidence_id: UUID
    submission_fingerprint: Digest
    status: EvidenceStatus
    acceptance: EvidenceAcceptance
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    required: bool = Field(strict=True)
    safety_critical: bool = Field(strict=True)


class EvidenceSynthesis(Immutable):
    """The deterministic reading of one case's currently admissible evidence.

    ``valid_until`` is the earliest expiry among the sources, never a window
    starting now. A summary written at noon over evidence that expires at
    half past does not make that evidence last until one — and if freshness were
    anchored to synthesis time, re-running FUSE would launder stale facts into
    fresh-looking ones, which is the single most dangerous thing a summariser
    can do.
    """

    policy_version: Identifier
    disposition: FuseDisposition
    hard_blockers: tuple[HardBlocker, ...] = Field(default=(), max_length=24)
    unresolved_gaps: tuple[UnresolvedGap, ...] = Field(default=(), max_length=24)
    support_factors: tuple[SynthesisFactor, ...] = Field(default=(), max_length=16)
    caution_factors: tuple[SynthesisFactor, ...] = Field(default=(), max_length=16)
    sources: tuple[SourceReference, ...] = Field(min_length=1, max_length=12)
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    evaluated_at: AwareDatetime
    input_digest: Digest

    @model_validator(mode="after")
    def coherent(self) -> Self:
        # The disposition is a function of what was found, not an independent
        # opinion that happens to sit beside it. Stated as a constraint so the
        # two can never drift: a synthesis claiming COHERENT while carrying a
        # blocker would be the exact failure this phase exists to prevent.
        if self.hard_blockers and self.disposition != FuseDisposition.BLOCKED:
            raise ValueError("A synthesis carrying a hard blocker is blocked")
        if self.disposition == FuseDisposition.BLOCKED and not self.hard_blockers:
            raise ValueError("A blocked synthesis must name what blocks it")
        if (
            self.unresolved_gaps
            and not self.hard_blockers
            and self.disposition != FuseDisposition.INSUFFICIENT
        ):
            raise ValueError("Unresolved gaps leave the question unanswered")
        if self.disposition == FuseDisposition.INSUFFICIENT and not self.unresolved_gaps:
            raise ValueError("An insufficient synthesis must name what is missing")
        if self.disposition == FuseDisposition.CAUTION and not self.caution_factors:
            raise ValueError("A cautioning synthesis must say what to be careful of")
        if self.valid_until <= self.observed_at:
            raise ValueError("A synthesis cannot expire before the evidence it read")
        return self

    @property
    def is_actionable(self) -> bool:
        """Whether the evidence hangs together. Never whether to trade."""
        return self.disposition in (FuseDisposition.COHERENT, FuseDisposition.CAUTION)


# --------------------------------------------------------------- bounded views
#
# One view per source type, carrying the decision-relevant facts and nothing
# else. Full payloads are deliberately not forwarded: they contain advisory
# prose, cited observation ids, provider metadata and recorded market structure,
# none of which a synthesis reads, and all of which would be copied into durable
# storage a second time if it travelled.


class DiscoveryView(Immutable):
    """ORBIT: whether the candidate was worth opening a case for."""

    classification: Code | None = None
    strength: Code | None = None
    reason_codes: tuple[Code, ...] = Field(default=(), max_length=12)
    data_gaps: tuple[Code, ...] = Field(default=(), max_length=12)


class OnchainView(Immutable):
    """ATLAS: the contract findings, as the three integrity axes plus verdict.

    The axes are carried separately from the verdict because they say different
    things: `FAIL` is a measurement, `UNKNOWN` is its absence, and a synthesis
    that could not tell them apart would report a missing holder count as a
    clean one.
    """

    holder_integrity: Literal["PASS", "FAIL", "UNKNOWN"]
    dev_wallet_integrity: Literal["PASS", "FAIL", "UNKNOWN"]
    contract_integrity: Literal["PASS", "FAIL", "UNKNOWN"]
    verdict: Code | None = None
    blockers: tuple[Code, ...] = Field(default=(), max_length=12)
    data_gaps: tuple[Code, ...] = Field(default=(), max_length=12)


class SentimentView(Immutable):
    """SIGNAL: the assessment together with everything that qualifies it.

    Quality travels with the reading, always. A POSITIVE assessment drawn from a
    concentrated campaign is not the same fact as a POSITIVE assessment drawn
    from broad organic attention, and a synthesis that dropped the qualifier
    would promote the first into the second silently.
    """

    assessment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN"]
    data_quality: Code | None = None
    attention_level: Code | None = None
    organic_breadth: Code | None = None
    manipulation_concern: Code | None = None
    gaps: tuple[Code, ...] = Field(default=(), max_length=12)


class TradeSetupView(Immutable):
    """VECTOR: the geometry, its identity, and when it stops being true."""

    setup_id: UUID
    side: Code
    setup_fingerprint: Digest | None = None
    trigger_type: Code | None = None
    valid_from: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    # How much recorded history the setup was drawn from. Carried because a
    # setup grounded on a handful of bars is a different kind of claim from one
    # grounded on a full window, and the difference is worth saying out loud.
    history_bar_count: int | None = Field(default=None, strict=True, ge=0)
    history_timeframe: Identifier | None = None
    reason_codes: tuple[Code, ...] = Field(default=(), max_length=12)


class FuseSourceEvidence(Immutable):
    """One admissible source: its reference, and the bounded view of its content."""

    reference: SourceReference
    discovery: DiscoveryView | None = None
    onchain: OnchainView | None = None
    sentiment: SentimentView | None = None
    trade_setup: TradeSetupView | None = None

    @model_validator(mode="after")
    def exactly_one_view(self) -> Self:
        views = [self.discovery, self.onchain, self.sentiment, self.trade_setup]
        if sum(view is not None for view in views) != 1:
            raise ValueError("A source carries exactly one view of its own type")
        return self


class MissingSource(Immutable):
    """A required source that could not be read, and why. Never a view."""

    role: AgentRole
    evidence_type: EvidenceType
    origin: GapOrigin
    safety_critical: bool = Field(strict=True)
    evidence_id: UUID | None = None
    detail: Code | None = None


class FuseTaskInput(Immutable):
    """Everything FUSE is given, and nothing else.

    No session, no repository, no provider client, no reasoning provider, no
    ability to ask for another case's evidence or for a different version of
    this one. The server decided what is current and what is admissible before
    this was built; the worker reads the answer and cannot re-open the question.
    """

    trade_case_id: UUID
    task_id: UUID
    workflow_version: Identifier
    policy_version: Identifier
    sources: tuple[FuseSourceEvidence, ...] = Field(default=(), max_length=12)
    missing: tuple[MissingSource, ...] = Field(default=(), max_length=12)
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def one_per_type(self) -> Self:
        seen = [source.reference.evidence_type for source in self.sources]
        if len(set(seen)) != len(seen):
            raise ValueError("One current envelope per evidence type")
        overlap = {item.evidence_type for item in self.missing} & set(seen)
        if overlap:
            raise ValueError("Evidence cannot be both present and missing")
        if not self.sources and not self.missing:
            raise ValueError("A synthesis needs something to synthesize")
        return self


class FuseReasonCode(StrEnum):
    """Why no synthesis could honestly be recorded.

    Separate from `FuseDisposition` on purpose. A disposition is a reading of
    evidence; these are reasons there was no reading to give. Conflating them
    would let "nothing to read" appear in history as a finding about the market.
    """

    NO_ADMISSIBLE_EVIDENCE = "NO_ADMISSIBLE_EVIDENCE"
    SOURCES_NOT_CONCURRENT = "SOURCES_NOT_CONCURRENT"
