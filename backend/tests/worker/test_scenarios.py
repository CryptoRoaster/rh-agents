"""Phase 2B integration scenarios A-G, driven by deterministic fake workers only.

These handlers exist for tests. No model provider is involved anywhere, and no
handler is registered in any production runtime entrypoint.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.capabilities import AtlasCapabilities, OrbitCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskFailureReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.workflow.models import (
    EvidenceType,
    SpecialistTaskStatus,
    TradeCaseStatus,
)
from tests.worker.conftest import (
    ROLE_PAYLOADS,
    advanced,
    build_runtime,
    open_case,
    submission,
    trigger_payload,
)
from tests.worker.test_worker_runtime import claimed, evidence_result, register


class FakeOnchainSource:
    """Stands in for the assembled ATLAS on-chain context."""

    async def onchain_context(self, trade_case_id, task_id) -> object:
        return {"contract": {"status": "AVAILABLE"}}


@dataclass
class SuccessfulAtlasWorker:
    trade_case: object
    now: object
    role: AgentRole = AgentRole.ATLAS
    task_type: str = "ASSESS_ONCHAIN_INTEGRITY"
    seen_capabilities: object = None
    calls: int = 0
    invoked: asyncio.Event | None = None

    async def handle(self, lease, capabilities):
        self.seen_capabilities = capabilities
        self.calls += 1
        if self.invoked is not None:
            self.invoked.set()
        await capabilities.context.onchain_context(lease.trade_case_id, lease.task_id)
        evidence_type, payload = ROLE_PAYLOADS[AgentRole.ATLAS]
        return EvidenceTaskResult(
            submission=submission(
                self.trade_case, self.now, AgentRole.ATLAS, evidence_type, payload()
            ),
            result_key="atlas-result",
        )


@dataclass
class RetryingAtlasWorker:
    role: AgentRole = AgentRole.ATLAS
    task_type: str = "ASSESS_ONCHAIN_INTEGRITY"
    calls: int = 0

    async def handle(self, lease, capabilities):
        self.calls += 1
        return TaskFailureReport(
            category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_TIMEOUT"
        )


@dataclass
class CrashingAtlasWorker:
    role: AgentRole = AgentRole.ATLAS
    task_type: str = "ASSESS_ONCHAIN_INTEGRITY"

    async def handle(self, lease, capabilities):
        raise RuntimeError("handler exploded")


@dataclass
class WrongEvidenceAtlasWorker:
    trade_case: object
    now: object
    role: AgentRole = AgentRole.ATLAS
    task_type: str = "ASSESS_ONCHAIN_INTEGRITY"

    async def handle(self, lease, capabilities):
        evidence_type, payload = ROLE_PAYLOADS[AgentRole.SIGNAL]
        return EvidenceTaskResult(
            submission=submission(
                self.trade_case, self.now, AgentRole.SIGNAL, evidence_type, payload()
            ),
            result_key="smuggled",
        )


def atlas_provider(runtime):
    return CapabilityProvider(service=runtime, onchain=FakeOnchainSource())


# --------------------------------------------------------------- scenario A


async def test_scenario_a_worker_success_advances_the_trade_case(runtime, now, trace):
    """A: register, claim, handle, submit, evaluate, all committed atomically."""
    trade_case = await open_case(runtime.cases, now, trace, "scenario-a")
    handler = SuccessfulAtlasWorker(trade_case=trade_case, now=now)
    runner = WorkerRunner(
        runtime, handler, atlas_provider(runtime), registration_key="scenario-a-atlas"
    )
    await runner.register()
    disposition = await runner.run_once()
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    assert disposition.retry_scheduled is False

    # The worker received exactly its role's capabilities, with no submission
    # object that could reach another task.
    assert isinstance(handler.seen_capabilities, AtlasCapabilities)
    assert not isinstance(handler.seen_capabilities, OrbitCapabilities)

    evidence = await runtime.cases.evidence(trade_case.id)
    assert [item.evidence_type for item in evidence].count(EvidenceType.ONCHAIN) == 1
    tasks = {item.role: item for item in await runtime.cases.tasks(trade_case.id)}
    assert tasks[AgentRole.ATLAS].status == SpecialistTaskStatus.SUCCEEDED
    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.EVIDENCE_PENDING
    assert not any(blocker.role == AgentRole.ATLAS for blocker in updated.blockers)

    # Nothing left for a second ATLAS worker on this case.
    assert await runner.run_once() is None


# --------------------------------------------------------------- scenario B


async def test_scenario_b_worker_crash_is_recovered_without_manual_intervention(
    worker_db, now, trace
):
    """B: the process disappears, the lease expires, another runtime finishes it."""
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "scenario-b")
    lost = await register(runtime, key="scenario-b-lost")
    lease = await runtime.claim_next_task(lost.worker_instance_id)
    assert lease is not None

    # The worker never returns. Time passes; a sweeping runtime reclaims.
    expiry = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    recovered = await expiry.recover_expired_leases()
    assert [item.outcome for item in recovered] == [TaskAttemptOutcome.LEASE_EXPIRED]

    # The reclaimed slot also honours its retry backoff before anyone may take it.
    later = advanced(
        sessions,
        now,
        WORKER_RUNTIME_V1.lease_duration + WORKER_RUNTIME_V1.retry_delay(1) + timedelta(seconds=2),
    )
    handler = SuccessfulAtlasWorker(trade_case=trade_case, now=later.clock.now())
    runner = WorkerRunner(
        later, handler, atlas_provider(later), registration_key="scenario-b-second"
    )
    await runner.register()
    disposition = await runner.run_once()
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    assert disposition.attempt_number == 2

    onchain = [
        item
        for item in await later.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]
    assert len(onchain) == 1
    attempts = await later.attempts(task_id=lease.task_id)
    assert sorted(item.attempt_number for item in attempts) == [1, 2]
    assert {item.outcome for item in attempts} == {
        TaskAttemptOutcome.LEASE_EXPIRED,
        TaskAttemptOutcome.SUCCEEDED,
    }


# --------------------------------------------------------------- scenario C


async def test_scenario_c_stale_worker_cannot_overwrite_the_current_one(worker_db, now, trace):
    """C: A's lease expires, B reclaims and finishes, A wakes up and is refused."""
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "scenario-c")
    worker_a = await register(runtime, key="scenario-c-a")
    lease_a = await runtime.claim_next_task(worker_a.worker_instance_id)
    assert lease_a is not None

    expiry = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    await expiry.recover_expired_leases()
    later = advanced(
        sessions,
        now,
        WORKER_RUNTIME_V1.lease_duration + WORKER_RUNTIME_V1.retry_delay(1) + timedelta(seconds=2),
    )
    worker_b = await register(later, key="scenario-c-b")
    lease_b = await later.claim_next_task(worker_b.worker_instance_id)
    assert lease_b is not None and lease_b.lease_id != lease_a.lease_id

    accepted = await later.submit_task_result(
        lease_b, evidence_result(trade_case, lease_b, later.clock.now(), result_key="b-result")
    )
    assert accepted.outcome == TaskAttemptOutcome.SUCCEEDED

    # A finally wakes up with a perfectly well-formed payload. Lease ownership,
    # not payload quality, decides.
    with pytest.raises(WorkerFailure) as caught:
        await later.submit_task_result(
            lease_a, evidence_result(trade_case, lease_a, now, result_key="a-result")
        )
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED

    onchain = [
        item
        for item in await later.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]
    assert len(onchain) == 1
    assert onchain[0].idempotency_key.endswith("b-result")


# --------------------------------------------------------------- scenario D


async def test_scenario_d_wrong_capability_is_refused_and_audited(runtime, now, trace):
    """D: a worker reaches for another role's evidence type and is refused."""
    trade_case = await open_case(runtime.cases, now, trace, "scenario-d")
    handler = WrongEvidenceAtlasWorker(trade_case=trade_case, now=now)
    runner = WorkerRunner(
        runtime, handler, atlas_provider(runtime), registration_key="scenario-d-atlas"
    )
    await runner.register()
    disposition = await runner.run_once()
    assert disposition is not None
    # The runner turns a refused capability into a permanent failure, never success.
    assert disposition.outcome == TaskAttemptOutcome.FAILED_PERMANENT
    assert disposition.retry_scheduled is False

    evidence = await runtime.cases.evidence(trade_case.id)
    assert not any(item.evidence_type == EvidenceType.SENTIMENT for item in evidence)
    assert not any(item.evidence_type == EvidenceType.ONCHAIN for item in evidence)
    tasks = {item.role: item for item in await runtime.cases.tasks(trade_case.id)}
    assert tasks[AgentRole.ATLAS].status == SpecialistTaskStatus.FAILED

    timeline = {event.event_type for event in await runtime.cases.timeline(trade_case.id)}
    assert "TASK_FAILED_PERMANENT" in timeline
    attempts = await runtime.attempts(task_id=tasks[AgentRole.ATLAS].task_id)
    assert [item.failure_category for item in attempts] == [WorkerFailureCategory.CAPABILITY_DENIED]


# --------------------------------------------------------------- scenario E


async def test_scenario_e_superseded_setup_invalidates_a_running_pulse_task(runtime, now, trace):
    """E: VECTOR moves on while PULSE is working; the stale trigger is refused."""
    trade_case = await open_case(runtime.cases, now, trace, "scenario-e")
    for role, key in ((AgentRole.ATLAS, "e-atlas"), (AgentRole.SIGNAL, "e-signal")):
        evidence_type, payload = ROLE_PAYLOADS[role]
        await runtime.cases.record_evidence(
            trade_case.id,
            submission(trade_case, now, role, evidence_type, payload(), key=key),
        )
    setup_type, setup_payload = ROLE_PAYLOADS[AgentRole.VECTOR]
    setup_a = await runtime.cases.record_evidence(
        trade_case.id,
        submission(trade_case, now, AgentRole.VECTOR, setup_type, setup_payload(), key="e-setup-a"),
    )

    pulse = await register(runtime, AgentRole.PULSE, key="scenario-e-pulse")
    lease = await runtime.claim_next_task(pulse.worker_instance_id)
    assert lease is not None and lease.role == AgentRole.PULSE

    # VECTOR supersedes the setup the running PULSE task was built on.
    await runtime.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            setup_type,
            setup_payload(),
            key="e-setup-b",
            supersedes_id=setup_a.evidence_id,
        ),
    )

    stale_trigger = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup_a.evidence_id),
            key="ignored",
        ),
        result_key="stale-trigger",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, stale_trigger)
    assert caught.value.code == WorkerErrorCode.TASK_SUPERSEDED

    # The stale trigger never entered history at all, not even to be rejected later.
    assert not any(
        item.evidence_type == EvidenceType.TRIGGER
        for item in await runtime.cases.evidence(trade_case.id)
    )
    assert (
        await runtime.cases.get_trade_case(trade_case.id)
    ).status == TradeCaseStatus.READY_FOR_TRIGGER


# --------------------------------------------------------------- scenario F


async def test_scenario_f_retry_budget_is_exhausted_deterministically(worker_db, now, trace):
    """F: a repeatedly failing worker stops, it does not loop forever."""
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "scenario-f")
    handler = RetryingAtlasWorker()
    elapsed = timedelta(0)
    dispositions = []
    for _ in range(WORKER_RUNTIME_V1.max_attempts + 2):
        current = advanced(sessions, now, elapsed)
        runner = WorkerRunner(
            current, handler, atlas_provider(current), registration_key="scenario-f-atlas"
        )
        await runner.register()
        disposition = await runner.run_once()
        if disposition is None:
            break
        dispositions.append(disposition)
        if disposition.next_eligible_at is None:
            break
        elapsed = disposition.next_eligible_at - now + timedelta(seconds=1)

    assert handler.calls == WORKER_RUNTIME_V1.max_attempts
    assert [item.attempt_number for item in dispositions] == list(
        range(1, WORKER_RUNTIME_V1.max_attempts + 1)
    )
    assert dispositions[-1].reason_code == "TASK_RETRY_EXHAUSTED"
    assert dispositions[-1].retry_scheduled is False

    final = advanced(sessions, now, elapsed + timedelta(minutes=1))
    tasks = {item.role: item for item in await final.cases.tasks(trade_case.id)}
    assert tasks[AgentRole.ATLAS].status == SpecialistTaskStatus.FAILED
    # The slot stays closed; a new worker cannot pick the poison task back up.
    exhausted_runner = WorkerRunner(
        final, handler, atlas_provider(final), registration_key="scenario-f-atlas-2"
    )
    await exhausted_runner.register()
    assert await exhausted_runner.run_once() is None
    assert handler.calls == WORKER_RUNTIME_V1.max_attempts

    blocked = await final.cases.evaluate_trade_case(trade_case.id)
    assert blocked.status == TradeCaseStatus.EVIDENCE_PENDING
    assert any(blocker.role == AgentRole.ATLAS for blocker in blocked.blockers)
    timeline = {event.event_type for event in await final.cases.timeline(trade_case.id)}
    assert "TASK_RETRY_EXHAUSTED" in timeline


# --------------------------------------------------------------- scenario G


async def test_scenario_g_failure_before_commit_leaves_nothing_behind(
    runtime, now, trace, monkeypatch
):
    """G: a crash between evidence insertion and commit must roll both back."""
    _, lease = await claimed(runtime, now, trace, key="scenario-g")
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now, result_key="g-result")

    original = type(runtime)._event
    calls = {"count": 0}

    def exploding(self, session, case_row, event_type, reason_code, payload):
        if event_type == "TASK_RESULT_ACCEPTED":
            calls["count"] += 1
            raise RuntimeError("process died before commit")
        return original(self, session, case_row, event_type, reason_code, payload)

    monkeypatch.setattr(type(runtime), "_event", exploding)
    with pytest.raises(RuntimeError):
        await runtime.submit_task_result(lease, result)
    assert calls["count"] == 1

    monkeypatch.setattr(type(runtime), "_event", original)
    assert not any(
        item.evidence_type == EvidenceType.ONCHAIN
        for item in await runtime.cases.evidence(lease.trade_case_id)
    )
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].status == SpecialistTaskStatus.RUNNING
    attempts = await runtime.attempts(task_id=lease.task_id)
    assert [item.finished_at for item in attempts] == [None]

    # The retry then succeeds, and exactly one authoritative result exists.
    disposition = await runtime.submit_task_result(lease, result)
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    assert disposition.replayed is False
    onchain = [
        item
        for item in await runtime.cases.evidence(lease.trade_case_id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]
    assert len(onchain) == 1


async def test_crashing_handler_never_looks_like_success(runtime, now, trace):
    await open_case(runtime.cases, now, trace, "handler-crash")
    runner = WorkerRunner(
        runtime,
        CrashingAtlasWorker(),
        atlas_provider(runtime),
        registration_key="handler-crash-atlas",
    )
    await runner.register()
    disposition = await runner.run_once()
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.FAILED_RETRYABLE
    assert disposition.reason_code == "HANDLER_ERROR"


@dataclass
class CancellingWorker:
    role: AgentRole = AgentRole.ATLAS
    task_type: str = "ASSESS_ONCHAIN_INTEGRITY"

    async def handle(self, lease, capabilities):
        raise asyncio.CancelledError


async def test_runner_requires_registration_before_working(runtime):
    runner = WorkerRunner(
        runtime,
        CrashingAtlasWorker(),
        atlas_provider(runtime),
        registration_key="unregistered",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runner.run_once()
    assert caught.value.code == WorkerErrorCode.WORKER_NOT_FOUND


async def test_cancellation_writes_no_outcome_and_leaves_the_lease_to_expire(runtime, now, trace):
    await open_case(runtime.cases, now, trace, "cancel-case")
    runner = WorkerRunner(
        runtime, CancellingWorker(), atlas_provider(runtime), registration_key="cancel-atlas"
    )
    await runner.register()
    with pytest.raises(asyncio.CancelledError):
        await runner.run_once()
    attempts = await runtime.attempts()
    # No false outcome on the way out; recovery owns the abandoned lease.
    assert [item.outcome for item in attempts] == [None]


async def test_runner_loop_drains_work_then_stops_promptly(runtime, now, trace):
    trade_case = await open_case(runtime.cases, now, trace, "loop-case")
    handler = SuccessfulAtlasWorker(trade_case=trade_case, now=now)
    runner = WorkerRunner(
        runtime,
        handler,
        atlas_provider(runtime),
        registration_key="loop-atlas",
        poll_interval=timedelta(seconds=60),
    )
    handler.invoked = asyncio.Event()
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))

    # Synchronise on the handler rather than polling the database, so the
    # assertion is decided by the loop's own progress and never by timing.
    await asyncio.wait_for(handler.invoked.wait(), timeout=5)
    stop.set()
    # The loop finishes the iteration it is in, then waits on the stop event
    # instead of sleeping, so shutdown is immediate rather than a poll interval.
    await asyncio.wait_for(task, timeout=5)
    assert task.done() and not task.cancelled()
    assert handler.calls == 1

    evidence = await runtime.cases.evidence(trade_case.id)
    assert [item.evidence_type for item in evidence].count(EvidenceType.ONCHAIN) == 1


async def test_runner_rejects_unreasonable_poll_intervals(runtime):
    for interval in (timedelta(milliseconds=10), timedelta(minutes=10)):
        with pytest.raises(ValueError):
            WorkerRunner(
                runtime,
                CrashingAtlasWorker(),
                atlas_provider(runtime),
                registration_key="bad-interval",
                poll_interval=interval,
            )


async def test_unmappable_submission_refusal_propagates(runtime, now, trace):
    """A worker whose lease is gone has no standing to record anything."""
    from src.orchestration.worker.runner import REFUSAL_CATEGORIES

    assert WorkerErrorCode.LEASE_EXPIRED not in REFUSAL_CATEGORIES
    assert WorkerErrorCode.LEASE_OWNER_MISMATCH not in REFUSAL_CATEGORIES
    assert WorkerErrorCode.TRADE_CASE_NOT_WORKABLE not in REFUSAL_CATEGORIES
