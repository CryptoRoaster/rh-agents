"""ANCHOR: the execution-liquidity specialist worker.

The handler reads its context, evaluates arithmetic, and returns typed evidence
or a typed failure. It never opens a transaction, never touches task state,
never sets TradeCase status and never builds envelope metadata itself; the Phase
2B runtime owns the authoritative lifecycle.

**No model, and none possible.** Like PULSE, this package imports no reasoning
provider and holds no prompt. Every conclusion follows from quotes and a
versioned policy, which is what makes "why did ANCHOR say five hundred?"
answerable at all.

**A market that cannot serve the trade is evidence, not an error.** The most
important distinction in this file is between the provider telling us the market
has no route — a fact, recorded as available and blocking evidence — and the
provider failing to tell us anything, which is an absence and must be retried.
Collapsing them would let an outage read as an illiquid market, and a later
retry as the market recovering.

**Nothing here authorises anything.** The capacity reported is what the market
was shown to bear. SENTINEL decides what may actually be traded, from facts this
worker cannot see, and a future executor must re-quote before acting because a
quote is an offer at a moment.
"""

import json
from dataclasses import dataclass
from hashlib import sha256

from src.agents.anchor.assessment import assess
from src.agents.anchor.models import (
    AnchorReasonCode,
    AnchorTaskInput,
    CapacitySemantics,
    ExecutionAssessment,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, AnchorExecutionPolicy
from src.agents.anchor.ports import AnchorContextUnavailable
from src.core.models import AgentRole
from src.core.numbers import canonical_decimal
from src.orchestration.worker.capabilities import AnchorCapabilities
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
    ExecutionAssessmentDetail,
    LiquidityExecutionPayload,
    QuotedLadderPoint,
)

ANCHOR_TASK_TYPE = "ASSESS_EXECUTION"

# Context problems, categorised by whether another attempt could resolve them.
CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "MARKET_OBSERVATION_MISSING": WorkerFailureCategory.TRANSIENT,
    "NO_TRIGGERED_SETUP": WorkerFailureCategory.TRANSIENT,
    "TRIGGER_NOT_FOR_CURRENT_SETUP": WorkerFailureCategory.TRANSIENT,
    "MARKET_IDENTITY_MISMATCH": WorkerFailureCategory.INTERNAL,
    "TOKEN_DECIMALS_UNKNOWN": WorkerFailureCategory.INTERNAL,
}

# Reasons the assessment could not honestly be made. These are absences of
# evidence, so they are retried rather than written down as facts about a market.
UNASSESSABLE: dict[AnchorReasonCode, WorkerFailureCategory] = {
    AnchorReasonCode.REFERENCE_UNAVAILABLE: WorkerFailureCategory.TRANSIENT,
    AnchorReasonCode.REFERENCE_TOO_STALE: WorkerFailureCategory.TRANSIENT,
    AnchorReasonCode.QUOTES_UNAVAILABLE: WorkerFailureCategory.TRANSIENT,
}


def execution_digest(task_input: AnchorTaskInput, assessment: ExecutionAssessment) -> str:
    """Canonical fingerprint of one execution assessment.

    Covers the case, the setup and trigger it is bound to, the market, the
    reference, and every tested point with its verdict. Excludes the lease, the
    worker, the attempt and the moment of evaluation: the same ladder assessed
    twice is the same fact, and a digest that disagreed would make a replay look
    like a second opinion.
    """
    canonical = json.dumps(
        {
            "trade_case_id": str(task_input.trade_case_id),
            "setup_evidence_id": str(task_input.setup_evidence_id),
            "setup_fingerprint": task_input.setup_fingerprint,
            "trigger_evidence_id": str(task_input.trigger_evidence_id),
            "pair_id": task_input.market.pair_id,
            "chain": task_input.market.chain,
            "network": task_input.market.network,
            "payment_asset": task_input.market.quote_asset_id,
            "target_asset": task_input.market.base_asset_id,
            "reference_price": canonical_decimal(assessment.reference_price),
            # The valuation is part of the identity of this assessment: the same
            # ladder priced through a different payment-asset price is a
            # different conclusion and must not resolve to the same evidence.
            "quote_asset_usd_price": canonical_decimal(assessment.quote_asset_usd_price),
            "semantics": assessment.semantics.value,
            "capacity_usd": (
                None
                if assessment.largest_tested_acceptable_notional_usd is None
                else canonical_decimal(assessment.largest_tested_acceptable_notional_usd)
            ),
            "policy_version": assessment.policy_version,
            "ladder": [
                {
                    "notional_usd": canonical_decimal(point.notional_usd),
                    "amount_in_tokens": canonical_decimal(point.amount_in_tokens),
                    "accepted": point.accepted,
                    "amount_out": point.amount_out,
                    "effective_price_usd": (
                        None
                        if point.effective_price_usd is None
                        else canonical_decimal(point.effective_price_usd)
                    ),
                    "deviation_bps": (
                        None
                        if point.execution_deviation_bps is None
                        else canonical_decimal(point.execution_deviation_bps)
                    ),
                    "route_hops": point.route_hops,
                    "venues": list(point.venues),
                    "quoted_at": None if point.quoted_at is None else point.quoted_at.isoformat(),
                    "block": point.source_block_number,
                    "rejection": None if point.rejection is None else point.rejection.value,
                }
                for point in assessment.ladder
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class AnchorWorkerHandler:
    """One claim, one bounded ladder, one typed answer."""

    policy: AnchorExecutionPolicy = ANCHOR_EXECUTION_V1
    quote_provider: str = "unconfigured"

    @property
    def role(self) -> AgentRole:
        return AgentRole.ANCHOR

    @property
    def task_type(self) -> str:
        return ANCHOR_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, AnchorCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.execution_context(
                lease.trade_case_id, lease.task_id
            )
        except AnchorContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.INTERNAL),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, AnchorTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        outcome = assess(task_input, task_input.evaluated_at, self.policy)
        if isinstance(outcome, AnchorReasonCode):
            # No assessment could honestly be made. Never recorded as a fact
            # about the market, because it is not one.
            return TaskFailureReport(
                category=UNASSESSABLE.get(outcome, WorkerFailureCategory.TRANSIENT),
                reason_code=outcome.value,
            )
        return self._evidence(lease, task_input, outcome)

    def _evidence(
        self, lease: TaskLease, task_input: AnchorTaskInput, assessment: ExecutionAssessment
    ) -> EvidenceTaskResult:
        """Build the envelope from runtime facts. Nothing here is a judgement."""
        reference = task_input.reference
        assert reference is not None
        valuation = task_input.quote_asset_valuation
        assert valuation is not None
        digest = execution_digest(task_input, assessment)
        capacity = assessment.largest_tested_acceptable_notional_usd

        # The Phase 2A scalars are left empty rather than filled with the
        # nearest-looking number, and that is the point.
        #
        # `estimated_slippage_bps` means a realisable fill cost in this
        # repository: its sibling on a market snapshot is what the paper
        # executor uses to move a fill price and then records as
        # `realized_slippage_bps`. This assessment has no such figure. It has an
        # execution deviation — a quote's distance from a reference, mixing
        # depth, fees, spread and elapsed time — and writing that into a field
        # named for slippage would put a number nobody measured in front of a
        # reader who would reasonably act on it.
        #
        # `price_impact_bps` means the provider's own impact figure. KyberSwap
        # publishes none on either supported chain, verified live, so the honest
        # value is absent. Substituting the deviation would manufacture provider
        # provenance for a number this system computed itself.
        #
        # Both are None by design. The separated, correctly named figures live
        # in the detail below, which is what anything reasoning about execution
        # reads — and which is why the evidence is still substantive without
        # them.
        accepted = next((point for point in assessment.ladder if point.accepted), None)
        provider_impact = None if accepted is None else accepted.provider_price_impact_bps
        return EvidenceTaskResult(
            submission=EvidenceSubmission(
                # Keyed by the assessment, so the same ladder resubmitted after a
                # lost acknowledgement resolves to one piece of evidence.
                idempotency_key=f"anchor:{digest}",
                producer_role=AgentRole.ANCHOR,
                evidence_type=EvidenceType.LIQUIDITY_EXECUTION,
                provenance=EvidenceProvenance(
                    source=f"anchor:{self.quote_provider}",
                    reference_id=reference.observation_id,
                    source_version=self.policy.version,
                ),
                observed_at=reference.observed_at,
                # Execution conditions age quickly, and the horizon runs from the
                # reference observation rather than from this run.
                #
                # Anchoring it to the run was the same laundering FUSE was
                # corrected for in Phase 2K: re-assessing the same quotes would
                # extend the evidence's life without any of the facts getting
                # newer, so a stale offer could be kept alive indefinitely by
                # simply looking at it again. It also made two assessments of
                # identical quotes produce different submission fingerprints
                # under one idempotency key, which the runtime refuses as a
                # conflict rather than accepting as the replay it is.
                valid_until=reference.observed_at + self.policy.max_reference_age,
                status=(
                    EvidenceStatus.UNKNOWN
                    if assessment.semantics == CapacitySemantics.UNKNOWN
                    else EvidenceStatus.AVAILABLE
                ),
                reason_codes=(assessment.reason_code.value,),
                payload=LiquidityExecutionPayload(
                    setup_evidence_id=task_input.setup_evidence_id,
                    trigger_evidence_id=task_input.trigger_evidence_id,
                    quoted_price=assessment.effective_price_usd_at_capacity,
                    liquidity_usd=reference.liquidity_usd,
                    estimated_slippage_bps=None,
                    price_impact_bps=provider_impact,
                    maximum_safe_size_usd=capacity,
                    routing_provenance=self.quote_provider,
                    execution=ExecutionAssessmentDetail(
                        policy_version=assessment.policy_version,
                        capacity_semantics=assessment.semantics.value,
                        reason_code=assessment.reason_code.value,
                        largest_tested_acceptable_notional_usd=capacity,
                        first_tested_rejected_notional_usd=(
                            assessment.first_tested_rejected_notional_usd
                        ),
                        reference_price=reference.price,
                        reference_price_basis=reference.price_basis,
                        reference_observed_at=reference.observed_at,
                        quote_asset_usd_price=assessment.quote_asset_usd_price,
                        quote_asset_usd_observed_at=valuation.observed_at,
                        quote_asset_usd_provider=valuation.provider,
                        effective_price_usd_at_capacity=(
                            assessment.effective_price_usd_at_capacity
                        ),
                        execution_deviation_bps_at_capacity=(
                            assessment.execution_deviation_bps_at_capacity
                        ),
                        payment_asset_id=task_input.market.quote_asset_id,
                        target_asset_id=task_input.market.base_asset_id,
                        quote_provider=self.quote_provider,
                        quote_requests=assessment.quote_requests,
                        ladder=tuple(
                            QuotedLadderPoint(
                                notional_usd=point.notional_usd,
                                amount_in_tokens=point.amount_in_tokens,
                                accepted=point.accepted,
                                amount_out=point.amount_out,
                                provider_amount_in_usd=point.provider_amount_in_usd,
                                effective_price_usd=point.effective_price_usd,
                                execution_deviation_bps=point.execution_deviation_bps,
                                provider_price_impact_bps=point.provider_price_impact_bps,
                                route_hops=point.route_hops,
                                venues=point.venues,
                                quoted_at=point.quoted_at,
                                source_block_number=point.source_block_number,
                                rejection=(
                                    None if point.rejection is None else point.rejection.value
                                ),
                            )
                            for point in assessment.ladder
                        ),
                        execution_digest=digest,
                    ),
                ),
                correlation_id=lease.correlation_id,
            ),
            result_key=f"anchor:{digest}",
        )
