"""SIGNAL: the social attention specialist worker.

The handler reasons and returns a typed result. It never opens a transaction,
never touches task state, never sets TradeCase status and never builds evidence
envelope metadata itself; the Phase 2B runtime owns the authoritative lifecycle.

Two decisions shape this file.

**An unusable set is answered without a model.** When the deterministic policy
already knows there is nothing to read — no posts, none inside the window, too
few voices to generalize from — no reasoning call is made at all. Asking a model
to interpret an empty feed can only produce invented sentiment, and it would be
paid for.

**A readable set that the model could not read is a failure, not an observation.**
Where ATLAS keeps a deterministic verdict when its model is unavailable, SIGNAL
cannot: its whole output is an interpretation of language, and there is no
non-probabilistic fallback for what words mean. So a provider failure becomes a
retryable task failure and the requirement stays unmet, rather than a neutral
reading nobody made. The deterministic metrics survive in the attempt record.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from src.agents.signal.context import reasoning_payload, signal_input_digest
from src.agents.signal.models import (
    SIGNAL_OUTPUT_SCHEMA_VERSION,
    SentimentDirection,
    SignalAssessment,
    SignalDataQuality,
    SignalGap,
    SignalQualityFeatures,
    SignalStructuralAssessment,
    SignalTaskInput,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1, SignalQualityPolicy
from src.agents.signal.ports import SignalSourceUnavailable
from src.agents.signal.prompt import (
    SIGNAL_INSTRUCTIONS,
    SIGNAL_PROMPT_HASH,
    SIGNAL_PROMPT_VERSION,
)
from src.agents.signal.validation import SignalValidationError, validate_assessment
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import SignalCapabilities
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
    SentimentIntelligence,
    SentimentPayload,
    SentimentSourceMetrics,
)
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningRequest,
    ReasoningResult,
)
from src.reasoning.provider import ReasoningProvider

SIGNAL_TASK_TYPE = "ASSESS_SENTIMENT"

# A direction the model could not read is recorded as exactly that. It is never
# mapped onto NEUTRAL, which would turn "we could not tell" into an observation
# that people were indifferent.
UNCLEAR_REASON = "SENTIMENT_DIRECTION_UNCLEAR"

PROVIDER_FAILURES: dict[ReasoningErrorCategory, WorkerFailureCategory] = {
    ReasoningErrorCategory.PROVIDER_TIMEOUT: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_RATE_LIMIT: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_UNAVAILABLE: WorkerFailureCategory.TRANSIENT,
    ReasoningErrorCategory.PROVIDER_REFUSED: WorkerFailureCategory.INVALID_RESULT,
    ReasoningErrorCategory.INVALID_MODEL_OUTPUT: WorkerFailureCategory.INVALID_RESULT,
    ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST: WorkerFailureCategory.INTERNAL,
    ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED: WorkerFailureCategory.CAPABILITY_DENIED,
}

# The legacy sentiment field the workflow has always carried. MIXED and NEUTRAL
# both collapse to NEUTRAL there; the distinction survives in the intelligence
# record, which is where anything that cares should read it.
LegacyAssessment = Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN"]

LEGACY_ASSESSMENT: dict[SentimentDirection, LegacyAssessment] = {
    SentimentDirection.POSITIVE: "POSITIVE",
    SentimentDirection.NEGATIVE: "NEGATIVE",
    SentimentDirection.MIXED: "NEUTRAL",
    SentimentDirection.NEUTRAL: "NEUTRAL",
    SentimentDirection.UNCLEAR: "UNKNOWN",
}


def _metrics(features: SignalQualityFeatures, window_seconds: int) -> SentimentSourceMetrics:
    return SentimentSourceMetrics(
        observation_count=features.observation_count,
        unique_author_count=features.unique_author_count,
        unique_authoring_count=features.unique_authoring_count,
        original_count=features.original_count,
        repost_count=features.repost_count,
        reply_count=features.reply_count,
        unique_content_count=features.unique_content_count,
        duplicate_cluster_count=len(features.duplicate_clusters),
        duplicate_share=features.duplicate_share,
        largest_duplicate_cluster_share=features.largest_duplicate_cluster_share,
        top1_author_share=features.top1_author_share,
        top5_author_share=features.top5_author_share,
        burst_share=features.burst_share,
        strong_binding_count=features.strong_binding_count,
        weak_binding_count=features.weak_binding_count,
        excluded_ambiguous_count=features.excluded_ambiguous_count,
        excluded_outside_window_count=features.excluded_outside_window_count,
        coverage=features.coverage.value,
        source_count=features.source_count,
        sources=tuple(entry.source.value for entry in features.sources),
        window_seconds=window_seconds,
        oldest_observation_at=features.oldest_observation_at,
        latest_observation_at=features.latest_observation_at,
        content_hash_algorithm=features.content_hash_algorithm,
    )


def _unusable_status(structure: SignalStructuralAssessment) -> EvidenceStatus:
    """Why nothing could be read, kept distinguishable in the audit record.

    A source that failed, a window with nothing in it and a genuinely quiet
    market are three different situations with three different remedies, and
    flattening them into one status would hide which one occurred.
    """
    if SignalGap.SOURCE_UNAVAILABLE in structure.gaps:
        return EvidenceStatus.UNAVAILABLE
    if SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW in structure.gaps:
        return EvidenceStatus.STALE
    return EvidenceStatus.UNKNOWN


@dataclass(frozen=True)
class SignalWorkerHandler:
    provider: ReasoningProvider
    policy: SignalQualityPolicy = SIGNAL_QUALITY_V1
    validity: timedelta = timedelta(hours=1)
    max_output_tokens: int = 1024
    timeout: timedelta = timedelta(seconds=60)

    @property
    def role(self) -> AgentRole:
        return AgentRole.SIGNAL

    @property
    def task_type(self) -> str:
        return SIGNAL_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, SignalCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.sentiment_context(
                lease.trade_case_id, lease.task_id
            )
        except SignalSourceUnavailable as error:
            return TaskFailureReport(
                category=WorkerFailureCategory.TRANSIENT, reason_code=error.reason_code
            )
        if not isinstance(task_input, SignalTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        digest = signal_input_digest(task_input, self.policy)
        if task_input.structure.data_quality == SignalDataQuality.INSUFFICIENT:
            # Nothing to interpret, so nothing is asked of a model. This is a
            # successful worker outcome carrying an honest absence, not a failure.
            return EvidenceTaskResult(
                submission=self._unreadable(lease, task_input, digest),
                result_key=f"signal:{digest}",
            )

        request: ReasoningRequest[SignalAssessment] = ReasoningRequest(
            instructions=SIGNAL_INSTRUCTIONS,
            data=reasoning_payload(task_input, self.policy),
            output_model=SignalAssessment,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout.total_seconds(),
        )
        try:
            result: ReasoningResult[SignalAssessment] = await self.provider.generate_structured(
                request
            )
        except ReasoningFailure as error:
            return TaskFailureReport(
                category=PROVIDER_FAILURES.get(error.category, WorkerFailureCategory.INTERNAL),
                reason_code=error.category.value,
            )
        try:
            validate_assessment(result.output, task_input, self.policy)
        except SignalValidationError as error:
            # Output that invents a source or overrules the measured structure is
            # never persisted, not even as unknown evidence.
            return TaskFailureReport(
                category=WorkerFailureCategory.INVALID_RESULT, reason_code=error.reason_code
            )
        return EvidenceTaskResult(
            submission=self._read(lease, task_input, result, digest),
            result_key=f"signal:{digest}",
        )

    def _window_seconds(self) -> int:
        return int(self.policy.window.total_seconds())

    def _provenance(self, task_input: SignalTaskInput, source: str) -> EvidenceProvenance:
        return EvidenceProvenance(
            source=source,
            reference_id=task_input.trade_case_id,
            source_version=task_input.structure.policy_version,
        )

    def _unreadable(
        self, lease: TaskLease, task_input: SignalTaskInput, digest: str
    ) -> EvidenceSubmission:
        """Deterministic evidence for a set that cannot support a reading.

        Zero observations is not neutral sentiment, so the assessment is UNKNOWN
        and the envelope is not available. The metrics still travel: knowing that
        four hundred posts were dropped as ambiguous tickers is itself useful, and
        a later FUSE should not have to guess why the domain is empty.
        """
        structure = task_input.structure
        gaps = tuple(gap.value for gap in structure.gaps) or (SignalGap.NO_OBSERVATIONS.value,)
        return EvidenceSubmission(
            idempotency_key=f"signal:{task_input.task_id}:{digest}",
            producer_role=AgentRole.SIGNAL,
            evidence_type=EvidenceType.SENTIMENT,
            provenance=self._provenance(task_input, "signal:deterministic"),
            observed_at=task_input.features.latest_observation_at or task_input.evaluated_at,
            valid_until=task_input.evaluated_at + self.validity,
            status=_unusable_status(structure),
            reason_codes=gaps,
            payload=SentimentPayload(
                assessment="UNKNOWN",
                intelligence=SentimentIntelligence(
                    policy_version=structure.policy_version,
                    data_quality=structure.data_quality.value,
                    attention_level=structure.attention_level.value,
                    organic_breadth=structure.organic_breadth.value,
                    manipulation_concern=structure.manipulation_concern.value,
                    gaps=gaps,
                    metrics=_metrics(task_input.features, self._window_seconds()),
                    input_digest=digest,
                ),
            ),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )

    def _read(
        self,
        lease: TaskLease,
        task_input: SignalTaskInput,
        result: ReasoningResult[SignalAssessment],
        digest: str,
    ) -> EvidenceSubmission:
        """Build the envelope from runtime facts. The model fills only its own axes."""
        assessment = result.output
        structure = task_input.structure
        gaps = tuple(gap.value for gap in structure.gaps)
        unclear = assessment.sentiment_direction == SentimentDirection.UNCLEAR
        status = EvidenceStatus.UNKNOWN if unclear else EvidenceStatus.AVAILABLE
        return EvidenceSubmission(
            idempotency_key=f"signal:{task_input.task_id}:{digest}",
            producer_role=AgentRole.SIGNAL,
            evidence_type=EvidenceType.SENTIMENT,
            provenance=self._provenance(task_input, f"signal:{result.model.provider}"),
            observed_at=task_input.features.latest_observation_at or task_input.evaluated_at,
            valid_until=task_input.evaluated_at + self.validity,
            status=status,
            reason_codes=() if status == EvidenceStatus.AVAILABLE else (UNCLEAR_REASON, *gaps),
            payload=SentimentPayload(
                assessment=LEGACY_ASSESSMENT[assessment.sentiment_direction],
                intelligence=SentimentIntelligence(
                    policy_version=structure.policy_version,
                    data_quality=structure.data_quality.value,
                    attention_level=structure.attention_level.value,
                    organic_breadth=structure.organic_breadth.value,
                    manipulation_concern=structure.manipulation_concern.value,
                    gaps=gaps,
                    metrics=_metrics(task_input.features, self._window_seconds()),
                    input_digest=digest,
                    sentiment_direction=assessment.sentiment_direction.value,
                    sentiment_strength=assessment.sentiment_strength.value,
                    social_demand_indication=assessment.social_demand_indication.value,
                    narrative_tags=tuple(tag.value for tag in assessment.narrative_tags),
                    advisory_manipulation_observations=tuple(
                        item.value for item in assessment.manipulation_observations
                    ),
                    advisory_summary=assessment.summary,
                    cited_observation_ids=assessment.cited_observation_ids,
                    prompt_version=SIGNAL_PROMPT_VERSION,
                    prompt_hash=SIGNAL_PROMPT_HASH,
                    reasoning_provider=result.model.provider,
                    reasoning_model=result.model.model,
                    output_schema_version=SIGNAL_OUTPUT_SCHEMA_VERSION,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    latency_ms=result.usage.latency_ms,
                ),
            ),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )
