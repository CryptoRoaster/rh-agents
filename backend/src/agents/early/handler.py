"""EARLY: the deterministic PRE_VECTOR_EARLY_ENTRY_V1 setup producer.

No model, no provider of its own, no judgement. Given the facts its context
established, it writes exactly one geometry around the fresh reference price P:

* entry zone and trigger: ``PRICE_IN_RANGE`` over ``[0.95 P, 1.05 P]``, so a
  market that moved more than five percent between the decision and the
  trigger check is not chased;
* lifetime: ten minutes from the decision — a watch, never a standing order;
* invalidation: ``0.40 P`` — thesis evidence only; an exit is a separate,
  later contract;
* one informative target: ``2 P``. Recorded for audit. It is not a take-profit
  and nothing may read it as one.

Everything PULSE, ANCHOR and SENTINEL then do is unchanged: they bind to this
setup exactly as they bind to a VECTOR one.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from src.agents.early.context import YOUNG_HISTORY_POLICY
from src.agents.early.models import EarlyTaskInput
from src.agents.early.ports import EarlyContextPending, EarlyContextUnavailable
from src.agents.vector.models import PRICE_BASIS, TriggerType
from src.core.models import AgentRole, Side
from src.core.numbers import canonical_decimal, quantize_down, quantize_up
from src.orchestration.strategy.early import (
    EARLY_ENTRY_V1,
    EARLY_SETUP_KIND,
    EARLY_SETUP_POLICY_VERSION,
    EarlyEntryPolicy,
)
from src.orchestration.worker.capabilities import EarlyCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    TaskWaitReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import (
    EarlyEntryRecord,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    TradeSetupDetail,
    TradeSetupPayload,
    TradeSetupTrigger,
)
from src.orchestration.workflow.policy import EARLY_SETUP_TASK

EARLY_SUMMARY = (
    "PRE_VECTOR_EARLY_ENTRY_V1: deterministic pre-VECTOR setup around the fresh recorded "
    "price; VECTOR history too young by VECTOR's own sufficiency check."
)

# A provider that may answer later is retried; a fault retrying cannot reach is
# internal; a market the strategy does not admit is a permanent refusal with its
# own reason, so the no-entry baseline can say exactly why.
TRANSIENT_CONTEXT = frozenset(
    {
        "MARKET_HISTORY_PROVIDER_RATE_LIMITED",
        "MARKET_HISTORY_PROVIDER_UNAVAILABLE",
        "MARKET_HISTORY_REQUEST_BUDGET_EXHAUSTED",
    }
)
INTERNAL_CONTEXT = frozenset(
    {
        "MARKET_IDENTITY_MISMATCH",
        "MARKET_OBSERVATION_IN_FUTURE",
        "MARKET_HISTORY_PROVIDER_CONTRACT",
        "MARKET_HISTORY_PROVIDER_IDENTITY",
        "MARKET_HISTORY_NETWORK_UNSUPPORTED",
        "MARKET_HISTORY_PROVIDER_REJECTED",
        "EARLY_PRICE_PRECISION_UNSUPPORTED",
    }
)


def failure_category(reason_code: str) -> WorkerFailureCategory:
    if reason_code in TRANSIENT_CONTEXT:
        return WorkerFailureCategory.TRANSIENT
    if reason_code in INTERNAL_CONTEXT:
        return WorkerFailureCategory.INTERNAL
    # Ineligible, unprovable or misconfigured: no retry can change the answer.
    return WorkerFailureCategory.CAPABILITY_DENIED


@dataclass(frozen=True)
class EarlyGeometry:
    entry_low: Decimal
    entry_high: Decimal
    invalidation_price: Decimal
    target: Decimal


def early_geometry(reference: Decimal, policy: EarlyEntryPolicy = EARLY_ENTRY_V1) -> EarlyGeometry:
    """The fixed geometry around P, at the ledger's precision.

    Rounded inward — the zone's floor up, everything else down — so no level is
    ever wider or more generous than the strategy says. A price too small to
    express at eighteen places is refused rather than collapsed onto zero.
    """
    geometry = EarlyGeometry(
        entry_low=quantize_up(reference * (Decimal(1) - policy.zone_fraction)),
        entry_high=quantize_down(reference * (Decimal(1) + policy.zone_fraction)),
        invalidation_price=quantize_down(reference * policy.invalidation_multiple),
        target=quantize_down(reference * policy.target_multiple),
    )
    if (
        geometry.invalidation_price <= 0
        or geometry.entry_low > geometry.entry_high
        or not geometry.invalidation_price < geometry.entry_low
        or not geometry.entry_high < geometry.target
    ):
        raise EarlyContextUnavailable("EARLY_PRICE_PRECISION_UNSUPPORTED")
    return geometry


def _digest(document: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True
        ).encode()
    ).hexdigest()


def early_input_digest(task_input: EarlyTaskInput) -> str:
    """What the producer was shown. Evaluation time is excluded: it says when we looked."""
    document = task_input.model_dump(mode="json", exclude={"task_id", "evaluated_at"})
    document["reference_price"] = canonical_decimal(task_input.reference_price)
    return _digest(document)


def early_setup_fingerprint(
    task_input: EarlyTaskInput, geometry: EarlyGeometry, input_digest: str
) -> str:
    return _digest(
        {
            "market": task_input.pair_id,
            "chain": task_input.chain,
            "network": task_input.network,
            "base_asset_id": task_input.base_asset_id,
            "price_basis": PRICE_BASIS,
            "kind": EARLY_SETUP_KIND,
            "side": Side.BUY.value,
            "entry_low": canonical_decimal(geometry.entry_low),
            "entry_high": canonical_decimal(geometry.entry_high),
            "invalidation_price": canonical_decimal(geometry.invalidation_price),
            "targets": [canonical_decimal(geometry.target)],
            "trigger_type": TriggerType.PRICE_IN_RANGE.value,
            "decision_at": task_input.evaluated_at.isoformat(),
            "policy_version": EARLY_SETUP_POLICY_VERSION,
            "strategy_policy_id": task_input.strategy_policy_id,
            "input_digest": input_digest,
        }
    )


@dataclass(frozen=True)
class EarlyWorkerHandler:
    policy: EarlyEntryPolicy = EARLY_ENTRY_V1

    @property
    def role(self) -> AgentRole:
        return AgentRole.EARLY

    @property
    def task_type(self) -> str:
        return EARLY_SETUP_TASK

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, EarlyCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.early_context(
                lease.trade_case_id, lease.task_id
            )
        except EarlyContextPending as pending:
            return TaskWaitReport(reason_code=pending.reason_code)
        except EarlyContextUnavailable as error:
            return TaskFailureReport(
                category=failure_category(error.reason_code), reason_code=error.reason_code
            )
        if not isinstance(task_input, EarlyTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )
        try:
            geometry = early_geometry(task_input.reference_price, self.policy)
        except EarlyContextUnavailable as error:
            return TaskFailureReport(
                category=failure_category(error.reason_code), reason_code=error.reason_code
            )
        digest = early_input_digest(task_input)
        fingerprint = early_setup_fingerprint(task_input, geometry, digest)
        return EvidenceTaskResult(
            submission=self._submission(lease, task_input, geometry, digest, fingerprint),
            result_key=f"early:{fingerprint}",
        )

    def _submission(
        self,
        lease: TaskLease,
        task_input: EarlyTaskInput,
        geometry: EarlyGeometry,
        digest: str,
        fingerprint: str,
    ) -> EvidenceSubmission:
        decision_at = task_input.evaluated_at
        expires_at = decision_at + self.policy.lifetime
        reason_codes = (EARLY_SETUP_KIND, task_input.vector_sufficiency)
        return EvidenceSubmission(
            idempotency_key=f"early:{task_input.task_id}:{fingerprint}",
            producer_role=AgentRole.EARLY,
            evidence_type=EvidenceType.TRADE_SETUP,
            provenance=EvidenceProvenance(
                source="early:deterministic",
                reference_id=task_input.market_snapshot_id,
                source_version=EARLY_SETUP_POLICY_VERSION,
            ),
            observed_at=task_input.price_observed_at,
            # The envelope stops being current exactly when the setup does.
            valid_until=expires_at,
            status=EvidenceStatus.AVAILABLE,
            payload=TradeSetupPayload(
                setup_id=uuid5(NAMESPACE_URL, f"rh-agents:early:setup:{fingerprint}"),
                side=Side.BUY,
                entry_price=geometry.entry_high,
                invalidation_price=geometry.invalidation_price,
                target_prices=(geometry.target,),
                setup=TradeSetupDetail(
                    setup_fingerprint=fingerprint,
                    policy_version=EARLY_SETUP_POLICY_VERSION,
                    kind=EARLY_SETUP_KIND,
                    price_basis=PRICE_BASIS,
                    entry_low=geometry.entry_low,
                    entry_high=geometry.entry_high,
                    reference_price=task_input.reference_price,
                    expires_at=expires_at,
                    trigger=TradeSetupTrigger(
                        type=TriggerType.PRICE_IN_RANGE.value,
                        price_basis=PRICE_BASIS,
                        zone_low=geometry.entry_low,
                        zone_high=geometry.entry_high,
                        valid_from=decision_at,
                        expires_at=expires_at,
                    ),
                    reason_codes=reason_codes,
                    summary=EARLY_SUMMARY,
                    input_digest=digest,
                    history_provider=task_input.history_provider,
                    history_timeframe=task_input.history_timeframe,
                    history_bar_count=task_input.closed_bars,
                    early_entry=EarlyEntryRecord(
                        strategy_policy_id=task_input.strategy_policy_id,
                        workflow_version=task_input.workflow_version,
                        policy_version=EARLY_SETUP_POLICY_VERSION,
                        vector_sufficiency=task_input.vector_sufficiency,
                        young_history_sufficiency=task_input.young_history_sufficiency,
                        closed_bars=task_input.closed_bars,
                        history_provider=task_input.history_provider,
                        history_timeframe=task_input.history_timeframe,
                        history_aggregate=task_input.history_aggregate,
                        history_requested_bars=task_input.history_requested_bars,
                        history_policy_version=task_input.history_policy_version,
                        creation_block=task_input.creation_block,
                        creation_timestamp=task_input.creation_timestamp,
                        creation_time_source=task_input.creation_time_source,
                        onchain_evidence_id=task_input.onchain_evidence_id,
                        candidate_age_seconds=task_input.candidate_age_seconds,
                        max_age_seconds=task_input.max_age_seconds,
                        market_snapshot_id=task_input.market_snapshot_id,
                        price_observation_id=task_input.price_observation_id,
                        reference_price=task_input.reference_price,
                        decision_at=decision_at,
                    ),
                ),
            ),
            correlation_id=lease.correlation_id,
            supersedes_id=task_input.supersedes_evidence_id,
        )


__all__ = ["YOUNG_HISTORY_POLICY", "EarlyWorkerHandler", "early_geometry"]
