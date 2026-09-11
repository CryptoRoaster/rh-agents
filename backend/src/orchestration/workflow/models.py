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


class SentimentPayload(AcceptancePayload):
    kind: Literal["sentiment"] = "sentiment"
    assessment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN"]


class TradeSetupPayload(AcceptancePayload):
    kind: Literal["trade_setup"] = "trade_setup"
    setup_id: UUID
    side: Side
    entry_price: Positive
    invalidation_price: Positive
    target_prices: tuple[Positive, ...] = Field(min_length=1)


class TriggerPayload(AcceptancePayload):
    kind: Literal["trigger"] = "trigger"
    setup_evidence_id: UUID
    observed_price: Positive
    trigger_code: Code


class LiquidityExecutionPayload(AcceptancePayload):
    kind: Literal["liquidity_execution"] = "liquidity_execution"
    setup_evidence_id: UUID
    trigger_evidence_id: UUID
    quoted_price: Positive | None = None
    liquidity_usd: Nonnegative | None = None
    estimated_slippage_bps: Nonnegative | None = Field(default=None, le=10000)
    price_impact_bps: Nonnegative | None = Field(default=None, le=10000)
    maximum_safe_size_usd: Nonnegative | None = None
    routing_provenance: Identifier | None = None


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
            if any(
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
