"""The FUSE worker: read the admissible evidence, record one synthesis of it.

Deterministic throughout. There is no reasoning provider here, no prompt, no
retry on a model that answered differently — the same evidence yields the same
synthesis and therefore the same fingerprint, which is what makes a replay
resolve to one piece of evidence rather than two readings of the same facts.

The handler itself makes no judgements. It binds a lease to a context, calls a
pure function, and packages the answer. Everything that decides anything lives
in `synthesis.py`, and everything that decides what may be read lives in
`context.py`, on the server.
"""

import hashlib
import json
from dataclasses import dataclass

from src.agents.fuse.models import (
    FUSE_OUTPUT_SCHEMA_VERSION,
    EvidenceSynthesis,
    FuseReasonCode,
    FuseTaskInput,
    HardBlocker,
    SynthesisFactor,
    UnresolvedGap,
)
from src.agents.fuse.policy import FUSE_SYNTHESIS_V1, FuseSynthesisPolicy
from src.agents.fuse.ports import FuseContextUnavailable
from src.agents.fuse.synthesis import synthesize
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import FuseCapabilities
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
    SynthesisDetail,
    SynthesisFinding,
    SynthesisPayload,
    SynthesisSource,
)

FUSE_TASK_TYPE = "SYNTHESIZE_EVIDENCE"

# Why a context could not be built, and what kind of failure each one is. None
# of these is a statement about the case's evidence; they are reasons the
# question could not be asked.
CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "TRADE_CASE_TERMINAL": WorkerFailureCategory.TASK_INVALIDATED,
    "EVIDENCE_ROLE_MISMATCH": WorkerFailureCategory.INTERNAL,
    "UNSUPPORTED_EVIDENCE_PAYLOAD": WorkerFailureCategory.INTERNAL,
}

# Why a synthesis could not honestly be produced. Never recorded as evidence,
# because "there was nothing to read" is not a finding about a market.
UNSYNTHESIZABLE: dict[FuseReasonCode, WorkerFailureCategory] = {
    FuseReasonCode.NO_ADMISSIBLE_EVIDENCE: WorkerFailureCategory.TRANSIENT,
    FuseReasonCode.SOURCES_NOT_CONCURRENT: WorkerFailureCategory.INTERNAL,
}


def synthesis_fingerprint(
    task_input: FuseTaskInput, synthesis: EvidenceSynthesis, policy_version: str
) -> str:
    """A canonical fingerprint of the synthesis and what produced it.

    Covers the input digest, the derived blockers and gaps, the factors and the
    policy. Deliberately excludes the lease, the worker instance, the attempt
    number and any latency — two workers synthesizing the same evidence must
    produce the same fingerprint, or a replay would look like a new finding.
    """
    canonical = json.dumps(
        {
            "input_digest": synthesis.input_digest,
            "policy_version": policy_version,
            "schema_version": FUSE_OUTPUT_SCHEMA_VERSION,
            "disposition": synthesis.disposition.value,
            "hard_blockers": sorted(
                (item.code, str(item.evidence_id), item.origin.value)
                for item in synthesis.hard_blockers
            ),
            "unresolved_gaps": sorted(
                (item.code, str(item.evidence_id or ""), item.origin.value)
                for item in synthesis.unresolved_gaps
            ),
            "support_factors": sorted(
                (item.code, str(item.evidence_id)) for item in synthesis.support_factors
            ),
            "caution_factors": sorted(
                (item.code, str(item.evidence_id)) for item in synthesis.caution_factors
            ),
            "observed_at": synthesis.observed_at.isoformat(),
            "valid_until": synthesis.valid_until.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _finding(
    item: HardBlocker | UnresolvedGap | SynthesisFactor,
    *,
    safety_critical: bool | None = None,
) -> SynthesisFinding:
    """One derived finding, flattened for storage with its attribution intact.

    A factor has no origin of its own — it is an observation rather than a
    verdict — so it records that it was observed. Blockers and gaps carry the
    origin that produced them, which is what lets a reader tell a measured
    failure from an absent measurement without reading the statement.
    """
    origin = item.origin.value if not isinstance(item, SynthesisFactor) else "OBSERVED"
    return SynthesisFinding(
        code=item.code,
        role=item.role,
        evidence_type=item.evidence_type,
        origin=origin,
        statement=item.statement,
        evidence_id=item.evidence_id,
        safety_critical=safety_critical,
    )


@dataclass(frozen=True)
class FuseWorkerHandler:
    """Claims SYNTHESIZE_EVIDENCE tasks and records one reading of the evidence."""

    policy: FuseSynthesisPolicy = FUSE_SYNTHESIS_V1

    @property
    def role(self) -> AgentRole:
        return AgentRole.FUSE

    @property
    def task_type(self) -> str:
        return FUSE_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, FuseCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.synthesis_context(
                lease.trade_case_id, lease.task_id
            )
        except FuseContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.INTERNAL),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, FuseTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        outcome = synthesize(task_input, task_input.evaluated_at, self.policy)
        if isinstance(outcome, FuseReasonCode):
            return TaskFailureReport(
                category=UNSYNTHESIZABLE.get(outcome, WorkerFailureCategory.TRANSIENT),
                reason_code=outcome.value,
            )
        return self._evidence(lease, task_input, outcome)

    def _evidence(
        self, lease: TaskLease, task_input: FuseTaskInput, synthesis: EvidenceSynthesis
    ) -> EvidenceTaskResult:
        fingerprint = synthesis_fingerprint(task_input, synthesis, self.policy.version)
        sources = tuple(
            SynthesisSource(
                role=item.role,
                evidence_type=item.evidence_type,
                evidence_id=item.evidence_id,
                submission_fingerprint=item.submission_fingerprint,
                status=item.status.value,
                acceptance=item.acceptance.value,
                observed_at=item.observed_at,
                valid_until=item.valid_until,
                required=item.required,
                safety_critical=item.safety_critical,
            )
            for item in synthesis.sources
        )
        return EvidenceTaskResult(
            submission=EvidenceSubmission(
                # Keyed by the synthesis itself, so the same reading of the same
                # evidence resubmitted after a lost acknowledgement resolves to
                # one piece of evidence rather than two.
                idempotency_key=f"fuse:{fingerprint}",
                producer_role=AgentRole.FUSE,
                evidence_type=EvidenceType.SYNTHESIS,
                provenance=EvidenceProvenance(
                    source=f"fuse:{self.policy.version}",
                    # The case this reading is about, never the task that
                    # produced it. A task id would make two synthesizers reading
                    # identical evidence produce different submissions under the
                    # same idempotency key, which resolves as a conflict rather
                    # than as the replay it actually is.
                    reference_id=task_input.trade_case_id,
                    source_version=self.policy.version,
                ),
                # Both times come from the sources, never from this run. A
                # synthesis is exactly as old as the oldest thing it read, and
                # it stops being current when the first of them does — otherwise
                # re-running FUSE would launder stale facts into fresh evidence.
                observed_at=synthesis.observed_at,
                valid_until=synthesis.valid_until,
                # A synthesis that found problems still established what it set
                # out to establish. What it found is carried by acceptance, which
                # is the axis for "what the fact means" — status is the axis for
                # "could the fact be observed", and it could.
                status=EvidenceStatus.AVAILABLE,
                reason_codes=(synthesis.disposition.value,),
                payload=SynthesisPayload(
                    disposition=synthesis.disposition.value,
                    source_evidence_ids=tuple(item.evidence_id for item in synthesis.sources),
                    synthesis=SynthesisDetail(
                        policy_version=synthesis.policy_version,
                        disposition=synthesis.disposition.value,
                        hard_blockers=tuple(_finding(item) for item in synthesis.hard_blockers),
                        unresolved_gaps=tuple(
                            _finding(item, safety_critical=item.safety_critical)
                            for item in synthesis.unresolved_gaps
                        ),
                        support_factors=tuple(_finding(item) for item in synthesis.support_factors),
                        caution_factors=tuple(_finding(item) for item in synthesis.caution_factors),
                        sources=sources,
                        input_digest=synthesis.input_digest,
                        synthesis_fingerprint=fingerprint,
                        evaluated_at=synthesis.evaluated_at,
                    ),
                ),
                correlation_id=lease.correlation_id,
            ),
            result_key=f"fuse:{fingerprint}",
        )


__all__ = ["FUSE_TASK_TYPE", "FuseWorkerHandler", "synthesis_fingerprint"]
