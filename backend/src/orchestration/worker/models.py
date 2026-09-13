"""Typed contracts for the capability-restricted worker runtime.

Workers are untrusted from an authorization perspective. Nothing in this module
grants authority; it only describes the identities, leases and results the
deterministic runtime services validate before accepting any effect.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.core.models import AgentRole
from src.orchestration.workflow.models import EvidenceSubmission, EvidenceType

Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Version = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^\S(?:.*\S)?$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class WorkerErrorCode(StrEnum):
    WORKER_NOT_FOUND = "WORKER_NOT_FOUND"
    WORKER_NOT_ACTIVE = "WORKER_NOT_ACTIVE"
    WORKER_IDENTITY_CONFLICT = "WORKER_IDENTITY_CONFLICT"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_CLAIMABLE = "TASK_NOT_CLAIMABLE"
    TASK_SUPERSEDED = "TASK_SUPERSEDED"
    TRADE_CASE_NOT_WORKABLE = "TRADE_CASE_NOT_WORKABLE"
    TRADE_CASE_MISMATCH = "TRADE_CASE_MISMATCH"
    LEASE_NOT_FOUND = "LEASE_NOT_FOUND"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_OWNER_MISMATCH = "LEASE_OWNER_MISMATCH"
    LEASE_RENEWAL_EXHAUSTED = "LEASE_RENEWAL_EXHAUSTED"
    ROLE_NOT_AUTHORIZED = "ROLE_NOT_AUTHORIZED"
    EVIDENCE_TYPE_NOT_AUTHORIZED = "EVIDENCE_TYPE_NOT_AUTHORIZED"
    RESULT_CONFLICT = "RESULT_CONFLICT"
    WAIT_NOT_PERMITTED = "WAIT_NOT_PERMITTED"
    WAIT_REASON_NOT_PERMITTED = "WAIT_REASON_NOT_PERMITTED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"


class WorkerFailure(Exception):
    """Typed runtime refusal. Never carries provider detail, payloads or secrets."""

    def __init__(self, code: WorkerErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class WorkerInstanceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class TaskAttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    # An attempt that did its work correctly and found the world not yet ready.
    # A monitor whose condition has not become true has not failed at anything,
    # and recording it as a failure would make ordinary operation look like an
    # incident and spend a retry budget meant for things going wrong.
    WAITING = "WAITING"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL_ATTEMPT_OUTCOMES = frozenset(TaskAttemptOutcome)

# Outcomes that record work done rather than work gone wrong. Neither may carry
# a failure category, and neither is an error for an operator to look at.
NON_FAILURE_OUTCOMES = frozenset({TaskAttemptOutcome.SUCCEEDED, TaskAttemptOutcome.WAITING})

# What counts against the retry budget. Deliberately not every non-success:
# waiting is work done, and a superseded or cancelled attempt was not the
# worker's doing either.
FAILURE_OUTCOMES = frozenset(
    {
        TaskAttemptOutcome.FAILED_RETRYABLE,
        TaskAttemptOutcome.FAILED_PERMANENT,
        TaskAttemptOutcome.LEASE_EXPIRED,
    }
)


class WorkerFailureCategory(StrEnum):
    """How a worker failure must be treated, never left to the worker to decide."""

    TRANSIENT = "TRANSIENT"
    INVALID_RESULT = "INVALID_RESULT"
    INTERNAL = "INTERNAL"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    TASK_INVALIDATED = "TASK_INVALIDATED"


class WorkerRegistration(Immutable):
    """Logical registration identity, separate from ephemeral host/process metadata."""

    registration_key: Identifier
    role: AgentRole
    runtime_version: Version


class WorkerInstance(Immutable):
    worker_instance_id: UUID
    role: AgentRole
    runtime_version: Version
    status: WorkerInstanceStatus
    started_at: AwareDatetime
    last_seen_at: AwareDatetime
    registration_key: Identifier


class TaskLease(Immutable):
    """Proof of exclusive, time-bounded authority over exactly one task attempt."""

    lease_id: UUID
    task_id: UUID
    trade_case_id: UUID
    role: AgentRole
    task_type: Identifier
    worker_instance_id: UUID
    attempt_number: int = Field(ge=1)
    lease_started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    renewals: int = Field(ge=0)
    correlation_id: UUID

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.lease_expires_at <= self.lease_started_at:
            raise ValueError("Lease must expire after it starts")
        return self

    def is_active_at(self, now: datetime) -> bool:
        return now < self.lease_expires_at


class TaskAttempt(Immutable):
    attempt_id: UUID
    task_id: UUID
    trade_case_id: UUID
    role: AgentRole
    worker_instance_id: UUID
    lease_id: UUID
    attempt_number: int = Field(ge=1)
    started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: TaskAttemptOutcome | None = None
    reason_code: Code
    failure_category: WorkerFailureCategory | None = None
    runtime_version: Version
    correlation_id: UUID

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.finished_at is None) != (self.outcome is None):
            raise ValueError("An attempt is finished exactly when it carries an outcome")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("An attempt cannot finish before it starts")
        if self.failure_category is not None and self.outcome in NON_FAILURE_OUTCOMES:
            raise ValueError("A succeeded or waiting attempt cannot carry a failure category")
        return self


class EvidenceTaskResult(Immutable):
    """The only authoritative worker output shape in Phase 2B.

    A worker submits typed evidence, never a free-form recommendation and never a
    direct instruction to buy or sell. The deterministic workflow alone decides
    what the evidence means.
    """

    kind: Literal["evidence"] = "evidence"
    submission: EvidenceSubmission
    result_key: Identifier

    @property
    def evidence_type(self) -> EvidenceType:
        return self.submission.evidence_type


class TaskFailureReport(Immutable):
    kind: Literal["failure"] = "failure"
    category: WorkerFailureCategory
    reason_code: Code


class TaskWaitReport(Immutable):
    """The work was done and the answer is "not yet".

    A monitor needs a third answer. Reporting a wait as success would complete a
    task whose job is not finished; reporting it as failure would spend the retry
    budget on ordinary operation and fill an audit trail with incidents that
    never happened.

    **A worker reports a fact; it does not choose a schedule.** Cadence is
    policy, and a worker that could name its own would be able to postpone a task
    indefinitely or hammer a provider at will. The runtime derives the next
    eligibility time from the task's own server-side policy and accepts only the
    reasons that policy allows.

    ``not_after`` is the one exception, and it can only ever *shorten* the wait:
    a monitor knows when the thing it watches stops being watchable, and
    scheduling a check past that point would queue work that cannot succeed. It
    cannot extend anything, which is the direction that would matter.
    """

    kind: Literal["wait"] = "wait"
    reason_code: Code
    not_after: AwareDatetime | None = None


TaskOutcomeReport = Annotated[
    EvidenceTaskResult | TaskFailureReport | TaskWaitReport, Field(discriminator="kind")
]


class TaskDisposition(Immutable):
    """What the runtime decided about a task after an attempt finished."""

    task_id: UUID
    trade_case_id: UUID
    attempt_number: int = Field(ge=1)
    outcome: TaskAttemptOutcome
    reason_code: Code
    retry_scheduled: bool
    next_eligible_at: AwareDatetime | None = None
    replayed: bool = False
