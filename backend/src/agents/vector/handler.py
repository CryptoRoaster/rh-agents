"""VECTOR: the trade setup specialist worker.

The handler reasons and returns a typed result. It never opens a transaction,
never touches task state, never sets TradeCase status and never builds evidence
envelope metadata itself; the Phase 2B runtime owns the authoritative lifecycle.

Two decisions shape this file.

**A setup needs a model, and there is no fallback.** ATLAS keeps a deterministic
verdict when its model is unavailable because the verdict was never the model's.
VECTOR has nothing equivalent: proposing levels is the whole output. So a
provider failure is a retryable task failure and the requirement stays unmet —
never a default setup, and never the previous setup reissued as new. A stale
setup silently renewed would be the most dangerous artefact this system could
produce, because everything downstream treats a current setup as a current
opinion.

**A bad context is answered before the model, not after.** A missing, stale or
priceless market observation ends the attempt without a reasoning call: there is
no level to reason from, and asking anyway could only produce an invented one
that would have to be caught later by the validator, having already been paid
for.
"""

from dataclasses import dataclass
from datetime import timedelta

from src.agents.vector.context import (
    build_setup,
    reasoning_payload,
    vector_input_digest,
)
from src.agents.vector.models import (
    VECTOR_OUTPUT_SCHEMA_VERSION,
    VectorSetup,
    VectorSetupProposal,
    VectorTaskInput,
)
from src.agents.vector.policy import VECTOR_SETUP_V1, VectorSetupPolicy
from src.agents.vector.ports import VectorContextUnavailable
from src.agents.vector.prompt import (
    VECTOR_INSTRUCTIONS,
    VECTOR_PROMPT_HASH,
    VECTOR_PROMPT_VERSION,
)
from src.agents.vector.sufficiency import RECOVERABLE
from src.agents.vector.validation import VectorValidationError, trigger_for, validate_proposal
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import VectorCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import (
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    TradeSetupDetail,
    TradeSetupPayload,
    TradeSetupTrigger,
)
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningRequest,
    ReasoningResult,
)
from src.reasoning.provider import ReasoningProvider

VECTOR_TASK_TYPE = "DEFINE_TRADE_SETUP"

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
# a reasoning attempt. A market that has gone quiet may come back; one whose
# identity does not match the case is a wiring fault that retrying cannot fix.
CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "MARKET_OBSERVATION_MISSING": WorkerFailureCategory.TRANSIENT,
    "MARKET_OBSERVATION_TOO_STALE": WorkerFailureCategory.TRANSIENT,
    "PRICE_UNAVAILABLE": WorkerFailureCategory.TRANSIENT,
    "MARKET_IDENTITY_MISMATCH": WorkerFailureCategory.INTERNAL,
    "MARKET_OBSERVATION_IN_FUTURE": WorkerFailureCategory.INTERNAL,
    # A market that has not traded enough yet may trade enough later, so these
    # are retried. None of them produces a setup in the meantime.
    **{code.value: WorkerFailureCategory.TRANSIENT for code in RECOVERABLE},
    # A series for the wrong pool, in the wrong unit or on the wrong timeframe is
    # a wiring fault. Retrying cannot reach it.
    "MARKET_HISTORY_IDENTITY_MISMATCH": WorkerFailureCategory.INTERNAL,
    "MARKET_HISTORY_PRICE_BASIS_MISMATCH": WorkerFailureCategory.INTERNAL,
    "MARKET_HISTORY_TIMEFRAME_MISMATCH": WorkerFailureCategory.INTERNAL,
    "MARKET_HISTORY_IN_FUTURE": WorkerFailureCategory.INTERNAL,
    # No provider is wired at all. Retrying will not configure one, and inventing
    # structure to proceed without it is the exact failure this phase closed.
    "MARKET_HISTORY_SOURCE_NOT_CONFIGURED": WorkerFailureCategory.CAPABILITY_DENIED,
}


@dataclass(frozen=True)
class VectorWorkerHandler:
    provider: ReasoningProvider
    policy: VectorSetupPolicy = VECTOR_SETUP_V1
    max_output_tokens: int = 1024
    timeout: timedelta = timedelta(seconds=60)

    @property
    def role(self) -> AgentRole:
        return AgentRole.VECTOR

    @property
    def task_type(self) -> str:
        return VECTOR_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, VectorCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.setup_context(
                lease.trade_case_id, lease.task_id
            )
        except VectorContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.INTERNAL),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, VectorTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        digest = vector_input_digest(task_input)
        request: ReasoningRequest[VectorSetupProposal] = ReasoningRequest(
            instructions=VECTOR_INSTRUCTIONS,
            data=reasoning_payload(task_input),
            output_model=VectorSetupProposal,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout.total_seconds(),
        )
        try:
            result: ReasoningResult[VectorSetupProposal] = await self.provider.generate_structured(
                request
            )
        except ReasoningFailure as error:
            return TaskFailureReport(
                category=PROVIDER_FAILURES.get(error.category, WorkerFailureCategory.INTERNAL),
                reason_code=error.category.value,
            )
        try:
            validate_proposal(result.output, task_input, self.policy)
            trigger = trigger_for(result.output, task_input)
        except VectorValidationError as error:
            # A proposal that does not hold together is refused, never repaired.
            # Quietly reordering levels would attribute a setup to the model that
            # it did not make, in the one record meant to show what was decided.
            return TaskFailureReport(
                category=WorkerFailureCategory.INVALID_RESULT, reason_code=error.reason_code
            )
        setup = build_setup(result.output, trigger, task_input, digest)
        return EvidenceTaskResult(
            submission=self._submission(lease, task_input, setup, result, digest),
            result_key=f"vector:{setup.setup_fingerprint}",
        )

    def _submission(
        self,
        lease: TaskLease,
        task_input: VectorTaskInput,
        setup: VectorSetup,
        result: ReasoningResult[VectorSetupProposal],
        digest: str,
    ) -> EvidenceSubmission:
        """Build the envelope from runtime facts. The model fills only the setup."""
        structure = task_input.market.structure
        return EvidenceSubmission(
            idempotency_key=f"vector:{task_input.task_id}:{setup.setup_fingerprint}",
            producer_role=AgentRole.VECTOR,
            evidence_type=EvidenceType.TRADE_SETUP,
            provenance=EvidenceProvenance(
                source=f"vector:{result.model.provider}",
                reference_id=task_input.market.snapshot_id,
                source_version=VECTOR_PROMPT_VERSION,
            ),
            observed_at=task_input.market.observed_at,
            # The envelope stops being current exactly when the setup does, so
            # nothing downstream can treat an expired proposal as a live one.
            valid_until=setup.expires_at,
            status=EvidenceStatus.AVAILABLE,
            payload=TradeSetupPayload(
                setup_id=setup.setup_id,
                side=setup.side,
                # The legacy single entry is the highest price at which this
                # setup is entered — the breakout level, or the top of a pullback
                # band. The band itself survives in the detail below.
                entry_price=setup.entry_high,
                invalidation_price=setup.invalidation_price,
                target_prices=setup.targets,
                setup=TradeSetupDetail(
                    setup_fingerprint=setup.setup_fingerprint,
                    policy_version=setup.policy_version,
                    kind=setup.kind.value,
                    price_basis=setup.price_basis,
                    entry_low=setup.entry_low,
                    entry_high=setup.entry_high,
                    reference_price=setup.reference_price,
                    expires_at=setup.expires_at,
                    trigger=TradeSetupTrigger(
                        type=setup.trigger.type.value,
                        price_basis=setup.trigger.price_basis,
                        reference_price=setup.trigger.reference_price,
                        zone_low=setup.trigger.zone_low,
                        zone_high=setup.trigger.zone_high,
                        valid_from=setup.trigger.valid_from,
                        expires_at=setup.trigger.expires_at,
                    ),
                    reason_codes=tuple(code.value for code in setup.reason_codes),
                    summary=setup.summary,
                    input_digest=digest,
                    history_provider=structure.provider,
                    # A code in the payload's own vocabulary; the provider's
                    # lowercase spelling stays in the provider layer.
                    history_timeframe=structure.timeframe.upper(),
                    history_bar_count=len(structure.bars),
                    history_window_start=structure.window_start,
                    history_window_end=structure.window_end,
                    history_coverage=structure.coverage,
                    observed_range_low=structure.range_low,
                    observed_range_high=structure.range_high,
                    prompt_version=VECTOR_PROMPT_VERSION,
                    prompt_hash=VECTOR_PROMPT_HASH,
                    reasoning_provider=result.model.provider,
                    reasoning_model=result.model.model,
                    output_schema_version=VECTOR_OUTPUT_SCHEMA_VERSION,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    latency_ms=result.usage.latency_ms,
                ),
            ),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )
