"""Future-compatible worker runner. No model provider is integrated in Phase 2B.

A handler is a plain async callable that receives a lease and its role-scoped
capabilities and returns a typed outcome report. Deterministic fake handlers are
enough to prove the runtime; probabilistic reasoning arrives in a later phase.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from src.core.models import AgentRole
from src.orchestration.worker.capabilities import (
    AnchorCapabilities,
    AtlasCapabilities,
    CommanderCapabilities,
    DiscoveryContextPort,
    EvidenceSubmissionPort,
    ExecutionAssessmentPort,
    FuseCapabilities,
    MarketHistoryPort,
    OnchainContextPort,
    OrbitCapabilities,
    PulseCapabilities,
    SentimentPort,
    SignalCapabilities,
    TriggerFeedPort,
    ValidatedEvidencePort,
    VectorCapabilities,
    WorkflowStatePort,
)
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskDisposition,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerRegistration,
)
from src.orchestration.worker.service import WorkerRuntimeService


def new_registration_key() -> str:
    """Mint an idempotency token for exactly one runtime start.

    A registration key identifies a single process lifetime, not a role and not a
    deployment slot. Retrying the same registration reuses the key so the runtime
    keeps one worker_instance_id; a genuinely new process mints a new key and is a
    new instance. Deriving it from role, host or version instead would merge a
    crashed process and its replacement into one identity and make attempt history
    unable to tell them apart.
    """
    return f"runtime:{uuid4()}"


REFUSAL_CATEGORIES = {
    WorkerErrorCode.ROLE_NOT_AUTHORIZED: WorkerFailureCategory.CAPABILITY_DENIED,
    WorkerErrorCode.EVIDENCE_TYPE_NOT_AUTHORIZED: WorkerFailureCategory.CAPABILITY_DENIED,
    WorkerErrorCode.TASK_SUPERSEDED: WorkerFailureCategory.TASK_INVALIDATED,
    WorkerErrorCode.RESULT_CONFLICT: WorkerFailureCategory.INVALID_RESULT,
}


class WorkerHandler(Protocol):
    """Role-bound unit of reasoning. Receives capabilities, returns typed output."""

    @property
    def role(self) -> AgentRole: ...

    @property
    def task_type(self) -> str: ...

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport: ...


@dataclass(frozen=True)
class BoundEvidenceSubmission:
    """An evidence write already bound to one lease.

    A worker cannot name a different task, attempt or trade case; it can only
    answer for the lease it was given. The service re-verifies regardless.
    """

    _lease: TaskLease
    _service: WorkerRuntimeService

    @property
    def lease(self) -> TaskLease:
        return self._lease

    async def submit_evidence(self, submission: object, *, result_key: str) -> TaskDisposition:
        result = EvidenceTaskResult.model_validate(
            {"submission": submission, "result_key": result_key}
        )
        return await self._service.submit_task_result(self._lease, result)


@dataclass(frozen=True)
class CapabilityProvider:
    """Builds the role-scoped capability object for a lease.

    Ports left unset are simply absent: the matching role cannot be run, which is
    honest about what Phase 2B actually implements rather than handing a worker a
    stub that silently returns nothing.
    """

    service: WorkerRuntimeService
    context: DiscoveryContextPort | None = None
    onchain: OnchainContextPort | None = None
    sentiment: SentimentPort | None = None
    history: MarketHistoryPort | None = None
    triggers: TriggerFeedPort | None = None
    execution: ExecutionAssessmentPort | None = None
    evidence: ValidatedEvidencePort | None = None
    workflow: WorkflowStatePort | None = None

    def build(self, lease: TaskLease) -> object:
        submit: EvidenceSubmissionPort = BoundEvidenceSubmission(lease, self.service)
        match lease.role:
            case AgentRole.ORBIT if self.context is not None:
                return OrbitCapabilities(lease=lease, context=self.context, submit=submit)
            case AgentRole.ATLAS if self.onchain is not None:
                return AtlasCapabilities(lease=lease, context=self.onchain, submit=submit)
            case AgentRole.SIGNAL if self.sentiment is not None:
                return SignalCapabilities(lease=lease, context=self.sentiment, submit=submit)
            case AgentRole.VECTOR if self.history is not None:
                return VectorCapabilities(lease=lease, history=self.history, submit=submit)
            case AgentRole.PULSE if self.triggers is not None:
                return PulseCapabilities(lease=lease, triggers=self.triggers, submit=submit)
            case AgentRole.ANCHOR if self.execution is not None:
                return AnchorCapabilities(lease=lease, execution=self.execution, submit=submit)
            case AgentRole.FUSE if self.evidence is not None:
                return FuseCapabilities(lease=lease, evidence=self.evidence)
            case AgentRole.COMMANDER if self.workflow is not None:
                return CommanderCapabilities(lease=lease, workflow=self.workflow)
            case _:
                raise WorkerFailure(WorkerErrorCode.ROLE_NOT_AUTHORIZED)


class WorkerRunner:
    """Bounded claim/handle/report loop. Never started implicitly by the API."""

    def __init__(
        self,
        service: WorkerRuntimeService,
        handler: WorkerHandler,
        capabilities: CapabilityProvider,
        *,
        registration_key: str | None = None,
        runtime_version: str = "worker-runtime-v1",
        poll_interval: timedelta = timedelta(seconds=5),
    ) -> None:
        if poll_interval < timedelta(seconds=1) or poll_interval > timedelta(minutes=5):
            raise ValueError("Poll interval must be between one second and five minutes")
        self.service = service
        self.handler = handler
        self.capabilities = capabilities
        # One runner is one runtime lifetime, so it mints its own key by default.
        # Passing one explicitly is for resuming a specific registration, never for
        # pinning a role or deployment to a permanent identity.
        self.registration_key = (
            registration_key if registration_key is not None else new_registration_key()
        )
        self.runtime_version = runtime_version
        self.poll_interval = poll_interval
        self.worker_instance_id: UUID | None = None

    async def register(self) -> UUID:
        instance = await self.service.register_worker(
            WorkerRegistration(
                registration_key=self.registration_key,
                role=self.handler.role,
                runtime_version=self.runtime_version,
            )
        )
        self.worker_instance_id = instance.worker_instance_id
        return instance.worker_instance_id

    async def run_once(self) -> TaskDisposition | None:
        """Claim at most one task and carry it to a durable disposition."""
        if self.worker_instance_id is None:
            raise WorkerFailure(WorkerErrorCode.WORKER_NOT_FOUND)
        lease = await self.service.claim_next_task(self.worker_instance_id)
        if lease is None:
            return None
        try:
            report = await self.handler.handle(lease, self.capabilities.build(lease))
        except asyncio.CancelledError:
            # Leave the lease to expire rather than writing a false outcome on the
            # way out; recovery reclaims it deterministically.
            raise
        except WorkerFailure:
            return await self.service.report_task_failure(
                lease,
                TaskFailureReport(
                    category=WorkerFailureCategory.CAPABILITY_DENIED,
                    reason_code="CAPABILITY_DENIED",
                ),
            )
        except Exception:
            # Never let an unknown handler bug look like success, and never carry
            # provider detail back into durable state.
            return await self.service.report_task_failure(
                lease,
                TaskFailureReport(
                    category=WorkerFailureCategory.INTERNAL, reason_code="HANDLER_ERROR"
                ),
            )
        if isinstance(report, TaskFailureReport):
            return await self.service.report_task_failure(lease, report)
        try:
            return await self.service.submit_task_result(lease, report)
        except WorkerFailure as error:
            return await self._record_refusal(lease, error)

    async def _record_refusal(self, lease: TaskLease, error: WorkerFailure) -> TaskDisposition:
        """A refused submission must leave durable state, not just raise.

        Only refusals the worker still has standing to report are recorded. Once
        the lease itself is gone the worker has no authority to write anything, so
        the refusal propagates and recovery owns the task instead.
        """
        category = REFUSAL_CATEGORIES.get(error.code)
        if category is None:
            raise error
        return await self.service.report_task_failure(
            lease, TaskFailureReport(category=category, reason_code=error.code.value)
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until asked to stop. Waits on the stop event instead of sleeping,
        so shutdown is immediate and no busy-spin or orphan task is created."""
        if self.worker_instance_id is None:
            await self.register()
        while not stop.is_set():
            disposition = await self.run_once()
            if disposition is not None:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval.total_seconds())
            except TimeoutError:
                continue
