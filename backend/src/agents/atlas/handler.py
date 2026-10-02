"""ATLAS: the first safety-critical specialist worker.

The order of operations is the safety property. Facts are collected
deterministically, the versioned policy computes the verdict from those facts
alone, and only then may a model add commentary. Nothing after the policy step
can change the verdict, so an optimistic model, a hostile token label or an
unavailable provider all leave the same conclusion standing.

If the model fails, the deterministic safety state is submitted without
commentary. System safety does not depend on model uptime.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.agents.atlas.context import (
    AtlasContextUnavailable,
    AtlasTaskInput,
    atlas_snapshot_digest,
    snapshot_document,
)
from src.agents.atlas.models import (
    AtlasAssessment,
    AtlasDomain,
    AtlasOnchainSnapshot,
    AtlasReasonCode,
    AtlasSafetyDecision,
    AtlasVerdict,
    HolderFacts,
)
from src.agents.atlas.policy import ATLAS_POLICY_V1, AtlasPolicy, evaluate_snapshot
from src.agents.atlas.prompt import ATLAS_INSTRUCTIONS, ATLAS_PROMPT_HASH, ATLAS_PROMPT_VERSION
from src.agents.atlas.v4.models import PoolControlFacts
from src.agents.atlas.validation import AtlasValidationError, validate_assessment
from src.core.clock import Clock, SystemClock
from src.core.models import AgentRole
from src.markets.models import Availability
from src.orchestration.worker.capabilities import AtlasCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import (
    POOL_CONTROL_LISTED_POSITIONS,
    EconomicHolderShare,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    HolderDistributionFacts,
    OnchainAdvisoryFinding,
    OnchainIntelligence,
    OnchainPayload,
    PoolControlPool,
    PoolControlPosition,
    PoolControlSummary,
    ReconciledExclusion,
)
from src.reasoning.models import ReasoningFailure, ReasoningRequest, ReasoningResult
from src.reasoning.provider import ReasoningProvider

ATLAS_TASK_TYPE = "ASSESS_ONCHAIN_INTEGRITY"

CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "TOKEN_ADDRESS_NOT_EVM": WorkerFailureCategory.INTERNAL,
    "TOKEN_ADDRESS_ZERO": WorkerFailureCategory.INTERNAL,
    "SOURCE_CHAIN_MISMATCH": WorkerFailureCategory.INTERNAL,
}

# Which domain carries each reason when the verdict is written into evidence.
REASON_DOMAIN: dict[AtlasReasonCode, AtlasDomain] = {
    AtlasReasonCode.CHAIN_ID_MISMATCH: AtlasDomain.CONTRACT,
    AtlasReasonCode.CONTRACT_CODE_ABSENT: AtlasDomain.CONTRACT,
    AtlasReasonCode.TOTAL_SUPPLY_ZERO: AtlasDomain.CONTRACT,
    AtlasReasonCode.PROXY_ADMIN_PRESENT: AtlasDomain.CONTRACT,
    AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE: AtlasDomain.CONTRACT,
    AtlasReasonCode.TOTAL_SUPPLY_UNKNOWN: AtlasDomain.CONTRACT,
    AtlasReasonCode.SNAPSHOT_STALE: AtlasDomain.CONTRACT,
    AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED: AtlasDomain.HOLDERS,
    AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE: AtlasDomain.HOLDERS,
    AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED: AtlasDomain.HOLDERS,
    AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED: AtlasDomain.HOLDERS,
    AtlasReasonCode.ORIGIN_FACTS_UNAVAILABLE: AtlasDomain.ORIGIN,
    AtlasReasonCode.ORIGIN_SOURCE_NOT_CONFIGURED: AtlasDomain.ORIGIN,
    # Pool control decides whether a V4 token's holder concentration is known.
    AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE: AtlasDomain.HOLDERS,
    AtlasReasonCode.V4_POSITION_FACTS_INCOMPLETE: AtlasDomain.HOLDERS,
    AtlasReasonCode.V4_POOL_BALANCE_UNATTRIBUTED: AtlasDomain.HOLDERS,
}


def domain_verdicts(decision: AtlasSafetyDecision) -> dict[AtlasDomain, str]:
    """Translate the policy decision into the three Phase 2A domain verdicts.

    These say what policy concluded about each domain, not raw availability: FAIL
    is a measured violation, UNKNOWN is a required domain that could not be
    established, PASS is everything policy is satisfied with. Raw availability is
    preserved separately in the intelligence record.
    """
    verdicts: dict[AtlasDomain, str] = dict.fromkeys(AtlasDomain, "PASS")
    for gap in decision.data_gaps:
        verdicts[REASON_DOMAIN[gap]] = "UNKNOWN"
    for blocker in decision.blockers:
        # A known violation outranks a gap on the same domain: it is the more
        # precise statement about what is wrong.
        verdicts[REASON_DOMAIN[blocker]] = "FAIL"
    return verdicts


def holder_distribution(facts: HolderFacts) -> HolderDistributionFacts | None:
    """Carry the measured distribution onto the evidence, or record nothing.

    Only an available measurement travels. A verdict, a failure code or a
    partially established domain produces no numbers here, because a number
    that was never measured is worse than an absent one: the first is acted on
    and the second is noticed.

    Nothing is recomputed. These are the figures ATLAS already derived from raw
    balances against on-chain supply, moved from a transient snapshot into the
    durable record so a later reader does not have to re-run the collector — or,
    failing that, infer a distribution from `holder_integrity == "PASS"`.
    """
    if facts.status != Availability.AVAILABLE or facts.observed_at is None:
        return None
    if facts.observation_basis is None:
        return None
    fields: dict[str, Any] = {
        "source": facts.source,
        "observed_at": facts.observed_at,
        "observation_basis": facts.observation_basis.value,
        "completeness": facts.completeness.value,
        "snapshot_block": facts.snapshot_block,
        "holder_block_delta": facts.holder_block_delta,
        "holder_count": facts.holder_count,
        "total_supply_raw": (
            None if facts.total_supply_raw is None else str(facts.total_supply_raw)
        ),
        "top_one_fraction": facts.top1_share,
        "top_five_fraction": facts.top5_share,
        "top_ten_fraction": facts.top10_share,
        "top_ten_fraction_excluding_burn": facts.top10_share_excluding_burn,
        "burned_fraction": facts.burned_share,
        "provider_excluded_addresses": facts.excluded_addresses,
    }
    if facts.reconciled_exclusions:
        # Carried only when something was reconciled, so evidence without a
        # reconciliation keeps exactly the shape it always had — and, like
        # legacy evidence, leaves every exclusion unresolved.
        fields["reconciled_exclusions"] = tuple(
            ReconciledExclusion(
                address=item.address,
                balance_raw=str(item.balance_raw),
                block=item.block,
                method=item.method,
            )
            for item in facts.reconciled_exclusions
        )
    return HolderDistributionFacts(**fields)


def pool_control_summary(
    control: PoolControlFacts | None, raw_top_ten: Decimal | None
) -> PoolControlSummary | None:
    """The bounded, durable record of a token's V4 pool control.

    Every pool is listed (the census caps them); positions are listed largest
    controlled amount first up to a fixed count, and the counts beside them
    always cover all of them. No event list is written, only what it proved.
    """
    if control is None:
        return None
    census = control.census
    listed = sorted(
        census.positions,
        key=lambda item: (-item.controlled_token_raw, item.pool_id, item.owner_key, item.salt),
    )[:POOL_CONTROL_LISTED_POSITIONS]

    def raw(value: int | None) -> str | None:
        return None if value is None else str(value)

    return PoolControlSummary(
        status=control.status.value,
        gap=None if control.gap is None else control.gap.value,
        failure=None if census.failure is None else census.failure.value,
        required=control.required,
        source=census.source,
        chain_id=census.chain_id,
        pool_manager=census.pool_manager,
        position_managers=census.position_managers,
        scan_from_block=census.scan_from_block,
        snapshot_block=census.scan_to_block,
        total_supply_raw=raw(control.total_supply_raw),
        pool_manager_balance_raw=raw(census.pool_manager_balance_raw),
        attributed_raw=raw(control.attributed_raw),
        unattributed_raw=raw(control.unattributed_raw),
        creator_controlled_raw=raw(control.creator_controlled_raw),
        pool_held_supply_fraction=control.pool_held_supply_fraction,
        attributable_pool_supply_fraction=control.attributable_pool_supply_fraction,
        creator_controlled_pool_supply_fraction=control.creator_controlled_pool_supply_fraction,
        unattributed_pool_supply_fraction=control.unattributed_pool_supply_fraction,
        pools_total=control.pools_total,
        pools_with_hooks=control.pools_with_hooks,
        creator_controlled_hooks=control.creator_controlled_hooks,
        positions_total=control.positions_total,
        positions_attributed=control.positions_attributed,
        positions_unattributed=control.positions_unattributed,
        pools=tuple(
            PoolControlPool(
                pool_id=pool.pool_id,
                currency0=pool.currency0,
                currency1=pool.currency1,
                fee=pool.fee,
                tick_spacing=pool.tick_spacing,
                hook=pool.hook,
                created_block=pool.created_block,
                created_at=pool.created_at,
                initialized=pool.initialized,
                hook_code_present=None if pool.hook_facts is None else pool.hook_facts.code_present,
                hook_permissions=() if pool.hook_facts is None else pool.hook_facts.permissions,
                hook_owner=None if pool.hook_facts is None else pool.hook_facts.owner,
                hook_owner_status=(
                    None if pool.hook_facts is None else pool.hook_facts.owner_status.value
                ),
                hook_owner_is_creator=(
                    None if pool.hook_facts is None else pool.hook_facts.owner_is_creator
                ),
            )
            for pool in census.pools
        ),
        positions=tuple(
            PoolControlPosition(
                kind=item.kind.value,
                pool_id=item.pool_id,
                owner_key=item.owner_key,
                position_manager=item.position_manager,
                token_id=raw(item.token_id),
                tick_lower=item.tick_lower,
                tick_upper=item.tick_upper,
                liquidity=str(item.liquidity),
                owner=item.owner,
                owner_status=item.owner_status.value,
                controlled_token_raw=str(item.controlled_token_raw),
                owner_is_creator=item.owner_is_creator,
            )
            for item in listed
        ),
        raw_top_ten_fraction=raw_top_ten,
        basis=None if control.basis is None else control.basis.value,
        economic_top_one_fraction=control.economic_top1_share,
        economic_top_five_fraction=control.economic_top5_share,
        economic_top_ten_fraction=control.economic_top10_share,
        economic_top_holders=tuple(
            EconomicHolderShare(
                holder=item.holder, balance_raw=str(item.balance_raw), share=item.share
            )
            for item in control.economic_top_holders
        ),
    )


def onchain_payload(
    snapshot: AtlasOnchainSnapshot,
    decision: AtlasSafetyDecision,
    assessment: AtlasAssessment | None = None,
) -> OnchainPayload:
    """The ONCHAIN payload a deterministic decision implies, commentary optional.

    One derivation for every reader: the worker's evidence and a PAPER exit's
    own fresh read describe a snapshot identically. Nothing here consults a
    model; `assessment` is only carried when one was already validated.
    """
    verdicts = domain_verdicts(decision)
    digest = atlas_snapshot_digest(snapshot)
    return OnchainPayload(
        holder_integrity=verdicts[AtlasDomain.HOLDERS],  # type: ignore[arg-type]
        dev_wallet_integrity=verdicts[AtlasDomain.ORIGIN],  # type: ignore[arg-type]
        contract_integrity=verdicts[AtlasDomain.CONTRACT],  # type: ignore[arg-type]
        intelligence=OnchainIntelligence(
            verdict=decision.verdict.value,
            policy_version=decision.policy_version,
            blockers=tuple(code.value for code in decision.blockers),
            data_gaps=tuple(code.value for code in decision.data_gaps),
            domain_status=decision.domain_status,
            chain_id=snapshot.chain.chain_id,
            block_number=snapshot.chain.block_number,
            snapshot_digest=digest,
            advisory_summary=None if assessment is None else assessment.summary,
            advisory_findings=(
                ()
                if assessment is None
                else tuple(
                    OnchainAdvisoryFinding(
                        kind=finding.kind,
                        code=finding.code,
                        statement=finding.statement,
                        referenced_addresses=finding.referenced_addresses,
                    )
                    for finding in assessment.findings
                )
            ),
            prompt_version=None if assessment is None else ATLAS_PROMPT_VERSION,
            prompt_hash=None if assessment is None else ATLAS_PROMPT_HASH,
            # Always passed, `None` included. An explicitly recorded
            # absence says "this run looked and found nothing", which is
            # a different fact from a row written before the block
            # existed — and the two must not serialise alike.
            holders=holder_distribution(snapshot.holders),
            pool_control=pool_control_summary(snapshot.pool_control, snapshot.holders.top10_share),
        ),
    )


@dataclass(frozen=True)
class AtlasWorkerHandler:
    provider: ReasoningProvider | None = None
    policy: AtlasPolicy = ATLAS_POLICY_V1
    clock: Clock = SystemClock()
    max_output_tokens: int = 1024
    timeout: timedelta = timedelta(seconds=60)

    @property
    def role(self) -> AgentRole:
        return AgentRole.ATLAS

    @property
    def task_type(self) -> str:
        return ATLAS_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, AtlasCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.onchain_context(
                lease.trade_case_id, lease.task_id
            )
        except AtlasContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.TRANSIENT),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, AtlasTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        # The authoritative step. Everything after this is commentary.
        decision = evaluate_snapshot(task_input.snapshot, self.clock.now(), self.policy)
        assessment = await self._commentary(task_input)
        return EvidenceTaskResult(
            submission=self._submission(lease, task_input, decision, assessment),
            result_key=f"atlas:{atlas_snapshot_digest(task_input.snapshot)}",
        )

    async def _commentary(self, task_input: AtlasTaskInput) -> AtlasAssessment | None:
        """Optional advisory analysis. Any failure simply yields no commentary."""
        if self.provider is None:
            return None
        request: ReasoningRequest[AtlasAssessment] = ReasoningRequest(
            instructions=ATLAS_INSTRUCTIONS,
            data={"onchain_facts": snapshot_document(task_input.snapshot)},
            output_model=AtlasAssessment,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout.total_seconds(),
        )
        try:
            result: ReasoningResult[AtlasAssessment] = await self.provider.generate_structured(
                request
            )
        except ReasoningFailure:
            # A safety verdict that already exists is not discarded because an
            # explanation could not be produced.
            return None
        try:
            validate_assessment(result.output, task_input, self.policy)
        except AtlasValidationError:
            return None
        return (
            result.output.model_copy(update={}, deep=False) if result.output is not None else None
        )

    def _submission(
        self,
        lease: TaskLease,
        task_input: AtlasTaskInput,
        decision: AtlasSafetyDecision,
        assessment: AtlasAssessment | None,
    ) -> EvidenceSubmission:
        snapshot = task_input.snapshot
        digest = atlas_snapshot_digest(snapshot)
        # A known violation is an available fact. Only an unestablished domain
        # makes the envelope itself unknown.
        status = (
            EvidenceStatus.UNKNOWN
            if decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
            else EvidenceStatus.AVAILABLE
        )
        reasons = tuple(
            code.value
            for code in (
                decision.blockers
                if decision.verdict == AtlasVerdict.BLOCKED
                else decision.data_gaps
            )
        )
        return EvidenceSubmission(
            idempotency_key=f"atlas:{snapshot.task_id}:{digest}",
            producer_role=AgentRole.ATLAS,
            evidence_type=EvidenceType.ONCHAIN,
            provenance=EvidenceProvenance(
                source=f"atlas:{snapshot.chain.source}",
                reference_id=snapshot.trade_case_id,
                source_version=self.policy.version,
            ),
            observed_at=snapshot.chain.observed_at,
            valid_until=snapshot.collected_at + self.policy.snapshot_validity,
            status=status,
            reason_codes=reasons,
            payload=onchain_payload(snapshot, decision, assessment),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )
