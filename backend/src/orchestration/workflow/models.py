"""Immutable workflow contracts and typed evidence payloads."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    model_serializer,
    model_validator,
)

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
    # A paper entry was filled and booked against this case. Terminal: the case
    # asked one question, got one authorization and spent it.
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


# A rejection is final for this TradeCase; materially new conditions require a
# new TradeCase rather than reopening a rejected one. RISK_APPROVED and
# RISK_LIMITED are deliberately absent: both are authorizations that the
# evaluator must be able to revoke when their safety inputs change.
TERMINAL_CASE_STATUSES = frozenset(
    {
        TradeCaseStatus.RISK_REJECTED,
        TradeCaseStatus.EXECUTED,
        TradeCaseStatus.EXPIRED,
        TradeCaseStatus.CANCELLED,
    }
)

# Terminal statuses that also bar the market from opening a further case.
#
# A rejection is a verdict about the market, and re-observing it is not new
# information about risk. An execution leaves a *position*, and this system has
# no contract for adding to one, exiting one, or deciding when a further entry
# would be a different trade rather than the same one repeated — so a further
# case is refused rather than opened on an invented rule. `EXPIRED` and
# `CANCELLED` are deliberately absent: both end a case without deciding anything
# about the market and without leaving anything behind.
MARKET_BARRING_CASE_STATUSES = frozenset(
    {
        TradeCaseStatus.RISK_REJECTED,
        TradeCaseStatus.EXECUTED,
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
    SYNTHESIS = "SYNTHESIS_EVIDENCE"


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


# The optional holder block, and the rule for payloads written before it existed.
#
# Evidence is append-only and its fingerprints are stored, so adding a field to
# a payload is a change to bytes already in the database: an absent key is not
# the same as a key holding `null`, and emitting one for a row that never had it
# breaks replay while parsing perfectly. The shape as read is therefore
# remembered and reproduced — the same mechanism `ExecutionAssessmentDetail` and
# `SynthesisDetail` use for their legacy timestamp, applied once more rather
# than generalised into a serialisation layer nobody could reason about.
HOLDER_FACTS_KEY = "holders"


def _holder_facts_present(data: Any) -> bool:
    """Whether a payload arrived carrying the holder block at all."""
    if isinstance(data, Mapping):
        return HOLDER_FACTS_KEY in data
    return bool(getattr(data, "_holder_facts_key_present", False))


class HolderDistributionFacts(Immutable):
    """The measured holder distribution, and everything needed to trust it.

    A verdict is not a metric. ``holder_integrity == "PASS"`` says the holder
    domain met its data-quality prerequisites; it says nothing about how
    concentrated the token is, and a reader that needed a number could not get
    one from it. These are the numbers, recorded beside the verdict rather than
    in place of it.

    The measure is named rather than implied. ``top_ten_fraction`` is the sum of
    the ten largest balances over **on-chain total supply**, unadjusted — the
    same figure ATLAS's own concentration policy judges. The burn-adjusted
    variant divides by circulating supply instead and travels separately,
    because a large position must never be hidden by an adjustment and the two
    are not interchangeable inputs to a threshold.

    ``provider_excluded_addresses`` names rows the provider removed before we
    ever saw them. They change no figure here — a row that never arrived cannot
    be added back — but they make the top-ten numerator a lower bound, so a
    reader applying a limit must treat the metric as unproven rather than
    passing. That judgement belongs to the reader; this records the fact.
    """

    # The measure, stated so no later reader has to infer it from a field name.
    measurement: Literal["TOP_TEN_OVER_TOTAL_SUPPLY"] = "TOP_TEN_OVER_TOTAL_SUPPLY"
    source: Identifier
    # The source's own account of when this holder state was true, never when we
    # fetched it. What that instant actually refers to is `observation_basis`.
    observed_at: AwareDatetime
    observation_basis: Code
    completeness: Code
    snapshot_block: int | None = Field(default=None, strict=True, ge=0)
    # Chain block minus holder snapshot block. Signed: the pinned contract block
    # trails the head by the confirmation lag while an indexer tracks the head.
    holder_block_delta: int | None = Field(default=None, strict=True)
    # The provider's own count of holders. Not derived from the rows we received
    # and not verifiable from them, which is why its basis is recorded beside it.
    holder_count: int | None = Field(default=None, strict=True, ge=0)
    holder_count_basis: Code = "PROVIDER_REPORTED"
    # The denominator actually divided by, as text: a uint256 supply exceeds what
    # a JSON number can carry back unharmed in every reader.
    total_supply_raw: Annotated[str, Field(pattern=r"^[0-9]{1,78}$")] | None = None
    top_one_fraction: Share | None = None
    top_five_fraction: Share | None = None
    top_ten_fraction: Share | None = None
    # Top ten over *circulating* supply with proven burn holdings removed. A
    # different measure against a different denominator, never a substitute.
    top_ten_fraction_excluding_burn: Share | None = None
    burned_fraction: Share | None = None
    provider_excluded_addresses: tuple[Identifier, ...] = Field(default=(), max_length=8)


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
    # Absent on evidence written before the holder metrics were carried, and
    # omitted rather than serialised as `null` for exactly those rows.
    holders: "HolderDistributionFacts | None" = None

    _holder_facts_key_present: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def _remember_holder_shape(cls, data: Any, handler: Any) -> Any:
        present = _holder_facts_present(data)
        model = handler(data)
        object.__setattr__(model, "_holder_facts_key_present", present)
        return model

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: Any) -> dict[str, Any]:
        emitted: dict[str, Any] = handler(self)
        if self._holder_facts_key_present:
            return emitted
        return {key: value for key, value in emitted.items() if key != HOLDER_FACTS_KEY}


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


# The four shapes this system has written for the same optional timestamp, and
# the rule for reproducing each. Evidence is append-only and fingerprints are
# stored, so the serialised form is a contract with rows already in the
# database — and these four differ in ways that a single nullable field cannot
# express, because *absence* and *an explicitly stored null* are different bytes
# that mean the same thing.
#
#   9515b49   "evaluated_at": "<value>"              before execution_digest / last
#   094cad3   (no key at all)
#   c760266   "legacy_evaluated_at": null            last
#   c760266   "legacy_evaluated_at": "<value>"       last  (a rewritten 9515b49 row)
#   current   (no key at all)                        for anything newly computed
#
# So the key *as read* is remembered and reproduced: its name decides both what
# is emitted and where. The historical name keeps the position the field was
# originally declared at; the interim name always came last, in both models.
LEGACY_TIMESTAMP_KEYS = ("evaluated_at", "legacy_evaluated_at")
INTERIM_TIMESTAMP_KEY = "legacy_evaluated_at"
HISTORICAL_TIMESTAMP_KEY = "evaluated_at"


def _observed_legacy_key(data: Any) -> str | None:
    """Which spelling of the optional timestamp a payload arrived with, if any."""
    if isinstance(data, Mapping):
        for key in LEGACY_TIMESTAMP_KEYS:
            if key in data:
                return key
        return None
    # Already a model instance: carry whatever it recorded.
    return getattr(data, "_legacy_timestamp_key", None)


def _restore_legacy_shape(data: dict[str, Any], observed: str | None) -> dict[str, Any]:
    """Re-emit the optional timestamp exactly as it was read, or not at all.

    Order matters as much as the name: the key's position is part of the bytes
    the submission fingerprint was taken over, so it is rebuilt in iteration
    order rather than popped and re-appended.
    """
    value = data.get(INTERIM_TIMESTAMP_KEY)
    without = {key: item for key, item in data.items() if key != INTERIM_TIMESTAMP_KEY}
    if observed is None:
        # Never stored, or newly computed. Newly computed results deliberately
        # carry no run metadata at all.
        return without
    if observed == INTERIM_TIMESTAMP_KEY:
        # The interim shape always came last, including its explicit null.
        return {**without, INTERIM_TIMESTAMP_KEY: value}
    # The original shape, at the position it was declared in.
    return {
        (HISTORICAL_TIMESTAMP_KEY if key == INTERIM_TIMESTAMP_KEY else key): item
        for key, item in data.items()
    }


class QuotedLadderPoint(Immutable):
    """One tested size and what the market said about it.

    Kept whether it passed or failed. "Why did ANCHOR say five hundred?" is
    answered as much by the size that was refused as by the one that was not, and
    a digest alone would prove the ladder unchanged without saying what it held.
    """

    # The USD size actually tested, and the payment-asset amount actually sent
    # for it. Both are kept because the second is what the provider was asked
    # and the first is what it means, and re-deriving either later would need a
    # price that has since moved.
    notional_usd: Positive
    amount_in_tokens: Positive
    accepted: bool = Field(strict=True)
    amount_out: int | None = Field(default=None, strict=True, ge=0)
    # The provider's own USD valuation of the input, when it published one.
    # Recorded so the cross-check that was performed is auditable.
    provider_amount_in_usd: Nonnegative | None = None
    # USD per unit bought, so it is comparable with the reference price.
    effective_price_usd: Positive | None = None
    execution_deviation_bps: Decimal | None = Field(default=None, allow_inf_nan=False)
    provider_price_impact_bps: Nonnegative | None = None
    route_hops: int | None = Field(default=None, strict=True, ge=0)
    venues: tuple[Identifier, ...] = Field(default=(), max_length=32)
    quoted_at: AwareDatetime | None = None
    source_block_number: int | None = Field(default=None, strict=True, ge=0)
    rejection: Code | None = None


class ExecutionAssessmentDetail(Immutable):
    """What the market was shown to support, and how that was established.

    Deliberately carries no evaluation timestamp, for the same reason the
    synthesis detail carries none: the submission fingerprint is computed over
    this payload, so a field moving with the wall clock made two assessments of
    identical quotes produce one idempotency key with two fingerprints — which
    the runtime correctly reports as a conflict rather than the replay it is.
    When the work happened is already on the envelope as `created_at` and
    `recorded_at`, and each ladder point carries its own `quoted_at`.

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
    # The largest size tested and accepted, in USD. The name states both facts
    # it depends on: the unit, and that it was *tested* rather than measured.
    # Nothing here proves the market's true maximum, and nothing proves anything
    # about the untested sizes between this and the rejected one below.
    largest_tested_acceptable_notional_usd: Positive | None = None
    first_tested_rejected_notional_usd: Positive | None = None
    reference_price: Positive
    reference_price_basis: Code
    reference_observed_at: AwareDatetime
    # What the payment asset was worth when this was assessed, and where that
    # came from. The USD figures above are only as good as this one, so it
    # travels with them rather than being left implicit.
    quote_asset_usd_price: Positive
    quote_asset_usd_observed_at: AwareDatetime
    quote_asset_usd_provider: Identifier
    effective_price_usd_at_capacity: Positive | None = None
    execution_deviation_bps_at_capacity: Decimal | None = Field(default=None, allow_inf_nan=False)
    payment_asset_id: Identifier
    target_asset_id: Identifier
    quote_provider: Identifier
    quote_requests: int = Field(ge=0)
    ladder: tuple[QuotedLadderPoint, ...] = Field(min_length=1, max_length=12)
    # Legacy only, and never written.
    #
    # Evidence is append-only and fingerprints are stored, so the serialised
    # form of a payload is a contract with rows already in the database. Three
    # generations exist: one that wrote `evaluated_at`, one that wrote no such
    # key at all, and this one. All three must read *and re-serialise* to
    # exactly the bytes they were stored as, because the submission fingerprint
    # is taken over the whole JSON and replay compares against the fingerprint
    # recorded at the time.
    #
    # So the field is declared at the position the original occupied, emitted
    # under the original name, and omitted entirely when absent — which is what
    # `_without_absent_legacy_timestamp` below does. Renaming it or adding a
    # `null` would each change the bytes and break replay while parsing
    # perfectly, which is how the previous attempt passed its own tests.
    #
    # A blanket `extra="ignore"` would have achieved the parsing and lost the
    # contract: every other unknown field would vanish with it.
    legacy_evaluated_at: AwareDatetime | None = Field(
        default=None,
        validation_alias=AliasChoices("evaluated_at", "legacy_evaluated_at"),
    )
    execution_digest: Digest

    _legacy_timestamp_key: str | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _remember_legacy_shape(cls, data: Any, handler: Any) -> Any:
        observed = _observed_legacy_key(data)
        model = handler(data)
        object.__setattr__(model, "_legacy_timestamp_key", observed)
        return model

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: Any) -> dict[str, Any]:
        return _restore_legacy_shape(handler(self), self._legacy_timestamp_key)


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
    # Legacy scalars from Phase 2A, retained so older evidence still parses.
    #
    # An ANCHOR worker leaves both empty, and that is a decision rather than an
    # omission. `estimated_slippage_bps` means a realisable fill cost here — the
    # sibling field on a market snapshot is what the paper executor uses to move
    # a fill price and then reports as `realized_slippage_bps` — and an
    # execution deviation is not that. `price_impact_bps` means the provider's
    # own figure, and a provider that publishes none leaves it absent rather
    # than lending its name to a number computed elsewhere.
    #
    # Both are therefore `None` on anything ANCHOR produces. The separated,
    # correctly named figures live in `execution`.
    estimated_slippage_bps: Nonnegative | None = Field(default=None, le=10000)
    price_impact_bps: Nonnegative | None = Field(default=None, le=10000)
    # The legacy capacity scalar. It never over-claims — it is a size actually
    # tested and accepted, in USD — but it cannot express that the true capacity
    # may be higher, so anything reasoning about capacity reads `execution`.
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
        if detail.largest_tested_acceptable_notional_usd is None:
            return EvidenceAcceptance.BLOCKED
        return EvidenceAcceptance.ACCEPTED


class SynthesisSource(Immutable):
    """One envelope a synthesis was built from, by identity and fingerprint.

    The fingerprint is what makes the reference checkable: it says not merely
    "ATLAS evidence" but "that exact ATLAS finding", so a synthesis can never be
    read as describing a later one.
    """

    role: AgentRole
    evidence_type: EvidenceType
    evidence_id: UUID
    submission_fingerprint: Digest
    status: Code
    acceptance: Code
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    required: bool = Field(strict=True)
    safety_critical: bool = Field(strict=True)


class SynthesisFinding(Immutable):
    """One blocker, gap or factor, attributed to the evidence that produced it."""

    code: Code
    role: AgentRole
    evidence_type: EvidenceType
    origin: Code
    statement: Annotated[str, Field(min_length=1, max_length=300)]
    evidence_id: UUID | None = None
    safety_critical: bool | None = Field(default=None, strict=True)


class SynthesisDetail(Immutable):
    """The structured reading of a case's admissible evidence.

    Deliberately carries no evaluation timestamp. A synthesis is a function of
    the evidence it read, and the submission fingerprint is computed over this
    payload — so a field that moved with the wall clock made two recomputations
    of identical evidence produce the same idempotency key with different
    fingerprints, which the runtime correctly reports as a conflict. When the
    work *did* happen is already recorded authoritatively on the envelope as
    `created_at` and `recorded_at`; repeating it here bought nothing and cost
    replay.

    There is no score here, and its absence is deliberate. A composite number
    over four incommensurable specialist findings would be false precision — it
    would look calibrated, invite comparison between cases, and encode weights
    nobody chose. What is recorded instead is what was found, attributed to who
    found it, in four lists that a reader can check.
    """

    policy_version: Identifier
    disposition: Code
    hard_blockers: tuple[SynthesisFinding, ...] = Field(default=(), max_length=24)
    unresolved_gaps: tuple[SynthesisFinding, ...] = Field(default=(), max_length=24)
    support_factors: tuple[SynthesisFinding, ...] = Field(default=(), max_length=16)
    caution_factors: tuple[SynthesisFinding, ...] = Field(default=(), max_length=16)
    sources: tuple[SynthesisSource, ...] = Field(min_length=1, max_length=12)
    input_digest: Digest
    synthesis_fingerprint: Digest
    # Legacy only, and never written.
    #
    # Evidence is append-only and fingerprints are stored, so the serialised
    # form of a payload is a contract with rows already in the database. Three
    # generations exist: one that wrote `evaluated_at`, one that wrote no such
    # key at all, and this one. All three must read *and re-serialise* to
    # exactly the bytes they were stored as, because the submission fingerprint
    # is taken over the whole JSON and replay compares against the fingerprint
    # recorded at the time.
    #
    # So the field is declared at the position the original occupied, emitted
    # under the original name, and omitted entirely when absent — which is what
    # `_without_absent_legacy_timestamp` below does. Renaming it or adding a
    # `null` would each change the bytes and break replay while parsing
    # perfectly, which is how the previous attempt passed its own tests.
    #
    # A blanket `extra="ignore"` would have achieved the parsing and lost the
    # contract: every other unknown field would vanish with it.
    legacy_evaluated_at: AwareDatetime | None = Field(
        default=None,
        validation_alias=AliasChoices("evaluated_at", "legacy_evaluated_at"),
    )

    _legacy_timestamp_key: str | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _remember_legacy_shape(cls, data: Any, handler: Any) -> Any:
        observed = _observed_legacy_key(data)
        model = handler(data)
        object.__setattr__(model, "_legacy_timestamp_key", observed)
        return model

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: Any) -> dict[str, Any]:
        return _restore_legacy_shape(handler(self), self._legacy_timestamp_key)


class SynthesisPayload(AcceptancePayload):
    """FUSE's reading of the evidence. Analytical only, and authoritative over nothing.

    It does not approve a trade, size one, or clear anything another specialist
    recorded. SENTINEL continues to read the canonical safety evidence directly;
    this summarises that evidence and changes nothing about what it says.

    Deliberately carries no position size, notional, slippage, route or risk
    outcome — not merely unset, but absent from the schema, so a synthesis that
    tried to grow trade authority would fail to validate rather than succeed
    quietly.
    """

    kind: Literal["synthesis"] = "synthesis"
    disposition: Code
    source_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=12)
    synthesis: "SynthesisDetail | None" = None

    def acceptance(self) -> EvidenceAcceptance:
        """Honest about its own reading, and gating nothing.

        FUSE evidence is optional and not safety-critical, so this never moves a
        case on its own — the evaluator reads the canonical sources for that.
        What it does is keep the record truthful: a synthesis that found a
        blocker is not an accepted one, however convenient that would be.
        """
        detail = self.synthesis
        if detail is None:
            return EvidenceAcceptance.ACCEPTED
        if detail.hard_blockers:
            return EvidenceAcceptance.BLOCKED
        if detail.unresolved_gaps:
            return EvidenceAcceptance.INSUFFICIENT
        return EvidenceAcceptance.ACCEPTED


EvidencePayload = Annotated[
    DiscoveryPayload
    | OnchainPayload
    | SentimentPayload
    | TradeSetupPayload
    | TriggerPayload
    | LiquidityExecutionPayload
    | SynthesisPayload,
    Field(discriminator="kind"),
]


PAYLOAD_KIND = {
    EvidenceType.DISCOVERY: "discovery",
    EvidenceType.ONCHAIN: "onchain",
    EvidenceType.SENTIMENT: "sentiment",
    EvidenceType.TRADE_SETUP: "trade_setup",
    EvidenceType.TRIGGER: "trigger",
    EvidenceType.LIQUIDITY_EXECUTION: "liquidity_execution",
    EvidenceType.SYNTHESIS: "synthesis",
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


class SourceRefreshOutcome(StrEnum):
    """What came of asking for one risk source to be observed again."""

    # The observing task is armed. Nothing has been observed yet: a new reading
    # exists only once its handler produces one and the evidence is recorded.
    ORDERED = "ORDERED"
    # Nothing in this workflow observes that source, so there is no task to arm.
    SOURCE_NOT_REFRESHABLE = "SOURCE_NOT_REFRESHABLE"
    # The case is not at the point where a refresh means anything: it is not
    # waiting on a risk request, not blocked on a source that has merely aged
    # out, or already finished.
    CASE_NOT_READY = "CASE_NOT_READY"
    # The case is blocked, and not on this source being old. A negative
    # assessment, an unavailable one, or evidence that established nothing are
    # all real findings, and observing again is not the answer to any of them.
    SOURCE_NOT_STALE = "SOURCE_NOT_STALE"
    # Somebody already ordered this and it has not produced a reading yet. A
    # second order would be a duplicate of work already outstanding.
    ALREADY_ORDERED = "ALREADY_ORDERED"
    # The task exists but can no longer be claimed: its attempts are spent, it
    # expired, or it ended terminally. Re-arming it would queue work that the
    # runtime would refuse to hand out.
    OBSERVER_UNAVAILABLE = "OBSERVER_UNAVAILABLE"


class SourceRefreshOrder(Immutable):
    """The answer to one refresh request, always typed and never a silent no-op."""

    outcome: SourceRefreshOutcome
    source: Identifier
    trade_case_id: UUID
    case_revision: int = Field(ge=1)
    role: AgentRole | None = None
    task_type: Identifier | None = None
    task_id: UUID | None = None
    # The attempt the observing task now stands at. Present only when one was
    # actually armed, so a reader cannot mistake a refusal for a count.
    attempt: int | None = Field(default=None, ge=1)


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
