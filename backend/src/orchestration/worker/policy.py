"""One versioned location for worker authorization and retry rules.

The role-to-evidence matrix is *derived* from the workflow policy rather than
restated, so a capability can never silently drift away from the workflow
requirement it is supposed to serve.
"""

from dataclasses import dataclass
from datetime import timedelta

from src.core.models import AgentRole
from src.orchestration.worker.models import WorkerFailureCategory
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.policy import TRADE_CASE_V1, WorkflowPolicy

# Roles that are deterministic services rather than reasoning workers never appear
# in AgentRole at all, so SENTINEL, LEDGER and EXECUTOR are structurally incapable
# of being claimed through this runtime.


def authorized_evidence_type(
    role: AgentRole, policy: WorkflowPolicy = TRADE_CASE_V1
) -> EvidenceType | None:
    """The single evidence type a role may ever submit, or None if it may not."""
    for requirement in policy.requirements:
        if requirement.role == role:
            return requirement.evidence_type
    return None


def evidence_roles(policy: WorkflowPolicy = TRADE_CASE_V1) -> frozenset[AgentRole]:
    return frozenset(requirement.role for requirement in policy.requirements)


def role_evidence_matrix(
    policy: WorkflowPolicy = TRADE_CASE_V1,
) -> dict[AgentRole, EvidenceType]:
    return {requirement.role: requirement.evidence_type for requirement in policy.requirements}


def authorized_task_type(role: AgentRole, policy: WorkflowPolicy = TRADE_CASE_V1) -> str | None:
    for requirement in policy.requirements:
        if requirement.role == role:
            return requirement.task_type
    return None


RETRYABLE_CATEGORIES = frozenset(
    {
        WorkerFailureCategory.TRANSIENT,
        WorkerFailureCategory.INVALID_RESULT,
        WorkerFailureCategory.INTERNAL,
    }
)

PERMANENT_CATEGORIES = frozenset({WorkerFailureCategory.CAPABILITY_DENIED})

# Task invalidation is not a worker failure to retry; the work itself no longer
# applies, so the attempt is superseded instead.
SUPERSEDING_CATEGORIES = frozenset({WorkerFailureCategory.TASK_INVALIDATED})


@dataclass(frozen=True)
class WorkerRuntimePolicy:
    version: str
    lease_duration: timedelta
    max_attempts: int
    max_lease_renewals: int
    retry_initial_delay: timedelta
    retry_max_delay: timedelta
    claim_batch: int
    # Bounds on a monitor's own proposed recheck interval. A worker proposes;
    # the runtime decides. Without a floor a worker could poll a provider as
    # fast as it liked, and without a ceiling a task could effectively stop
    # watching without ever saying so.
    min_wait_interval: timedelta
    max_wait_interval: timedelta

    def __post_init__(self) -> None:
        if self.lease_duration <= timedelta(0):
            raise ValueError("Lease duration must be positive")
        if self.max_attempts < 1:
            raise ValueError("At least one attempt must be permitted")
        if self.max_lease_renewals < 0:
            raise ValueError("Renewal budget cannot be negative")
        if self.retry_initial_delay < timedelta(0) or self.retry_max_delay < timedelta(0):
            raise ValueError("Retry delays cannot be negative")
        if self.retry_max_delay < self.retry_initial_delay:
            raise ValueError("Maximum retry delay cannot be below the initial delay")
        if not 1 <= self.claim_batch <= 50:
            raise ValueError("Claim batch must be between 1 and 50")
        if self.min_wait_interval <= timedelta(0):
            raise ValueError("A recheck interval must be positive")
        if self.max_wait_interval < self.min_wait_interval:
            raise ValueError("The recheck ceiling cannot sit below its floor")

    def wait_interval(self, proposed: timedelta) -> timedelta:
        """Clamp a worker's proposed recheck into the runtime's own bounds."""
        if proposed < self.min_wait_interval:
            return self.min_wait_interval
        return min(proposed, self.max_wait_interval)

    def is_retryable(self, category: WorkerFailureCategory) -> bool:
        return category in RETRYABLE_CATEGORIES

    def retry_delay(self, attempt_number: int) -> timedelta:
        """Deterministic bounded exponential backoff; no jitter, no wall-clock sleep."""
        if attempt_number < 1:
            raise ValueError("Attempt numbers start at one")
        # Cap the shift before computing it so a large attempt count cannot build a
        # huge intermediate value.
        shift = min(attempt_number - 1, 16)
        delay: timedelta = self.retry_initial_delay * (2**shift)
        return delay if delay < self.retry_max_delay else self.retry_max_delay


WORKER_RUNTIME_V1 = WorkerRuntimePolicy(
    version="worker-runtime-v1",
    lease_duration=timedelta(seconds=60),
    max_attempts=3,
    max_lease_renewals=10,
    retry_initial_delay=timedelta(seconds=15),
    retry_max_delay=timedelta(minutes=10),
    claim_batch=10,
    # Ten seconds is below any market data cadence this system consumes, so the
    # floor exists to bound a misbehaving worker rather than to enable fast
    # polling. The ceiling keeps a watch from silently going to sleep for hours.
    min_wait_interval=timedelta(seconds=10),
    max_wait_interval=timedelta(minutes=15),
)
