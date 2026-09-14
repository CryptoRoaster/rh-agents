from datetime import timedelta
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskFailureReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerInstanceStatus,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.workflow.models import EvidenceType, SpecialistTaskStatus, TradeCaseStatus
from tests.worker.conftest import (
    ROLE_PAYLOADS,
    advanced,
    atlas_payload,
    build_runtime,
    open_case,
    submission,
)


async def register(runtime, role=AgentRole.ATLAS, key="atlas-1"):
    return await runtime.register_worker(
        WorkerRegistration(registration_key=key, role=role, runtime_version="worker-runtime-v1")
    )


async def claimed(runtime, now, trace, role=AgentRole.ATLAS, key="case-claim"):
    await open_case(runtime.cases, now, trace, key)
    worker = await register(runtime, role, key=f"{key}-{role.value}")
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    return worker, lease


def evidence_result(runtime_case, lease, now, role=AgentRole.ATLAS, result_key="r1"):
    evidence_type, payload = ROLE_PAYLOADS[role]
    return EvidenceTaskResult(
        submission=submission(runtime_case, now, role, evidence_type, payload()),
        result_key=result_key,
    )


# ------------------------------------------------------------------- registration


async def test_registration_is_idempotent_and_conflict_is_rejected(runtime):
    first = await register(runtime)
    again = await register(runtime)
    assert again == first
    assert first.status == WorkerInstanceStatus.ACTIVE
    assert first.role == AgentRole.ATLAS
    with pytest.raises(WorkerFailure) as caught:
        await runtime.register_worker(
            WorkerRegistration(
                registration_key="atlas-1",
                role=AgentRole.SIGNAL,
                runtime_version="worker-runtime-v1",
            )
        )
    assert caught.value.code == WorkerErrorCode.WORKER_IDENTITY_CONFLICT


async def test_unknown_and_inactive_workers_cannot_claim(runtime, now, trace):
    await open_case(runtime.cases, now, trace)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.claim_next_task(uuid4())
    assert caught.value.code == WorkerErrorCode.WORKER_NOT_FOUND

    worker = await register(runtime)
    await runtime.set_worker_status(worker.worker_instance_id, WorkerInstanceStatus.STOPPED)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.claim_next_task(worker.worker_instance_id)
    assert caught.value.code == WorkerErrorCode.WORKER_NOT_ACTIVE


@pytest.mark.parametrize("role", [AgentRole.COMMANDER])
async def test_roles_without_evidence_authority_cannot_claim(runtime, now, trace, role):
    await open_case(runtime.cases, now, trace)
    worker = await register(runtime, role, key=f"{role.value}-1")
    with pytest.raises(WorkerFailure) as caught:
        await runtime.claim_next_task(worker.worker_instance_id)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


# -------------------------------------------------------------------------- claim


async def test_claim_establishes_a_bounded_single_lease(runtime, now, trace):
    worker, lease = await claimed(runtime, now, trace)
    assert lease.role == AgentRole.ATLAS
    assert lease.worker_instance_id == worker.worker_instance_id
    assert lease.attempt_number == 1
    assert lease.lease_expires_at == now + WORKER_RUNTIME_V1.lease_duration
    assert lease.is_active_at(now)
    assert not lease.is_active_at(lease.lease_expires_at)

    tasks = {task.task_id: task for task in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].status == SpecialistTaskStatus.RUNNING

    # A second ATLAS worker finds nothing left to take on that case.
    other = await register(runtime, AgentRole.ATLAS, key="atlas-2")
    assert await runtime.claim_next_task(other.worker_instance_id) is None


async def test_worker_only_ever_sees_its_own_role(runtime, now, trace):
    await open_case(runtime.cases, now, trace)
    signal = await register(runtime, AgentRole.SIGNAL, key="signal-1")
    lease = await runtime.claim_next_task(signal.worker_instance_id)
    assert lease is not None and lease.role == AgentRole.SIGNAL
    assert lease.task_type == "ASSESS_SENTIMENT"


async def test_claim_order_is_deterministic_by_creation_then_id(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    first = await open_case(runtime.cases, now, trace, "case-a")
    later = build_runtime(sessions, now + timedelta(minutes=1))
    second = await open_case(later.cases, now + timedelta(minutes=1), trace, "case-b")
    worker = await register(runtime)
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    assert lease.trade_case_id == first.id
    assert second.id != first.id


async def test_terminal_case_and_terminal_task_are_not_claimable(runtime, now, trace):
    trade_case = await open_case(runtime.cases, now, trace)
    await runtime.cases.cancel_trade_case(trade_case.id)
    worker = await register(runtime)
    assert await runtime.claim_next_task(worker.worker_instance_id) is None


async def test_expired_trade_case_task_is_not_claimable(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    await open_case(runtime.cases, now, trace)
    later = advanced(sessions, now, timedelta(hours=2))
    worker = await later.register_worker(
        WorkerRegistration(
            registration_key="atlas-late",
            role=AgentRole.ATLAS,
            runtime_version="worker-runtime-v1",
        )
    )
    assert await later.claim_next_task(worker.worker_instance_id) is None


# -------------------------------------------------------------------------- lease


async def test_heartbeat_extends_only_the_owning_lease(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    later = advanced(sessions, now, timedelta(seconds=30))
    renewed = await later.renew_lease(lease)
    assert renewed.lease_expires_at > lease.lease_expires_at
    assert renewed.lease_id == lease.lease_id

    foreign = await runtime.register_worker(
        WorkerRegistration(
            registration_key="atlas-foreign",
            role=AgentRole.ATLAS,
            runtime_version="worker-runtime-v1",
        )
    )
    with pytest.raises(WorkerFailure) as caught:
        await later.renew_lease(
            lease.model_copy(update={"worker_instance_id": foreign.worker_instance_id})
        )
    assert caught.value.code == WorkerErrorCode.LEASE_OWNER_MISMATCH

    with pytest.raises(WorkerFailure) as caught:
        await later.renew_lease(lease.model_copy(update={"lease_id": uuid4()}))
    assert caught.value.code == WorkerErrorCode.LEASE_NOT_FOUND


async def test_expired_lease_cannot_be_renewed(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    later = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    with pytest.raises(WorkerFailure) as caught:
        await later.renew_lease(lease)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED


async def test_lease_renewal_budget_is_bounded(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    current = lease
    for step in range(WORKER_RUNTIME_V1.max_lease_renewals):
        later = advanced(sessions, now, timedelta(seconds=step + 1))
        current = await later.renew_lease(current)
    final = advanced(sessions, now, timedelta(seconds=WORKER_RUNTIME_V1.max_lease_renewals + 1))
    with pytest.raises(WorkerFailure) as caught:
        await final.renew_lease(current)
    assert caught.value.code == WorkerErrorCode.LEASE_RENEWAL_EXHAUSTED


async def test_reclaim_after_expiry_invalidates_the_previous_lease(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, first = await claimed(runtime, now, trace)
    later = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    recovered = await later.recover_expired_leases()
    assert [item.outcome for item in recovered] == [TaskAttemptOutcome.LEASE_EXPIRED]

    # Retry backoff must elapse before the slot is claimable again.
    second_worker = await later.register_worker(
        WorkerRegistration(
            registration_key="atlas-second",
            role=AgentRole.ATLAS,
            runtime_version="worker-runtime-v1",
        )
    )
    assert await later.claim_next_task(second_worker.worker_instance_id) is None
    eligible = advanced(
        sessions,
        now,
        WORKER_RUNTIME_V1.lease_duration + WORKER_RUNTIME_V1.retry_delay(1) + timedelta(seconds=2),
    )
    second = await eligible.claim_next_task(second_worker.worker_instance_id)
    assert second is not None
    assert second.attempt_number == 2
    assert second.lease_id != first.lease_id

    attempts = await eligible.attempts(task_id=first.task_id)
    assert sorted(item.attempt_number for item in attempts) == [1, 2]
    assert {item.outcome for item in attempts if item.finished_at is not None} == {
        TaskAttemptOutcome.LEASE_EXPIRED
    }


# ------------------------------------------------------------------------ results


async def test_successful_result_records_evidence_and_completes_the_task(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    disposition = await runtime.submit_task_result(lease, evidence_result(trade_case, lease, now))
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    assert disposition.retry_scheduled is False
    assert disposition.replayed is False

    evidence = await runtime.cases.evidence(lease.trade_case_id)
    assert any(item.evidence_type == EvidenceType.ONCHAIN for item in evidence)
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].status == SpecialistTaskStatus.SUCCEEDED
    attempts = await runtime.attempts(task_id=lease.task_id)
    assert [item.outcome for item in attempts] == [TaskAttemptOutcome.SUCCEEDED]


async def test_identical_replay_is_idempotent_and_conflicting_replay_is_rejected(
    runtime, now, trace
):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now)
    first = await runtime.submit_task_result(lease, result)
    replay = await runtime.submit_task_result(lease, result)
    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert len(await runtime.cases.evidence(lease.trade_case_id)) == 2

    conflicting = result.model_copy(
        update={
            "submission": result.submission.model_copy(
                update={"reason_codes": ("CHANGED_AFTER_THE_FACT",)}
            )
        }
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, conflicting)
    assert caught.value.code == WorkerErrorCode.RESULT_CONFLICT


@pytest.mark.parametrize(
    "role,evidence_type,payload_name",
    [
        (AgentRole.SIGNAL, EvidenceType.SENTIMENT, "signal"),
        (AgentRole.VECTOR, EvidenceType.TRADE_SETUP, "vector"),
    ],
)
async def test_wrong_role_evidence_is_rejected(
    runtime, now, trace, role, evidence_type, payload_name
):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    other_type, payload = ROLE_PAYLOADS[role]
    wrong = EvidenceTaskResult(
        submission=submission(trade_case, now, role, other_type, payload()),
        result_key="wrong-role",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, wrong)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].status == SpecialistTaskStatus.RUNNING
    assert not any(
        item.evidence_type == other_type
        for item in await runtime.cases.evidence(lease.trade_case_id)
    )


async def test_right_role_wrong_evidence_type_is_rejected(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    mismatched = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.ATLAS,
            EvidenceType.SENTIMENT,
            ROLE_PAYLOADS[AgentRole.SIGNAL][1](),
        ),
        result_key="wrong-type",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, mismatched)
    assert caught.value.code == WorkerErrorCode.EVIDENCE_TYPE_NOT_AUTHORIZED


async def test_result_for_another_trade_case_is_rejected(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace, key="case-one")
    other = await open_case(runtime.cases, now, trace, "case-two")
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(
            lease.model_copy(update={"trade_case_id": other.id}), result
        )
    assert caught.value.code in {
        WorkerErrorCode.TRADE_CASE_MISMATCH,
        WorkerErrorCode.LEASE_NOT_FOUND,
        WorkerErrorCode.TASK_NOT_FOUND,
    }


async def test_expired_lease_result_is_rejected(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    later = advanced(sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    with pytest.raises(WorkerFailure) as caught:
        await later.submit_task_result(lease, evidence_result(trade_case, lease, now))
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert not any(
        item.evidence_type == EvidenceType.ONCHAIN
        for item in await later.cases.evidence(lease.trade_case_id)
    )


async def test_result_after_case_cancellation_is_rejected(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    result = evidence_result(trade_case, lease, now)
    await runtime.cases.cancel_trade_case(lease.trade_case_id)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, result)
    assert caught.value.code == WorkerErrorCode.TRADE_CASE_NOT_WORKABLE


# -------------------------------------------------------------------------- retry


async def test_retryable_failure_schedules_durable_backoff(worker_db, now, trace):
    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_failure(
        lease,
        TaskFailureReport(category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_TIMEOUT"),
    )
    assert disposition.outcome == TaskAttemptOutcome.FAILED_RETRYABLE
    assert disposition.retry_scheduled is True
    assert disposition.next_eligible_at == now + WORKER_RUNTIME_V1.retry_delay(1)

    worker = await register(runtime, key="atlas-retry")
    assert await runtime.claim_next_task(worker.worker_instance_id) is None
    # Schedule survives rebuilding every service object, so it is durable state.
    eligible = advanced(sessions, now, WORKER_RUNTIME_V1.retry_delay(1) + timedelta(seconds=1))
    again = await eligible.claim_next_task(worker.worker_instance_id)
    assert again is not None and again.attempt_number == 2


async def test_permanent_failure_does_not_retry(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_failure(
        lease,
        TaskFailureReport(
            category=WorkerFailureCategory.CAPABILITY_DENIED, reason_code="CAPABILITY_DENIED"
        ),
    )
    assert disposition.outcome == TaskAttemptOutcome.FAILED_PERMANENT
    assert disposition.retry_scheduled is False
    tasks = {item.task_id: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    assert tasks[lease.task_id].status == SpecialistTaskStatus.FAILED


async def test_invalidated_task_is_superseded_rather_than_treated_as_worker_error(
    runtime, now, trace
):
    _, lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_failure(
        lease,
        TaskFailureReport(
            category=WorkerFailureCategory.TASK_INVALIDATED, reason_code="TASK_INVALIDATED"
        ),
    )
    assert disposition.outcome == TaskAttemptOutcome.SUPERSEDED


async def test_failure_replay_returns_the_recorded_disposition(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    report = TaskFailureReport(
        category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_TIMEOUT"
    )
    first = await runtime.report_task_failure(lease, report)
    replay = await runtime.report_task_failure(lease, report)
    assert replay.replayed is True
    assert replay.attempt_number == first.attempt_number
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1


async def test_case_still_blocks_while_required_evidence_is_missing(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace)
    await runtime.report_task_failure(
        lease,
        TaskFailureReport(
            category=WorkerFailureCategory.CAPABILITY_DENIED, reason_code="CAPABILITY_DENIED"
        ),
    )
    trade_case = await runtime.cases.evaluate_trade_case(lease.trade_case_id)
    assert trade_case.status == TradeCaseStatus.EVIDENCE_PENDING
    assert any(blocker.role == AgentRole.ATLAS for blocker in trade_case.blockers)


def test_atlas_payload_helper_is_available():
    assert atlas_payload().holder_integrity == "PASS"
