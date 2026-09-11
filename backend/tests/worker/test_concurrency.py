"""PostgreSQL is authoritative for every guarantee here.

None of these assertions depend on asyncio scheduling or on sleeping: each one is
decided by row locks, unique constraints and transaction ordering.
"""

import asyncio
from datetime import timedelta

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.models import (
    TaskAttemptOutcome,
    TaskFailureReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.workflow.models import EvidenceType, SpecialistTaskStatus
from tests.worker.conftest import ROLE_PAYLOADS, advanced, build_runtime, open_case, submission
from tests.worker.test_worker_runtime import claimed, evidence_result, register


@pytest.fixture(autouse=True)
def require_postgresql(worker_db):
    engine, _ = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking and SKIP LOCKED")


async def test_two_claimers_of_one_task_produce_exactly_one_lease(runtime, now, trace):
    await open_case(runtime.cases, now, trace)
    first = await register(runtime, key="atlas-a")
    second = await register(runtime, key="atlas-b")
    results = await asyncio.gather(
        runtime.claim_next_task(first.worker_instance_id),
        runtime.claim_next_task(second.worker_instance_id),
        return_exceptions=True,
    )
    leases = [item for item in results if item is not None and not isinstance(item, Exception)]
    assert len(leases) == 1
    attempts = await runtime.attempts(task_id=leases[0].task_id)
    assert len(attempts) == 1


async def test_independent_cases_are_claimed_in_parallel(runtime, now, trace):
    await open_case(runtime.cases, now, trace, "case-p1")
    await open_case(runtime.cases, now, trace, "case-p2")
    first = await register(runtime, key="atlas-p1")
    second = await register(runtime, key="atlas-p2")
    results = await asyncio.gather(
        runtime.claim_next_task(first.worker_instance_id),
        runtime.claim_next_task(second.worker_instance_id),
    )
    leases = [item for item in results if item is not None]
    assert len(leases) == 2
    assert leases[0].trade_case_id != leases[1].trade_case_id
    assert leases[0].lease_id != leases[1].lease_id


async def test_heartbeat_and_reclaim_cannot_both_win(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    boundary = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration - timedelta(seconds=1))
    results = await asyncio.gather(
        boundary.renew_lease(lease),
        boundary.recover_expired_leases(),
        return_exceptions=True,
    )
    renew, recover = results
    # The lease has not expired at this instant, so recovery must find nothing and
    # the renewal must stand. There is never a second active lease.
    assert not isinstance(renew, Exception)
    assert recover == ()
    attempts = await boundary.attempts(task_id=lease.task_id)
    assert len([item for item in attempts if item.finished_at is None]) == 1


async def test_expired_lease_reclaim_and_stale_renewal_cannot_both_win(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    later = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    recovered, renewal = await asyncio.gather(
        later.recover_expired_leases(),
        later.renew_lease(lease),
        return_exceptions=True,
    )
    assert isinstance(renewal, WorkerFailure)
    assert renewal.code == WorkerErrorCode.LEASE_EXPIRED
    assert [item.outcome for item in recovered] == [TaskAttemptOutcome.LEASE_EXPIRED]


async def test_two_result_submissions_for_one_lease_insert_one_evidence(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now)
    outcomes = await asyncio.gather(
        runtime.submit_task_result(lease, result),
        runtime.submit_task_result(lease, result),
        return_exceptions=True,
    )
    accepted = [item for item in outcomes if not isinstance(item, Exception)]
    assert accepted
    onchain = [
        item
        for item in await runtime.cases.evidence(lease.trade_case_id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]
    assert len(onchain) == 1
    finished = [item for item in await runtime.attempts(task_id=lease.task_id) if item.finished_at]
    assert len(finished) == 1


async def test_cancellation_racing_result_submission_never_leaves_a_half_state(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now)
    await asyncio.gather(
        runtime.cases.cancel_trade_case(lease.trade_case_id),
        runtime.submit_task_result(lease, result),
        return_exceptions=True,
    )
    evidence = [
        item
        for item in await runtime.cases.evidence(lease.trade_case_id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    # Evidence and task completion are one transaction, so they agree either way.
    assert (tasks[lease.task_id].status == SpecialistTaskStatus.SUCCEEDED) == bool(evidence)


async def test_setup_supersession_racing_a_trigger_result_is_safe(runtime, now, trace):
    trade_case = await open_case(runtime.cases, now, trace, "case-supersede")
    await runtime.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.ATLAS,
            EvidenceType.ONCHAIN,
            ROLE_PAYLOADS[AgentRole.ATLAS][1](),
            key="sup-atlas",
        ),
    )
    await runtime.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.SIGNAL,
            EvidenceType.SENTIMENT,
            ROLE_PAYLOADS[AgentRole.SIGNAL][1](),
            key="sup-signal",
        ),
    )
    setup_a = await runtime.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            ROLE_PAYLOADS[AgentRole.VECTOR][1](),
            key="sup-setup-a",
        ),
    )
    pulse = await register(runtime, AgentRole.PULSE, key="pulse-race")
    lease = await runtime.claim_next_task(pulse.worker_instance_id)
    assert lease is not None

    from src.orchestration.worker.models import EvidenceTaskResult
    from tests.worker.conftest import trigger_payload

    trigger_result = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup_a.evidence_id),
            key="ignored",
        ),
        result_key="trigger-a",
    )
    await asyncio.gather(
        runtime.cases.record_evidence(
            trade_case.id,
            submission(
                trade_case,
                now,
                AgentRole.VECTOR,
                EvidenceType.TRADE_SETUP,
                ROLE_PAYLOADS[AgentRole.VECTOR][1](),
                key="sup-setup-b",
                supersedes_id=setup_a.evidence_id,
            ),
        ),
        runtime.submit_task_result(lease, trigger_result),
        return_exceptions=True,
    )
    triggers = [
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.TRIGGER
    ]
    current_setups = [
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.TRADE_SETUP and item.supersedes_id is not None
    ]
    # A trigger may only exist if it names the setup that is still current.
    if triggers and current_setups:
        assert triggers[0].payload.setup_evidence_id == current_setups[0].evidence_id


async def test_retry_exhaustion_under_concurrent_failure_reports(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    report = TaskFailureReport(
        category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_TIMEOUT"
    )
    outcomes = await asyncio.gather(
        runtime.report_task_failure(lease, report),
        runtime.report_task_failure(lease, report),
        return_exceptions=True,
    )
    assert all(not isinstance(item, Exception) for item in outcomes)
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].attempt == 2


async def test_worker_instance_registration_race_yields_one_identity(runtime):
    registration = WorkerRegistration(
        registration_key="atlas-race",
        role=AgentRole.ATLAS,
        runtime_version="worker-runtime-v1",
    )
    first, second = await asyncio.gather(
        runtime.register_worker(registration),
        runtime.register_worker(registration),
    )
    assert first.worker_instance_id == second.worker_instance_id
    assert len(await runtime.workers(role=AgentRole.ATLAS)) == 1
