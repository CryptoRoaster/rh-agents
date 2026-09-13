"""PULSE: the deterministic trigger monitor.

The handler reads its context, evaluates arithmetic, and returns one of three
typed outcomes. It never opens a transaction, never touches task state, never
sets TradeCase status and never builds evidence envelope metadata itself; the
Phase 2B runtime owns the authoritative lifecycle.

**There is no model here, and that is the point.** Every other specialist so far
has had a reasoning provider because every other specialist had to interpret
something. PULSE interprets nothing. It compares a Decimal to a Decimal. A model
in this position would add cost, latency and variance to a question with one
correct answer, and would make it impossible to say afterwards why the system
acted — so this package imports no provider, holds no prompt and makes no paid
call, and a test proves it.

**Not yet is not a failure.** The distinction that shapes this file is between a
condition that has not become true — ordinary operation, for possibly hours — and
something actually going wrong. The first returns a wait that reschedules the
task durably; only the second returns a failure. Collapsing them would fill the
audit trail with incidents that never happened, and would spend a retry budget on
patience.

**The loop lives in the database.** One claim performs exactly one check. There
is no sleeping inside a lease and no polling loop in this process, so a thousand
waiting cases cost a thousand rows rather than a thousand coroutines.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

from src.agents.pulse.evaluator import evaluate
from src.agents.pulse.models import (
    PriceObservation,
    PulseTaskInput,
    TriggerEvaluation,
    TriggerOutcome,
    WatchedTrigger,
)
from src.agents.pulse.policy import PULSE_TRIGGER_V1, PulseTriggerPolicy
from src.agents.pulse.ports import PulseContextUnavailable
from src.core.models import AgentRole
from src.core.numbers import canonical_decimal
from src.orchestration.worker.capabilities import PulseCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskOutcomeReport,
    TaskWaitReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import (
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    TriggerDetail,
    TriggerPayload,
)

PULSE_TASK_TYPE = "WAIT_FOR_TRIGGER"

# Context problems that are faults rather than weather. A case or market layer
# that cannot answer is transient; anything naming the wrong thing is a wiring
# fault retrying cannot reach.
CONTEXT_FAILURES: dict[str, WorkerFailureCategory] = {
    "MARKET_UNAVAILABLE": WorkerFailureCategory.TRANSIENT,
    "TRADE_CASE_UNAVAILABLE": WorkerFailureCategory.TRANSIENT,
    "MARKET_IDENTITY_MISMATCH": WorkerFailureCategory.INTERNAL,
}


def trigger_digest(
    task_input: PulseTaskInput,
    trigger: WatchedTrigger,
    observation: PriceObservation,
    evaluation: TriggerEvaluation,
    policy: PulseTriggerPolicy,
) -> str:
    """Canonical fingerprint of one factual trigger event.

    Covers the case, the setup identity, the condition, the market, the price,
    its unit and the market's own observation time. Deliberately excludes the
    lease, the worker, the attempt, the moment of evaluation and anything about
    how the data was retrieved: the same crossing observed by a different worker
    on a different attempt is the same event, and a digest that disagreed would
    make a replay look like a new fact.
    """
    canonical = json.dumps(
        {
            "trade_case_id": str(task_input.trade_case_id),
            "setup_evidence_id": str(trigger.setup_evidence_id),
            "setup_id": str(trigger.setup_id),
            "setup_fingerprint": trigger.setup_fingerprint,
            "trigger_type": trigger.type.value,
            "reference_price": (
                None
                if trigger.reference_price is None
                else canonical_decimal(trigger.reference_price)
            ),
            "zone": (
                None
                if trigger.zone_low is None or trigger.zone_high is None
                else [canonical_decimal(trigger.zone_low), canonical_decimal(trigger.zone_high)]
            ),
            "price_basis": observation.price_basis,
            "pair_id": observation.pair_id,
            "chain": observation.chain,
            "network": observation.network,
            "venue": observation.venue,
            "provider": observation.provider,
            "observation_id": str(observation.observation_id),
            "observed_price": canonical_decimal(observation.price),
            "observed_at": observation.observed_at.isoformat(),
            "outcome": evaluation.outcome.value,
            "policy_version": policy.version,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class PulseWorkerHandler:
    """One claim, one check, one typed answer."""

    policy: PulseTriggerPolicy = PULSE_TRIGGER_V1

    @property
    def role(self) -> AgentRole:
        return AgentRole.PULSE

    @property
    def task_type(self) -> str:
        return PULSE_TASK_TYPE

    async def handle(self, lease: TaskLease, capabilities: object) -> TaskOutcomeReport:
        if not isinstance(capabilities, PulseCapabilities):
            return TaskFailureReport(
                category=WorkerFailureCategory.CAPABILITY_DENIED,
                reason_code="CAPABILITY_MISMATCH",
            )
        try:
            task_input = await capabilities.context.trigger_context(
                lease.trade_case_id, lease.task_id
            )
        except PulseContextUnavailable as error:
            return TaskFailureReport(
                category=CONTEXT_FAILURES.get(error.reason_code, WorkerFailureCategory.INTERNAL),
                reason_code=error.reason_code,
            )
        if not isinstance(task_input, PulseTaskInput):
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL, reason_code="CONTEXT_SCHEMA_MISMATCH"
            )

        evaluation = evaluate(task_input, task_input.evaluated_at, self.policy)
        if evaluation.outcome == TriggerOutcome.TRIGGERED:
            return self._evidence(lease, task_input, evaluation)
        if evaluation.outcome == TriggerOutcome.OBSERVATION_INVALID:
            # A price that cannot be compared to this condition is a wiring
            # fault. Waiting patiently for it to become comparable would be
            # waiting for something that cannot happen.
            return TaskFailureReport(
                category=WorkerFailureCategory.INTERNAL,
                reason_code=evaluation.reason_code.value,
            )
        if evaluation.outcome == TriggerOutcome.SETUP_EXPIRED:
            # The window closed. Not an error and not a trigger; the watch is
            # over unless a new setup arrives, and the runtime decides that.
            return TaskWaitReport(
                reason_code=evaluation.reason_code.value,
                retry_after=self.policy.poll_interval,
            )
        return TaskWaitReport(
            reason_code=evaluation.reason_code.value,
            retry_after=self._next_check(task_input, evaluation),
        )

    def _next_check(self, task_input: PulseTaskInput, evaluation: TriggerEvaluation) -> timedelta:
        """When to look again, never past the window being watched.

        Scheduling beyond a setup's expiry would queue checks that cannot
        possibly succeed. The runtime clamps this into its own bounds afterwards,
        so a very short remaining window still produces a legal interval.
        """
        interval = self.policy.poll_interval
        trigger = task_input.trigger
        if trigger is None:
            return interval
        remaining = trigger.expires_at - evaluation.evaluated_at
        if timedelta(0) < remaining < interval:
            return remaining
        return interval

    def _evidence(
        self, lease: TaskLease, task_input: PulseTaskInput, evaluation: TriggerEvaluation
    ) -> EvidenceTaskResult:
        """Build the envelope from runtime facts. Nothing here is a judgement."""
        trigger = task_input.trigger
        observation = task_input.observation
        assert trigger is not None and observation is not None
        assert evaluation.observed_price is not None and evaluation.observed_at is not None
        digest = trigger_digest(task_input, trigger, observation, evaluation, self.policy)
        return EvidenceTaskResult(
            submission=EvidenceSubmission(
                # Keyed by the event rather than the attempt, so the same
                # crossing re-submitted after a lost acknowledgement resolves to
                # one trigger instead of two.
                idempotency_key=f"pulse:{digest}",
                producer_role=AgentRole.PULSE,
                evidence_type=EvidenceType.TRIGGER,
                provenance=EvidenceProvenance(
                    source=f"pulse:{observation.provider}",
                    reference_id=observation.observation_id,
                    source_version=self.policy.version,
                ),
                observed_at=observation.observed_at,
                # A trigger event does not go stale the way a measurement does —
                # it records that something happened — but the setup it belongs
                # to does, so the evidence stops being current when the setup it
                # fired for does.
                valid_until=trigger.expires_at,
                status=EvidenceStatus.AVAILABLE,
                payload=TriggerPayload(
                    setup_evidence_id=trigger.setup_evidence_id,
                    observed_price=observation.price,
                    trigger_code=trigger.type.value,
                    detail=TriggerDetail(
                        setup_id=trigger.setup_id,
                        setup_fingerprint=trigger.setup_fingerprint,
                        policy_version=self.policy.version,
                        trigger_type=trigger.type.value,
                        price_basis=observation.price_basis,
                        reference_price=trigger.reference_price,
                        zone_low=trigger.zone_low,
                        zone_high=trigger.zone_high,
                        valid_from=trigger.valid_from,
                        expires_at=trigger.expires_at,
                        observed_at=observation.observed_at,
                        evaluated_at=evaluation.evaluated_at,
                        observation_id=observation.observation_id,
                        snapshot_id=observation.snapshot_id,
                        pair_id=observation.pair_id,
                        chain=observation.chain,
                        network=observation.network,
                        venue=observation.venue,
                        provider=observation.provider,
                        is_fixture=observation.is_fixture,
                        trigger_digest=digest,
                    ),
                ),
                correlation_id=lease.correlation_id,
            ),
            result_key=f"pulse:{digest}",
        )
