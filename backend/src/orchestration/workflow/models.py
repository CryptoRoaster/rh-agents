"""Immutable workflow contracts and typed evidence payloads."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.core.models import AgentRole, RiskOutcome, Side
from src.markets.models import MarketIdentity
from src.risk.authorization import RiskAuthorization, classify_risk_authorization

Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Confidence = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
# A proportion of an observation set. Exact, and never a float.
Share = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class WorkflowErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    CONCURRENCY_CONFLICT = "CONCURRENCY_CONFLICT"
    TERMINAL_CASE = "TERMINAL_CASE"
    EVIDENCE_BINDING = "EVIDENCE_BINDING"
    EVIDENCE_SUPERSESSION = "EVIDENCE_SUPERSESSION"
    TASK_TRANSITION = "TASK_TRANSITION"
    RISK_BINDING = "RISK_BINDING"
    EVIDENCE_INTEGRITY = "EVIDENCE_INTEGRITY"


class WorkflowFailure(Exception):
    def __init__(self, code: WorkflowErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class TradeCaseStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    BLOCKED = "BLOCKED"
    READY_FOR_TRIGGER = "READY_FOR_TRIGGER"
    TRIGGERED = "TRIGGERED"
    EXECUTION_EVIDENCE_PENDING = "EXECUTION_EVIDENCE_PENDING"
    READY_FOR_RISK = "READY_FOR_RISK"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_LIMITED = "RISK_LIMITED"
    RISK_APPROVED = "RISK_APPROVED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


# A rejection is final for this TradeCase; materially new conditions require a
# new TradeCase rather than reopening a rejected one. RISK_APPROVED and
# RISK_LIMITED are deliberately absent: both are authorizations that the
# evaluator must be able to revoke when their safety inputs change.
TERMINAL_CASE_STATUSES = frozenset(
    {
        TradeCaseStatus.RISK_REJECTED,
        TradeCaseStatus.EXPIRED,
        TradeCaseStatus.CANCELLED,
    }
)

RISK_AUTHORIZED_CASE_STATUSES = frozenset(
    {
        TradeCaseStatus.RISK_APPROVED,
        TradeCaseStatus.RISK_LIMITED,
    }
)


class SpecialistTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_TASK_STATUSES = frozenset(
    {
        SpecialistTaskStatus.SUCCEEDED,
        SpecialistTaskStatus.FAILED,
        SpecialistTaskStatus.CANCELLED,
        SpecialistTaskStatus.EXPIRED,
    }
)


class EvidenceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"
    STALE = "STALE"


class EvidenceAcceptance(StrEnum):
    """Whether an available envelope's own content satisfies its requirement.

    Deliberately a second axis, independent of EvidenceStatus. Availability says
    whether a fact could be observed; acceptance says what the observed fact
    means. A known-bad fact is AVAILABLE and BLOCKED — never disguised as UNKNOWN,
    because "we measured this and it is dangerous" and "we could not measure this"
    are different states that must stay distinguishable in the audit record.

    Derived deterministically from the typed payload. No worker, model, FUSE or
    COMMANDER can set or override it.
    """

    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT = "INSUFFICIENT"


class EvidenceType(StrEnum):
    DISCOVERY = "DISCOVERY_EVIDENCE"
    ONCHAIN = "ONCHAIN_EVIDENCE"
    SENTIMENT = "SENTIMENT_EVIDENCE"
    TRADE_SETUP = "TRADE_SETUP_EVIDENCE"
    TRIGGER = "TRIGGER_EVIDENCE"
    LIQUIDITY_EXECUTION = "LIQUIDITY_EXECUTION_EVIDENCE"


class EvidenceProvenance(Immutable):
    source: Identifier
    reference_id: UUID
    source_version: Identifier | None = None


SafeSummary = Annotated[str, Field(min_length=1, max_length=400)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DiscoveryAssessment(Immutable):
    """Optional verified discovery result plus the provenance that produced it.

    Role-agnostic and provider-neutral on purpose: the workflow records what was
    concluded and from which inputs, instructions and model, without depending on
    any specialist's internal schema. Only bounded structured fields are kept —
    never a raw vendor payload, never hidden model reasoning.
    """

    classification: Code
    strength: Code
    reason_codes: tuple[Code, ...] = Field(min_length=1, max_length=12)
    data_gaps: tuple[Code, ...] = Field(default=(), max_length=12)
    cited_observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    summary: SafeSummary
    input_digest: Digest
    prompt_version: Identifier
    prompt_hash: Digest
    reasoning_provider: Identifier
    reasoning_model: Identifier
    output_schema_version: int = Field(ge=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class AcceptancePayload(Immutable):
    """Base for typed evidence payloads.

    Most evidence carries no content-level policy of its own; its availability is
    the whole question. Payloads that do carry one override ``acceptance``.
    """

    def acceptance(self) -> EvidenceAcceptance:
        return EvidenceAcceptance.ACCEPTED


class DiscoveryPayload(AcceptancePayload):
    kind: Literal["discovery"] = "discovery"
    discovery_reference: UUID
    # Absent on the provenance envelope written when a case is opened; present
    # once a discovery worker has actually assessed the candidate.
    assessment: DiscoveryAssessment | None = None


class OnchainAdvisoryFinding(Immutable):
    """Model commentary. Advisory only; it never changes a domain verdict."""

    kind: Literal["VERIFIED_FACT", "INFERENCE"]
    code: Code
    statement: Annotated[str, Field(min_length=1, max_length=300)]
    referenced_addresses: tuple[Identifier, ...] = Field(default=(), max_length=8)


class OnchainIntelligence(Immutable):
    """The deterministic record behind an on-chain verdict.

    Verdict, blockers, gaps, per-domain availability and provenance are all
    produced by policy. ``advisory_*`` fields carry optional model commentary and
    have no authority over any of it.
    """

    verdict: Code
    policy_version: Identifier
    blockers: tuple[Code, ...] = Field(default=(), max_length=12)
    data_gaps: tuple[Code, ...] = Field(default=(), max_length=12)
    domain_status: dict[str, str]
    chain_id: int = Field(gt=0)
    block_number: int = Field(ge=0)
    snapshot_digest: Digest
    advisory_summary: SafeSummary | None = None
    advisory_findings: tuple[OnchainAdvisoryFinding, ...] = Field(default=(), max_length=10)
    reasoning_provider: Identifier | None = None
    reasoning_model: Identifier | None = None
    prompt_version: Identifier | None = None
    prompt_hash: Digest | None = None


class OnchainPayload(AcceptancePayload):
    """On-chain integrity across three fact domains.

    Each domain is one of three genuinely different states: PASS means the domain
    was established and is acceptable, FAIL means it was established and violates
    policy, UNKNOWN means it could not be established at all. Collapsing FAIL into
    UNKNOWN would hide a measured danger behind a missing measurement.
    """

    kind: Literal["onchain"] = "onchain"
    holder_integrity: Literal["PASS", "FAIL", "UNKNOWN"]
    dev_wallet_integrity: Literal["PASS", "FAIL", "UNKNOWN"]
    contract_integrity: Literal["PASS", "FAIL", "UNKNOWN"]

    # Deterministic detail recorded alongside the three domain verdicts, so a
    # future FUSE never has to parse prose to learn why.
    intelligence: "OnchainIntelligence | None" = None

    @property
    def domains(self) -> tuple[str, str, str]:
        return (self.holder_integrity, self.dev_wallet_integrity, self.contract_integrity)

    def acceptance(self) -> EvidenceAcceptance:
        # A known violation blocks even though the fact is perfectly available;
        # a domain that could not be established is insufficient, not safe.
        if "FAIL" in self.domains:
            return EvidenceAcceptance.BLOCKED
        if "UNKNOWN" in self.domains:
            return EvidenceAcceptance.INSUFFICIENT
        return EvidenceAcceptance.ACCEPTED


class SentimentSourceMetrics(Immutable):
    """Deterministic structure of the social observation set behind a reading.

    Counts and shares only, computed from normalized observations rather than
    asked of a model, so a future FUSE can see *why* a sentiment reading is worth
    what it is worth. Post text never appears here: a duplicate cluster is a hash
    and a count, which is enough to prove repetition without copying a stranger's
    writing into this system's records.
    """

    observation_count: int = Field(ge=0)
    unique_author_count: int = Field(ge=0)
    unique_authoring_count: int = Field(ge=0)
    original_count: int = Field(ge=0)
    repost_count: int = Field(ge=0)
    reply_count: int = Field(ge=0)
    unique_content_count: int = Field(ge=0)
    duplicate_cluster_count: int = Field(ge=0)
    duplicate_share: Share = Decimal(0)
    largest_duplicate_cluster_share: Share = Decimal(0)
    top1_author_share: Share = Decimal(0)
    top5_author_share: Share = Decimal(0)
    burst_share: Share = Decimal(0)
    strong_binding_count: int = Field(ge=0)
    weak_binding_count: int = Field(ge=0)
    excluded_ambiguous_count: int = Field(ge=0)
    excluded_outside_window_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    # Whether the provider ran out of results or a local budget stopped the read.
    coverage: Code | None = None
    sources: tuple[Code, ...] = Field(default=(), max_length=10)
    window_seconds: int = Field(gt=0)
    # Source-published times, never fetch receipts. Freshness anchors here.
    oldest_observation_at: AwareDatetime | None = None
    latest_observation_at: AwareDatetime | None = None
    content_hash_algorithm: Identifier


class SentimentIntelligence(Immutable):
    """The record behind a sentiment reading, split by who established what.

    ``data_quality``, ``attention_level``, ``organic_breadth`` and
    ``manipulation_concern`` are produced by a deterministic policy from the
    metrics above and stay authoritative. ``sentiment_*``,
    ``social_demand_indication``, ``narrative_tags`` and ``advisory_summary`` are
    model interpretation, recorded alongside and never in place of them.

    Sentiment direction and social demand are separate fields on purpose.
    Approving language is not an intention to buy, and one field would make the
    two indistinguishable the moment anything downstream read it.
    """

    policy_version: Identifier
    data_quality: Code
    attention_level: Code
    organic_breadth: Code
    manipulation_concern: Code
    gaps: tuple[Code, ...] = Field(default=(), max_length=12)
    metrics: SentimentSourceMetrics
    input_digest: Digest
    sentiment_direction: Code | None = None
    sentiment_strength: Code | None = None
    social_demand_indication: Code | None = None
    narrative_tags: tuple[Code, ...] = Field(default=(), max_length=6)
    advisory_manipulation_observations: tuple[Code, ...] = Field(default=(), max_length=6)
    advisory_summary: SafeSummary | None = None
    cited_observation_ids: tuple[UUID, ...] = Field(default=(), max_length=12)
    prompt_version: Identifier | None = None
    prompt_hash: Digest | None = None
    reasoning_provider: Identifier | None = None
    reasoning_model: Identifier | None = None
    output_schema_version: int | None = Field(default=None, ge=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class SentimentPayload(AcceptancePayload):
    """Social sentiment. Required by the workflow, and deliberately not safety-critical.

    Acceptance stays unconditional: a negative reading is a valid observation, not
    a blocker. Whether sentiment *should* stop a case is a synthesis question for
    a later FUSE, and encoding an answer here would quietly turn a mood into a
    veto. What does gate the workflow is availability — an unusable observation
    set leaves the requirement unmet, which is a data question rather than an
    opinion about the token.
    """

    kind: Literal["sentiment"] = "sentiment"
    assessment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN"]
    intelligence: "SentimentIntelligence | None" = None


class TradeSetupTrigger(Immutable):
    """The condition a future PULSE watches for, in terms it can decide.

    Deliberately a comparison rather than a description. A trigger expressed as
    prose would need a model to evaluate it, which would put a second
    probabilistic judgement between the setup and the act and leave no way to say
    afterwards what the system had been waiting for.
    """

    type: Code
    price_basis: Code
    reference_price: Positive | None = None
    zone_low: Positive | None = None
    zone_high: Positive | None = None
    valid_from: AwareDatetime
    expires_at: AwareDatetime


class RecordedBar(Immutable):
    """One closed interval exactly as VECTOR was shown it.

    Decimal throughout and no provider metadata: this is the normalized fact the
    reasoning was performed over, not a copy of a response.
    """

    opened_at: AwareDatetime
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume: Nonnegative


class RecordedMarketStructure(Immutable):
    """The bounded market structure an accepted setup was drawn from.

    A digest proves two inputs are equal; it cannot say what either one was. If
    the provider revises a candle, changes its normalization, or is replaced —
    or if our own normalization changes — a digest alone leaves "what exact
    market structure caused this setup?" unanswerable. The standing invariant is
    that decisions are traceable, so the answer is kept rather than referenced.

    This is deliberately not a market-data warehouse. It is the bounded input to
    one decision, stored with that decision: at most a couple of hundred
    normalized bars, no raw provider payload, no request metadata, no headers, no
    retrieval latency, no credential. Retrieval time is absent by construction —
    two fetches of the same closed bars produce the same record and the same
    digest.
    """

    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    base_asset_id: Identifier
    quote_asset_id: Identifier
    provider: Identifier
    # The provider's own spelling, verbatim. Recording a normalized variant here
    # would mean reconstruction had to transform it back, which is precisely the
    # kind of drift a durable decision input exists to rule out.
    timeframe: Identifier
    interval_seconds: int = Field(gt=0)
    price_basis: Code
    coverage: Code
    requested_bars: int = Field(gt=0)
    missing_intervals: int = Field(ge=0)
    window_start: AwareDatetime
    window_end: AwareDatetime
    observed_range_low: Positive
    observed_range_high: Positive
    policy_version: Identifier
    # Bounded hard. This is one decision's input, never an archive.
    bars: tuple[RecordedBar, ...] = Field(min_length=1, max_length=200)
    # Self-verifying: recomputed from the bars above, it must equal this.
    structure_digest: Digest


class TradeSetupDetail(Immutable):
    """The structured record behind a setup, for PULSE and a future FUSE.

    The legacy fields above stay exactly as they were; everything a watcher or a
    synthesiser would otherwise have to infer from prose lives here instead —
    which side of an entry band, what condition is being waited for, when the
    proposal stops being current, and which input and instructions produced it.
    """

    setup_fingerprint: Digest
    policy_version: Identifier
    kind: Code
    price_basis: Code
    entry_low: Positive
    entry_high: Positive
    # The observed price the proposal was drawn from, so a reader can see how far
    # the levels sat from the market at the time.
    reference_price: Positive
    expires_at: AwareDatetime
    trigger: TradeSetupTrigger
    reason_codes: tuple[Code, ...] = Field(default=(), max_length=8)
    summary: SafeSummary
    input_digest: Digest
    # Which market structure the proposal was drawn from. Bounded facts and a
    # digest rather than a copy of the bars: enough to answer afterwards which
    # window, which timeframe and which provider produced a setup, without
    # turning the evidence table into a candle archive.
    history_provider: Identifier | None = None
    history_timeframe: Identifier | None = None
    history_bar_count: int | None = Field(default=None, ge=0)
    history_window_start: AwareDatetime | None = None
    history_window_end: AwareDatetime | None = None
    history_coverage: Code | None = None
    observed_range_low: Positive | None = None
    observed_range_high: Positive | None = None
    # The exact normalized structure the model reasoned over. Additive and
    # optional, so evidence written before it stays readable.
    structure: "RecordedMarketStructure | None" = None
    prompt_version: Identifier | None = None
    prompt_hash: Digest | None = None
    reasoning_provider: Identifier | None = None
    reasoning_model: Identifier | None = None
    output_schema_version: int | None = Field(default=None, ge=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class TradeSetupPayload(AcceptancePayload):
    """A proposed setup. Analytical evidence, never an authorization.

    Acceptance stays unconditional: a structurally invalid proposal never becomes
    evidence in the first place, because the worker refuses it before submission
    rather than recording it as available and letting the workflow sort it out.
    What this payload asserts is that a setup was proposed — not that anyone may
    act on it, which remains PULSE's, ANCHOR's and SENTINEL's question in turn.
    """

    kind: Literal["trade_setup"] = "trade_setup"
    setup_id: UUID
    side: Side
    entry_price: Positive
    invalidation_price: Positive
    target_prices: tuple[Positive, ...] = Field(min_length=1)
    # Absent on evidence written before Phase 2H; present for anything a VECTOR
    # worker produced.
    setup: "TradeSetupDetail | None" = None


class TriggerDetail(Immutable):
    """What a monitor actually compared, and to what.

    Enough bounded fact to answer "why did this trigger?" without prose and
    without a model having been involved: the condition, the price, the moment
    the market had that price, the market it was, and the deterministic policy
    that judged it. There is no confidence, no score and no sentiment, because a
    comparison between two Decimals has none of those things.
    """

    setup_id: UUID
    setup_fingerprint: Digest
    policy_version: Identifier
    trigger_type: Code
    price_basis: Code
    # Present for the threshold conditions, absent for the range.
    reference_price: Positive | None = None
    # Present for the range condition, absent for the thresholds.
    zone_low: Positive | None = None
    zone_high: Positive | None = None
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    # The market's own account of when it had this price. Never the moment it was
    # read: a stale price fetched a second ago is still stale.
    observed_at: AwareDatetime
    evaluated_at: AwareDatetime
    observation_id: UUID
    snapshot_id: UUID
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    provider: Identifier
    is_fixture: bool = Field(strict=True)
    trigger_digest: Digest


class TriggerPayload(AcceptancePayload):
    """A factual observation that a condition became true.

    Not an authorization and not an opinion. It records that at a stated moment
    the authoritative setup's condition held against a stated recorded price. What
    anyone may do about that remains ANCHOR's and SENTINEL's question.
    """

    kind: Literal["trigger"] = "trigger"
    setup_evidence_id: UUID
    observed_price: Positive
    trigger_code: Code
    # Absent on evidence written before Phase 2I; present for anything a PULSE
    # monitor produced.
    detail: "TriggerDetail | None" = None


class QuotedLadderPoint(Immutable):
    """One tested size and what the market said about it.

    Kept whether it passed or failed. "Why did ANCHOR say five hundred?" is
    answered as much by the size that was refused as by the one that was not, and
    a digest alone would prove the ladder unchanged without saying what it held.
    """

    notional: Positive
    accepted: bool = Field(strict=True)
    amount_out: int | None = Field(default=None, strict=True, ge=0)
    effective_price: Positive | None = None
    execution_deviation_bps: Decimal | None = Field(default=None, allow_inf_nan=False)
    provider_price_impact_bps: Nonnegative | None = None
    route_hops: int | None = Field(default=None, strict=True, ge=0)
    venues: tuple[Identifier, ...] = Field(default=(), max_length=32)
    quoted_at: AwareDatetime | None = None
    source_block_number: int | None = Field(default=None, strict=True, ge=0)
    rejection: Code | None = None


class ExecutionAssessmentDetail(Immutable):
    """What the market was shown to support, and how that was established.

    ``market_capacity_notional`` is what the *market* will bear, never what
    anyone may trade: SENTINEL decides that, from facts this evidence cannot
    see. It is meaningless without ``capacity_semantics`` beside it, because a
    bounded search that passed every size it tried has learned a floor rather
    than a ceiling — and a reader who mistook the one for the other would size
    against a number that was never a limit.
    """

    policy_version: Identifier
    capacity_semantics: Code
    reason_code: Code
    market_capacity_notional: Positive | None = None
    first_rejected_notional: Positive | None = None
    reference_price: Positive
    reference_price_basis: Code
    reference_observed_at: AwareDatetime
    effective_price_at_capacity: Positive | None = None
    execution_deviation_bps_at_capacity: Decimal | None = Field(default=None, allow_inf_nan=False)
    payment_asset_id: Identifier
    target_asset_id: Identifier
    quote_provider: Identifier
    quote_requests: int = Field(ge=0)
    ladder: tuple[QuotedLadderPoint, ...] = Field(min_length=1, max_length=12)
    evaluated_at: AwareDatetime
    execution_digest: Digest


class LiquidityExecutionPayload(AcceptancePayload):
    """What the executable market supports for one triggered setup.

    Analytical evidence about execution conditions, never an authorisation. It
    does not say a trade should happen, how large it should be, or that the price
    will still be there — a future executor must re-quote immediately before
    acting, because a quote is an offer at a moment and this records the moment.
    """

    kind: Literal["liquidity_execution"] = "liquidity_execution"
    setup_evidence_id: UUID
    trigger_evidence_id: UUID
    quoted_price: Positive | None = None
    liquidity_usd: Nonnegative | None = None
    estimated_slippage_bps: Nonnegative | None = Field(default=None, le=10000)
    price_impact_bps: Nonnegative | None = Field(default=None, le=10000)
    # The legacy scalar. It never over-claims — it is a size actually tested and
    # accepted — but it cannot express that the true capacity may be higher, so
    # anything reasoning about capacity reads the detail below instead.
    maximum_safe_size_usd: Nonnegative | None = None
    routing_provenance: Identifier | None = None
    # Absent on evidence written before Phase 2J; present for anything an ANCHOR
    # worker produced.
    execution: "ExecutionAssessmentDetail | None" = None

    def acceptance(self) -> EvidenceAcceptance:
        """Known-bad execution conditions block rather than merely being recorded.

        A market that demonstrably cannot support the trade is a fact, and an
        available one: the provider answered. Treating it as merely present would
        let a case proceed to risk on evidence that says execution is impossible.
        """
        detail = self.execution
        if detail is None:
            return EvidenceAcceptance.ACCEPTED
        if detail.capacity_semantics == "UNKNOWN":
            return EvidenceAcceptance.INSUFFICIENT
        if detail.market_capacity_notional is None:
            return EvidenceAcceptance.BLOCKED
        return EvidenceAcceptance.ACCEPTED


EvidencePayload = Annotated[
    DiscoveryPayload
    | OnchainPayload
    | SentimentPayload
    | TradeSetupPayload
    | TriggerPayload
    | LiquidityExecutionPayload,
    Field(discriminator="kind"),
]


PAYLOAD_KIND = {
    EvidenceType.DISCOVERY: "discovery",
    EvidenceType.ONCHAIN: "onchain",
    EvidenceType.SENTIMENT: "sentiment",
    EvidenceType.TRADE_SETUP: "trade_setup",
    EvidenceType.TRIGGER: "trigger",
    EvidenceType.LIQUIDITY_EXECUTION: "liquidity_execution",
}


class EvidenceSubmission(Immutable):
    idempotency_key: Identifier
    producer_role: AgentRole
    evidence_type: EvidenceType
    schema_version: Literal[1] = 1
    provenance: EvidenceProvenance
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    status: EvidenceStatus
    confidence: Confidence | None = None
    reason_codes: tuple[Code, ...] = ()
    payload: EvidencePayload
    correlation_id: UUID
    supersedes_id: UUID | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError("Evidence validity must extend beyond observation time")
        if self.payload.kind != PAYLOAD_KIND[self.evidence_type]:
            raise ValueError("Evidence payload does not match evidence type")
        if self.confidence is not None and self.evidence_type != EvidenceType.SENTIMENT:
            raise ValueError("Confidence is only meaningful for sentiment evidence")
        if self.status != EvidenceStatus.AVAILABLE and not self.reason_codes:
            raise ValueError("Unavailable evidence requires a safe reason code")
        if self.status == EvidenceStatus.AVAILABLE and isinstance(
            self.payload, LiquidityExecutionPayload
        ):
            # Available execution evidence must have substance. Originally that
            # meant all six legacy scalars; a full assessment carries strictly
            # more — the ladder, the reference it was judged against and the
            # semantics of its capacity — so it satisfies the same intent.
            #
            # This also lets a market that demonstrably cannot be traded be
            # recorded as available and blocking. Under the scalar-only rule it
            # could not: there is no quoted price for a route that does not
            # exist, so a known-bad finding would have had to masquerade as an
            # unknown one, which is the distinction execution evidence most needs
            # to keep.
            if self.payload.execution is None and any(
                value is None
                for value in (
                    self.payload.quoted_price,
                    self.payload.liquidity_usd,
                    self.payload.estimated_slippage_bps,
                    self.payload.price_impact_bps,
                    self.payload.maximum_safe_size_usd,
                    self.payload.routing_provenance,
                )
            ):
                raise ValueError("Available execution evidence requires complete routing metrics")
        if self.status == EvidenceStatus.AVAILABLE and isinstance(self.payload, OnchainPayload):
            # A measured violation is a fact and belongs in available evidence, so
            # that "known dangerous" never has to masquerade as "unknown". What
            # cannot appear in available evidence is an unestablished domain.
            if "UNKNOWN" in self.payload.domains:
                raise ValueError(
                    "Available on-chain evidence cannot contain an unestablished domain"
                )
            if "FAIL" in self.payload.domains and not self.reason_codes:
                raise ValueError("A known on-chain violation requires a safe reason code")
        if (
            self.status == EvidenceStatus.AVAILABLE
            and isinstance(self.payload, SentimentPayload)
            and self.payload.assessment == "UNKNOWN"
        ):
            raise ValueError("Unknown sentiment cannot be marked available")
        return self

    def fingerprint(self) -> str:
        return sha256(self.model_dump_json(exclude={"idempotency_key"}).encode()).hexdigest()


class EvidenceEnvelope(Immutable):
    evidence_id: UUID
    trade_case_id: UUID
    producer_role: AgentRole
    evidence_type: EvidenceType
    schema_version: Literal[1] = 1
    provenance: EvidenceProvenance
    observed_at: AwareDatetime
    created_at: AwareDatetime
    recorded_at: AwareDatetime
    valid_until: AwareDatetime
    status: EvidenceStatus
    confidence: Confidence | None = None
    reason_codes: tuple[Code, ...] = ()
    payload: EvidencePayload
    correlation_id: UUID
    supersedes_id: UUID | None = None
    idempotency_key: Identifier
    submission_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def effective_status(self, now: datetime) -> EvidenceStatus:
        if self.observed_at > now:
            return EvidenceStatus.INVALID
        if now >= self.valid_until:
            return EvidenceStatus.STALE
        return self.status


class Blocker(Immutable):
    code: Code
    role: AgentRole | None = None
    evidence_type: EvidenceType | None = None
    evidence_id: UUID | None = None


class TradeCase(Immutable):
    id: UUID
    workflow_version: Literal["trade-case-v1"] = "trade-case-v1"
    market: MarketIdentity
    chain: str
    network: str
    status: TradeCaseStatus
    opened_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    originating_discovery_reference: UUID
    strategy_policy_id: Identifier | None = None
    revision: int = Field(ge=1)
    reason_code: Code
    blockers: tuple[Blocker, ...] = ()
    risk_input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    correlation_id: UUID
    open_idempotency_key: Identifier
    open_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if self.chain != self.market.chain or self.network != self.market.network:
            raise ValueError("TradeCase chain and network must match immutable market identity")
        return self


class SpecialistTask(Immutable):
    task_id: UUID
    trade_case_id: UUID
    role: AgentRole
    task_type: Identifier
    required: bool
    status: SpecialistTaskStatus
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    attempt: int = Field(ge=1)
    correlation_id: UUID
    reason_code: Code
    idempotency_key: Identifier


class RiskBinding(Immutable):
    """A SENTINEL decision bound to one TradeCase and one risk-input digest.

    Every field a future execution boundary must honour is typed and stored in
    its own column. ``decision_payload`` is retained for audit provenance only;
    enforcement never parses it.
    """

    binding_id: UUID
    trade_case_id: UUID
    risk_decision_id: UUID
    risk_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: RiskOutcome
    authorization: RiskAuthorization
    reason_codes: tuple[Code, ...] = Field(min_length=1)
    position_size_limit_usd: Nonnegative = Field(
        description="Absolute configured per-position USD limit, copied from the decision"
    )
    max_additional_notional_usd: Nonnegative = Field(
        description="Conservative additional BUY quote notional, copied from the decision"
    )
    max_slippage_bps: Nonnegative = Field(le=10000)
    evaluated_at: AwareDatetime
    expires_at: AwareDatetime
    correlation_id: UUID
    decision_payload: dict[str, object]

    @model_validator(mode="after")
    def authorization_matches_decision(self) -> Self:
        # The stored classification must still follow from the stored decision
        # values, so a divergent row can never authorize anything.
        if self.authorization != classify_risk_authorization(
            self.outcome,
            self.reason_codes,
            position_size_limit_usd=self.position_size_limit_usd,
            max_additional_notional_usd=self.max_additional_notional_usd,
        ):
            raise ValueError("Risk binding authorization does not follow from its risk decision")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("Risk binding validity must extend beyond evaluation time")
        return self


class TimelineEvent(Immutable):
    sequence: int
    event_id: UUID
    trade_case_id: UUID
    event_type: Code
    reason_code: Code
    recorded_at: AwareDatetime
    correlation_id: UUID
    payload: dict[str, object]
