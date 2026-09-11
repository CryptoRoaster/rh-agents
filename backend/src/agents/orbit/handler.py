"""ORBIT: the first real specialist worker, and the reference for the others.

The handler reasons and returns a typed result. It never opens a transaction,
never touches task state, never sets TradeCase status and never builds evidence
envelope metadata itself; the Phase 2B runtime owns the authoritative lifecycle.

Model invocation is at-least-once like every other part of the runtime: after an
ambiguous failure the model may be called again. That is accepted. The
authoritative effect stays idempotent because the runtime keys evidence on the
task attempt, not on the model call.
"""

from dataclasses import dataclass
from datetime import timedelta

from src.agents.orbit.context import (
    OrbitContextUnavailable,
    orbit_input_digest,
    reasoning_payload,
)
from src.agents.orbit.models import (
    ORBIT_OUTPUT_SCHEMA_VERSION,
    OrbitAssessment,
    OrbitClassification,
    OrbitTaskInput,
)
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS, ORBIT_PROMPT_HASH, ORBIT_PROMPT_VERSION
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import OrbitCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import (
    DiscoveryAssessment,
    DiscoveryPayload,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
)
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningRequest,
    ReasoningResult,
)
from src.reasoning.provider import ReasoningProvider

ORBIT_TASK_TYPE = "VERIFY_DISCOVERY"

# Phase 2B owns authoritative task retries; this only says what kind of failure
# each provider outcome is. A missing credential is permanent because retrying it
# cannot help, while transport problems and malformed output get a bounded retry.
PROVIDER_FAILURES: dict[ReasoningErrorCategory, WorkerFailureCategory] = {
    ReasoningErrorCategory.PROVIDER_TIMEOUT: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_RATE_LIMIT: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_UNAVAILABLE: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_REFUSED: WorkerFailureCategory.INVALID_RESULT,
    ReasoningErrorCategory.INVALID_MODEL_OUTPUT: WorkerFailureCategory.INVALID_RESULT,
    ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST: WorkerFailureCategory.INTERNAL,
    ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED: WorkerFailureCategory.CAPABILITY_DENIED,
}

# Context problems are decided before any model call, so a bad input never burns
# reasoning attempts.
CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "MARKET_OBSERVATION_MISSING": WorkerFailureCategory.TRANSIENT,
    "MARKET_OBSERVATION_TOO_STALE": WorkerFailureCategory.TRANSIENT,
    "MARKET_IDENTITY_MISMATCH": WorkerFailureCategory.INTERNAL,
    "MARKET_OBSERVATION_IN_FUTURE": WorkerFailureCategory.INTERNAL,
}

# A classification is a successful reasoning outcome either way. Only an explicit
# inability to judge is recorded as UNKNOWN, so AVAILABLE never smuggles one.
EVIDENCE_STATUS: dict[OrbitClassification, EvidenceStatus] = {
    OrbitClassification.INTERESTING: EvidenceStatus.AVAILABLE,
    OrbitClassification.NOT_INTERESTING: EvidenceStatus.AVAILABLE,
    OrbitClassification.INSUFFICIENT_DATA: EvidenceStatus.UNKNOWN,
}


@dataclass(frozen=True)
class OrbitWorkerHandler:
    provider: ReasoningProvider
    max_output_tokens: int = 1024
    timeout: timedelta = timedelta(seconds=60)

    @property
    def role(self) -> AgentRole:
        return AgentRole.ORBIT

    @property
    def task_type(self) -> str:
        return ORBIT_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, OrbitCapabilities):
            # Only the composed ORBIT capability is ever acceptable.
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.candidate_context(
                lease.trade_case_id, lease.task_id
            )
        except OrbitContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.INTERNAL),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, OrbitTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        digest = orbit_input_digest(task_input)
        request: ReasoningRequest[OrbitAssessment] = ReasoningRequest(
            instructions=ORBIT_INSTRUCTIONS,
            data=reasoning_payload(task_input),
            output_model=OrbitAssessment,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout.total_seconds(),
        )
        try:
            result: ReasoningResult[OrbitAssessment] = await self.provider.generate_structured(
                request
            )
        except ReasoningFailure as error:
            return TaskFailureReport(
                category=PROVIDER_FAILURES.get(error.category, WorkerFailureCategory.INTERNAL),
                reason_code=error.category.value,
            )
        try:
            validate_assessment(result.output, task_input)
        except OrbitValidationError as error:
            # Contradicted output is never persisted, not even as UNKNOWN evidence.
            return TaskFailureReport(
                category=WorkerFailureCategory.INVALID_RESULT, reason_code=error.reason_code
            )
        return EvidenceTaskResult(
            submission=self._submission(lease, task_input, result, digest),
            result_key=f"orbit:{digest}",
        )

    def _submission(
        self,
        lease: TaskLease,
        task_input: OrbitTaskInput,
        result: ReasoningResult[OrbitAssessment],
        digest: str,
    ) -> EvidenceSubmission:
        """Build the envelope from runtime facts. The model fills only the payload."""
        assessment = result.output
        gaps = tuple(code.value for code in assessment.data_gaps)
        status = EVIDENCE_STATUS[assessment.classification]
        return EvidenceSubmission(
            # Replaced by the runtime with a lease-derived identity.
            idempotency_key=f"orbit:{task_input.task_id}:{digest}",
            producer_role=AgentRole.ORBIT,
            evidence_type=EvidenceType.DISCOVERY,
            provenance=EvidenceProvenance(
                source=f"orbit:{result.model.provider}",
                reference_id=task_input.candidate.snapshot_id,
                source_version=ORBIT_PROMPT_VERSION,
            ),
            observed_at=task_input.candidate.observed_at,
            valid_until=task_input.evaluated_at + timedelta(hours=1),
            status=status,
            # An unavailable envelope must name what was missing. The assessment
            # model already guarantees INSUFFICIENT_DATA carries at least one gap.
            reason_codes=() if status == EvidenceStatus.AVAILABLE else gaps,
            payload=DiscoveryPayload(
                discovery_reference=task_input.discovery_reference,
                assessment=DiscoveryAssessment(
                    classification=assessment.classification.value,
                    strength=assessment.strength.value,
                    reason_codes=tuple(code.value for code in assessment.reason_codes),
                    data_gaps=gaps,
                    cited_observation_ids=assessment.cited_observation_ids,
                    summary=assessment.summary,
                    input_digest=digest,
                    prompt_version=ORBIT_PROMPT_VERSION,
                    prompt_hash=ORBIT_PROMPT_HASH,
                    reasoning_provider=result.model.provider,
                    reasoning_model=result.model.model,
                    output_schema_version=ORBIT_OUTPUT_SCHEMA_VERSION,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    latency_ms=result.usage.latency_ms,
                ),
            ),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )
