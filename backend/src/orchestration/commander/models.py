"""COMMANDER contracts: what a control plane may conclude, and what it may not.

COMMANDER answers one operational question about one TradeCase — *what safe
workflow action, if any, is currently eligible?* — and answers it from
authoritative state that other components already decided.

It is not a second workflow engine. The Phase 2A evaluator owns every status,
every requirement, every blocker and every freshness rule; this reads that
verdict and says what may happen next. Restating the transition matrix here
would create two authorities on one question, and the cheapest way for them to
disagree is for one of them to be updated.

It is not strategy. There is no technical rule, no sentiment reading, no holder
score, no entry selection, no route preference and no probability of anything.
Those belong to specialists that already produced structured findings, and a
coordinator that re-derived them would be competing with its own inputs.

It is not an authority. It cannot write a status, clear a blocker, overrule
SENTINEL, size a trade, reach a wallet, build a transaction or send one. What it
produces is a typed reading of eligibility, and a reading permits nothing.
"""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.core.models import AgentRole
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    SpecialistTaskStatus,
    TradeCaseStatus,
)
from src.risk.authorization import RiskAuthorization

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Statement = Annotated[str, Field(min_length=1, max_length=300)]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CommanderDisposition(StrEnum):
    """What the control plane concluded about one case, right now.

    Deliberately its own vocabulary. These are not case statuses — the evaluator
    owns those — and not risk outcomes. They describe what *coordination* should
    do, which is a different question from what the case *is*.
    """

    # Something the workflow requires has not been produced yet. Specialists are
    # the ones who produce it, and waiting is the correct coordination.
    AWAIT_SPECIALISTS = "AWAIT_SPECIALISTS"
    # The setup exists and the market has not reached it. PULSE owns that watch.
    AWAIT_TRIGGER = "AWAIT_TRIGGER"
    # The trigger fired and execution conditions are still being assessed.
    AWAIT_EXECUTION_EVIDENCE = "AWAIT_EXECUTION_EVIDENCE"
    # Every workflow prerequisite is satisfied and risk evaluation would be the
    # next step — but a prerequisite *this system* does not yet have is missing.
    # Distinct from every other disposition because the gap is architectural
    # rather than a matter of waiting for evidence.
    BLOCKED_ON_MISSING_CAPABILITY = "BLOCKED_ON_MISSING_CAPABILITY"
    # A current authorization already covers exactly this evidence set.
    RISK_CURRENT = "RISK_CURRENT"
    # The case cannot proceed: blocked, rejected, expired or cancelled. Not a
    # thing coordination fixes.
    HALTED = "HALTED"
    # A system-wide stop is in force.
    PAUSED = "PAUSED"


class CommanderReason(StrEnum):
    """Why the control plane concluded what it did.

    Typed because "why did COMMANDER do nothing?" must be answerable from a
    field rather than from prose. Nothing downstream parses a sentence.
    """

    REQUIRED_EVIDENCE_PENDING = "REQUIRED_EVIDENCE_PENDING"
    SAFETY_EVIDENCE_BLOCKED = "SAFETY_EVIDENCE_BLOCKED"
    WAITING_FOR_TRIGGER = "WAITING_FOR_TRIGGER"
    WAITING_FOR_EXECUTION_EVIDENCE = "WAITING_FOR_EXECUTION_EVIDENCE"
    # The honest stop. Every workflow prerequisite is met and SENTINEL cannot be
    # asked, because asking requires a requested trade size and nothing in this
    # system produces one. Reported rather than papered over with a number.
    AUTONOMOUS_SIZING_INPUT_MISSING = "AUTONOMOUS_SIZING_INPUT_MISSING"
    RISK_AUTHORIZATION_CURRENT = "RISK_AUTHORIZATION_CURRENT"
    RISK_REJECTED = "RISK_REJECTED"
    TRADE_CASE_TERMINAL = "TRADE_CASE_TERMINAL"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"


class EvidenceState(Immutable):
    """One current envelope, reduced to what coordination may look at.

    Identity, fingerprint and the two axes the workflow itself uses. No payload
    content: a control plane that read findings would be interpreting evidence,
    which is the specialists' job and not a coordinator's.
    """

    role: AgentRole
    evidence_type: EvidenceType
    evidence_id: UUID
    submission_fingerprint: Digest
    status: EvidenceStatus
    acceptance: EvidenceAcceptance
    required: bool = Field(strict=True)
    safety_critical: bool = Field(strict=True)


class TaskState(Immutable):
    """One specialist task slot, by role and status."""

    role: AgentRole
    task_type: Identifier
    status: SpecialistTaskStatus
    attempt: int = Field(strict=True, ge=1)
    required: bool = Field(strict=True)


class RiskState(Immutable):
    """The current authorization, and the evidence set it was granted against.

    ``matches_current_inputs`` is the only question coordination asks of it. An
    authorization granted against a different evidence set is not a weaker
    authorization — it is one about a case that no longer exists.
    """

    binding_id: UUID
    risk_decision_id: UUID
    authorization: RiskAuthorization
    risk_input_digest: Digest
    matches_current_inputs: bool = Field(strict=True)
    expires_at: AwareDatetime


class AdvisorySynthesis(Immutable):
    """FUSE's reading, carried for observability and authoritative over nothing.

    Present only when it is current *and* describes the case's current evidence.
    A synthesis of a superseded evidence set is not a weaker opinion; it is an
    opinion about a different case, so it does not travel at all.

    Nothing in the decision reads this. It exists so an operator can see what
    the advisory layer thought, and it is deliberately impossible to gate on:
    the field carries no authority and the decision function never consults it.
    """

    evidence_id: UUID
    disposition: Identifier
    hard_blocker_count: int = Field(strict=True, ge=0)
    unresolved_gap_count: int = Field(strict=True, ge=0)
    describes_current_inputs: bool = Field(strict=True)


class SystemControls(Immutable):
    """The system-wide stops, as authoritative facts rather than opinions."""

    # The configured deterministic kill switch SENTINEL itself honours.
    kill_switch: bool = Field(strict=True)
    # A durable pause recorded after a SENTINEL PAUSE_SYSTEM verdict.
    account_paused: bool = Field(strict=True)
    # PAPER or OBSERVE. Live is not representable.
    trading_mode: Identifier

    @property
    def halted(self) -> bool:
        return self.kill_switch or self.account_paused


class CommanderContext(Immutable):
    """Everything COMMANDER is given about one case, and nothing else.

    No session, no repository, no provider client, no reasoning provider, no
    market reader, no ability to ask a different question. The server decided
    what is authoritative before this was built, and the worker cannot re-open
    that decision — which is what makes "COMMANDER cannot choose its inputs" a
    property of the type rather than a rule somebody has to remember.
    """

    trade_case_id: UUID
    task_id: UUID
    workflow_version: Identifier
    policy_version: Identifier
    status: TradeCaseStatus
    revision: int = Field(strict=True, ge=1)
    reason_code: Identifier
    blocker_codes: tuple[Identifier, ...] = Field(default=(), max_length=24)
    evidence: tuple[EvidenceState, ...] = Field(default=(), max_length=12)
    tasks: tuple[TaskState, ...] = Field(default=(), max_length=16)
    risk: RiskState | None = None
    advisory: AdvisorySynthesis | None = None
    controls: SystemControls
    current_risk_input_digest: Digest | None = None
    observed_at: AwareDatetime
    context_digest: Digest

    @model_validator(mode="after")
    def one_per_type(self) -> Self:
        seen = [item.evidence_type for item in self.evidence]
        if len(set(seen)) != len(seen):
            raise ValueError("One current envelope per evidence type")
        return self


class CommanderDecision(Immutable):
    """The deterministic conclusion, bound to the state it was reached from.

    ``context_digest`` is not decoration. A decision is only valid for the state
    that produced it, and carrying the digest is what lets the server refuse an
    action derived from a case that has since moved on.
    """

    policy_version: Identifier
    trade_case_id: UUID
    disposition: CommanderDisposition
    reason_code: CommanderReason
    statement: Statement
    context_digest: Digest
    decided_at: AwareDatetime

    @property
    def is_progression(self) -> bool:
        """Whether this decision would advance anything. Today: never.

        The only progression a control plane could perform is asking SENTINEL,
        and that requires a requested trade size no component produces. Stated
        as a property so the day a sizing source exists, the thing that has to
        change is visible.
        """
        return False
