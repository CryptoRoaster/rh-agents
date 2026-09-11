"""SIGNAL inside the real workflow and the real Phase 2B runtime.

The question here is not what SIGNAL concludes but what the system does with it,
and the answer is deliberately narrower than for ATLAS. SIGNAL is required and
not safety-critical: an unusable social set leaves a requirement unmet, and a
negative reading does not stop anything. Those are repository facts, pinned here
so a later phase cannot drift into treating a mood as a veto.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.agents.signal.context import SignalContextReader
from src.agents.signal.handler import SIGNAL_TASK_TYPE, SignalWorkerHandler
from src.core.clock import FixedClock
from src.core.models import AgentRole, RiskMetrics, RiskOutcome
from src.orchestration.worker.capabilities import SignalCapabilities
from src.orchestration.worker.models import (
    TaskAttemptOutcome,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import (
    WORKER_RUNTIME_V1,
    authorized_evidence_type,
    authorized_task_type,
)
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.engine import risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from src.orchestration.workflow.service import TradeCaseService
from src.reasoning.fake import DeterministicReasoningProvider
from tests.signal.conftest import campaign_set, market_identity, organic_set, source_for
from tests.signal.test_scenarios import Fixed, NoSubmit, reply
from tests.signal.test_scenarios import lease_for as _lease_for_input


def _lease_for(task_input, instant, trace):
    lease = _lease_for_input(task_input, instant)
    return lease.model_copy(update={"correlation_id": trace})


def build_stack(sessions, instant, observations=(), *, source=None):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = SignalContextReader(
        cases=cases, source=source or source_for(observations), clock=clock
    )
    return runtime, reader


async def open_case(cases, now, trace, key):
    return await cases.open_trade_case(
        market_identity(),
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
    )


async def run_signal(runtime, reader, key, provider=None):
    handler = SignalWorkerHandler(
        provider=provider or DeterministicReasoningProvider.returning(reply())
    )
    runner = WorkerRunner(
        runtime,
        handler,
        CapabilityProvider(service=runtime, sentiment=reader),
        registration_key=key,
    )
    await runner.register()
    return await runner.run_once()


async def sentiment_evidence(cases, trade_case_id):
    return [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type == EvidenceType.SENTIMENT
    ]


# ----------------------------------------------- what the repository says


def test_signal_is_required_and_not_safety_critical():
    """Pinned deliberately. Phase 2F preserves this; it does not decide it."""
    requirement = TRADE_CASE_V1.requirement(EvidenceType.SENTIMENT)
    assert requirement.role == AgentRole.SIGNAL
    assert requirement.required is True
    assert requirement.safety_critical is False
    assert requirement.task_type == SIGNAL_TASK_TYPE
    assert EvidenceType.SENTIMENT not in TRADE_CASE_V1.safety_types


def test_signal_may_submit_sentiment_evidence_and_nothing_else():
    assert authorized_evidence_type(AgentRole.SIGNAL) == EvidenceType.SENTIMENT
    assert authorized_task_type(AgentRole.SIGNAL) == SIGNAL_TASK_TYPE


# -------------------------------------------------------- the happy path


async def test_a_usable_social_set_satisfies_the_requirement(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-ok")

    disposition = await run_signal(runtime, reader, "signal-ok-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.producer_role == AgentRole.SIGNAL
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert envelope.payload.intelligence is not None


async def test_the_case_waits_rather_than_blocking_when_social_data_is_unusable(
    worker_db, now, trace
):
    """Required but not safety-critical: a missing reading pends, never blocks."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, ())
    trade_case = await open_case(runtime.cases, now, trace, "signal-empty")
    await run_signal(runtime, reader, "signal-empty-worker")

    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.UNKNOWN

    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.EVIDENCE_PENDING
    assert any(
        blocker.role == AgentRole.SIGNAL and blocker.evidence_type == EvidenceType.SENTIMENT
        for blocker in updated.blockers
    )


async def test_a_negative_reading_is_accepted_evidence_and_stops_nothing(worker_db, now, trace):
    """Bad sentiment is an observation. Whether it should matter is FUSE's question."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-negative")
    await run_signal(
        runtime,
        reader,
        "signal-negative-worker",
        provider=DeterministicReasoningProvider.returning(
            reply(
                sentiment_direction="NEGATIVE",
                social_demand_indication="NONE",
                narrative_tags=["CRITICISM_OR_WARNING"],
                summary="Several accounts raised concerns about the unlock schedule.",
            )
        ),
    )
    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.payload.assessment == "NEGATIVE"
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED


async def test_a_campaign_produces_accepted_evidence_that_says_it_was_a_campaign(
    worker_db, now, trace
):
    """The workflow is not the place this is judged, and the record must survive."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, campaign_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-campaign")
    await run_signal(
        runtime,
        reader,
        "signal-campaign-worker",
        provider=DeterministicReasoningProvider.returning(
            reply(
                social_demand_indication="NONE",
                narrative_tags=["PROMOTIONAL_CALL_TO_ACTION"],
                summary="One promotional sentence repeated by a handful of accounts.",
            )
        ),
    )
    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    intelligence = envelope.payload.intelligence
    assert intelligence is not None
    assert intelligence.organic_breadth == "VERY_LOW"
    assert intelligence.manipulation_concern == "HIGH"


# ------------------------------------------------------- risk interaction


async def test_sentiment_is_not_part_of_the_risk_snapshot(worker_db, now, trace):
    """SIGNAL is not safety-critical, so it must not move the risk digest.

    Copying ATLAS's supersession semantics here would let a re-read of social
    media revoke a risk authorization, which is not what the workflow says and
    not a property anyone chose.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-digest")
    await run_signal(runtime, reader, "signal-digest-worker")

    evidence = await runtime.cases.evidence(trade_case.id)
    current = {item.evidence_type: item for item in evidence if item.supersedes_id is None}
    with_sentiment = risk_input_digest(trade_case, current)
    without = risk_input_digest(
        trade_case, {k: v for k, v in current.items() if k != EvidenceType.SENTIMENT}
    )
    assert with_sentiment == without


async def test_new_sentiment_supersedes_the_old_reading(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-supersede")
    await run_signal(runtime, reader, "signal-supersede-worker")

    later = now + timedelta(minutes=30)
    later_runtime, later_reader = build_stack(sessions, later, organic_set(later))
    refreshed = await later_reader.sentiment_context(trade_case.id, uuid4())
    assert refreshed.supersedes_evidence_id is not None

    handler = SignalWorkerHandler(
        provider=DeterministicReasoningProvider.returning(reply(sentiment_direction="NEGATIVE"))
    )
    lease = _lease_for(refreshed, later, trace)
    report = await handler.handle(
        lease, SignalCapabilities(lease=lease, context=Fixed(refreshed), submit=NoSubmit())
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)

    envelopes = await sentiment_evidence(later_runtime.cases, trade_case.id)
    assert len(envelopes) == 2
    current = next(item for item in envelopes if item.supersedes_id is not None)
    assert current.payload.assessment == "NEGATIVE"
    superseded = next(item for item in envelopes if item.supersedes_id is None)
    assert current.supersedes_id == superseded.evidence_id


# ----------------------------------------------------- J: the lease expires


async def claim_one(runtime, key):
    registration = await runtime.register_worker(
        WorkerRegistration(
            registration_key=key, role=AgentRole.SIGNAL, runtime_version="worker-runtime-v1"
        )
    )
    return await runtime.claim_next_task(registration.worker_instance_id)


async def produce(reader, lease, now, provider=None):
    """One real SIGNAL run, stopping short of submission."""
    task_input = await reader.sentiment_context(lease.trade_case_id, lease.task_id)
    handler = SignalWorkerHandler(
        provider=provider or DeterministicReasoningProvider.returning(reply())
    )
    return await handler.handle(
        lease, SignalCapabilities(lease=lease, context=Fixed(task_input), submit=NoSubmit())
    )


async def test_scenario_j_a_worker_that_lost_its_lease_cannot_submit(worker_db, now, trace):
    """The model call ran long, the lease expired, and the work arrives too late.

    Nothing about the reading is wrong. Authority over the task is simply no
    longer this worker's, and the runtime — not the worker — is what enforces it.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-lease")
    lease = await claim_one(runtime, "signal-lease-slow")
    assert lease is not None and lease.trade_case_id == trade_case.id

    result = await produce(reader, lease, now)

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    expired = WorkerRuntimeService(sessions, runtime.cases, clock=FixedClock(later))
    with pytest.raises(WorkerFailure) as error:
        await expired.submit_task_result(lease, result)
    assert error.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert await sentiment_evidence(runtime.cases, trade_case.id) == []


async def test_scenario_j_the_replacement_worker_completes_the_task(worker_db, now, trace):
    """And the task is not lost: whoever holds the current lease finishes it."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-lease-retaken")
    await claim_one(runtime, "signal-lease-abandoned")

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    expired = WorkerRuntimeService(sessions, runtime.cases, clock=FixedClock(later))
    await expired.recover_expired_leases()

    # Recovery schedules a bounded retry rather than handing the task straight to
    # the next caller, so the replacement arrives after that delay.
    retry_at = later + WORKER_RUNTIME_V1.retry_initial_delay + timedelta(seconds=1)
    resumed = WorkerRuntimeService(sessions, runtime.cases, clock=FixedClock(retry_at))
    _, later_reader = build_stack(sessions, retry_at, organic_set(retry_at))
    replacement = await claim_one(resumed, "signal-lease-replacement")
    assert replacement is not None

    disposition = await resumed.submit_task_result(
        replacement, await produce(later_reader, replacement, retry_at)
    )
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    assert len(await sentiment_evidence(runtime.cases, trade_case.id)) == 1


async def test_scenario_k_a_replayed_submission_creates_nothing_twice(worker_db, now, trace):
    """The result committed, the acknowledgement was lost, the worker retried."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-replay")
    lease = await claim_one(runtime, "signal-replay-worker")
    assert lease is not None

    result = await produce(reader, lease, now)
    first = await runtime.submit_task_result(lease, result)
    replay = await runtime.submit_task_result(lease, result)

    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert len(await sentiment_evidence(runtime.cases, trade_case.id)) == 1
    tasks = await runtime.cases.tasks(trade_case.id)
    assert len([item for item in tasks if item.role == AgentRole.SIGNAL]) == 1


async def test_scenario_k_a_different_reading_replayed_on_the_same_attempt_is_refused(
    worker_db, now, trace
):
    """Idempotency is on the attempt, so an attempt cannot change its own answer."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    await open_case(runtime.cases, now, trace, "signal-replay-conflict")
    lease = await claim_one(runtime, "signal-replay-conflict-worker")
    assert lease is not None

    result = await produce(reader, lease, now)
    await runtime.submit_task_result(lease, result)
    conflicting = result.model_copy(
        update={
            "submission": result.submission.model_copy(
                update={"reason_codes": ("CHANGED_AFTER_THE_FACT",)}
            )
        }
    )
    with pytest.raises(WorkerFailure) as error:
        await runtime.submit_task_result(lease, conflicting)
    assert error.value.code == WorkerErrorCode.RESULT_CONFLICT


# --------------------------------------------------------------- regression


async def test_the_worker_runtime_policy_is_untouched_by_this_phase():
    assert WORKER_RUNTIME_V1.version == "worker-runtime-v1"
    assert WORKER_RUNTIME_V1.max_attempts == 3


def test_risk_contracts_are_untouched_by_this_phase():
    assert {item.value for item in RiskOutcome} == {"APPROVE", "REJECT", "PAUSE_SYSTEM"}
    assert set(RiskMetrics.model_fields) == {
        "requested_notional_usd",
        "worst_case_notional_usd",
        "exposure_usd",
        "daily_loss_usd",
        "liquidity_usd",
        "estimated_slippage_bps",
    }
