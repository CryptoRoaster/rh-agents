"""The durable wait: a third answer for tasks that watch rather than compute.

Before this, a worker could report success or failure and nothing else. A monitor
needs to say "I did the work and the world is not yet ready", and saying that as
a failure would spend a retry budget on ordinary patience while filling the
attempt history with incidents that never happened.

These tests pin the runtime half of that. The semantics a monitor builds on top
live with PULSE; what is checked here is that a wait is fenced like any other
authoritative write, bounded like any other schedule, and recorded as work done.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.models import (
    TaskAttemptOutcome,
    TaskWaitReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.workflow.models import SpecialistTaskStatus
from tests.worker.conftest import advanced, open_case

WAIT_START = datetime(2026, 9, 13, 12, tzinfo=UTC)
WAIT = TaskWaitReport(reason_code="CONDITION_NOT_MET", retry_after=timedelta(seconds=90))


async def claimed(runtime, now, trace, *, key="wait-worker", role=AgentRole.ATLAS):
    await open_case(runtime.cases, now, trace, key=f"wait-case-{key}")
    worker = await runtime.register_worker(
        WorkerRegistration(registration_key=key, role=role, runtime_version="worker-runtime-v1")
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    return lease


# ------------------------------------------------------ the report itself


def test_a_wait_must_schedule_something_in_the_future():
    with pytest.raises(ValueError):
        TaskWaitReport(reason_code="CONDITION_NOT_MET", retry_after=timedelta(0))
    with pytest.raises(ValueError):
        TaskWaitReport(reason_code="CONDITION_NOT_MET", retry_after=timedelta(seconds=-1))


def test_the_runtime_clamps_what_a_worker_proposes():
    """A worker proposes; the runtime decides.

    Without a floor a worker could poll a provider as fast as it liked; without a
    ceiling a watch could effectively stop without ever saying so.
    """
    policy = WORKER_RUNTIME_V1
    assert policy.wait_interval(timedelta(seconds=1)) == policy.min_wait_interval
    assert policy.wait_interval(timedelta(hours=9)) == policy.max_wait_interval
    assert policy.wait_interval(timedelta(seconds=90)) == timedelta(seconds=90)


def test_a_waiting_attempt_can_never_carry_a_failure_category():
    from src.orchestration.worker.models import TaskAttempt, WorkerFailureCategory

    with pytest.raises(ValueError):
        TaskAttempt(
            attempt_id=uuid4(),
            task_id=uuid4(),
            trade_case_id=uuid4(),
            role=AgentRole.PULSE,
            worker_instance_id=uuid4(),
            lease_id=uuid4(),
            attempt_number=1,
            started_at=WAIT_START,
            lease_expires_at=WAIT_START + timedelta(minutes=1),
            finished_at=WAIT_START,
            outcome=TaskAttemptOutcome.WAITING,
            reason_code="CONDITION_NOT_MET",
            failure_category=WorkerFailureCategory.TRANSIENT,
            runtime_version="worker-runtime-v1",
            correlation_id=uuid4(),
        )


# ------------------------------------------------------- recorded as work


async def test_a_wait_records_work_done_rather_than_an_incident(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_wait(lease, WAIT)

    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert disposition.reason_code == "CONDITION_NOT_MET"
    assert disposition.retry_scheduled is True
    assert disposition.next_eligible_at == now + timedelta(seconds=90)

    attempts = await runtime.attempts(task_id=lease.task_id)
    assert [item.outcome for item in attempts] == [TaskAttemptOutcome.WAITING]
    assert attempts[0].failure_category is None

    tasks = {item.role: item for item in await runtime.cases.tasks(lease.trade_case_id)}
    task = tasks[lease.role]
    assert task.status == SpecialistTaskStatus.PENDING
    assert task.attempt == lease.attempt_number + 1


async def test_a_wait_releases_the_lease_for_the_next_check(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    await runtime.report_task_wait(lease, WAIT)
    # Nothing claimable until the scheduled moment, then claimable again.
    later = advanced(runtime.sessions, now, timedelta(seconds=91))
    worker = await later.register_worker(
        WorkerRegistration(
            registration_key="wait-next", role=lease.role, runtime_version="worker-runtime-v1"
        )
    )
    next_lease = await later.claim_next_task(worker.worker_instance_id)
    assert next_lease is not None
    assert next_lease.task_id == lease.task_id
    assert next_lease.attempt_number == lease.attempt_number + 1
    assert next_lease.lease_id != lease.lease_id


async def test_a_worker_proposing_an_absurd_interval_is_clamped(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_wait(
        lease, TaskWaitReport(reason_code="CONDITION_NOT_MET", retry_after=timedelta(days=2))
    )
    assert disposition.next_eligible_at == now + WORKER_RUNTIME_V1.max_wait_interval


async def test_a_worker_proposing_an_instant_recheck_is_clamped(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_wait(
        lease, TaskWaitReport(reason_code="CONDITION_NOT_MET", retry_after=timedelta(seconds=1))
    )
    assert disposition.next_eligible_at == now + WORKER_RUNTIME_V1.min_wait_interval


# ------------------------------------------------------------- fencing


async def test_a_lost_lease_cannot_reschedule_a_task(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    expired = advanced(
        runtime.sessions, now, WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    )
    with pytest.raises(WorkerFailure) as caught:
        await expired.report_task_wait(lease, WAIT)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED


async def test_an_unknown_lease_cannot_reschedule_anything(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    forged = lease.model_copy(update={"lease_id": uuid4()})
    with pytest.raises(WorkerFailure) as caught:
        await runtime.report_task_wait(forged, WAIT)
    assert caught.value.code == WorkerErrorCode.LEASE_NOT_FOUND


async def test_an_attempt_that_already_answered_cannot_also_wait(runtime, now, trace):
    """Waiting is not a second answer to a question that has been answered."""
    lease = await claimed(runtime, now, trace)
    await runtime.report_task_wait(lease, WAIT)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.report_task_wait(lease, WAIT)
    assert caught.value.code == WorkerErrorCode.RESULT_CONFLICT


async def test_a_wait_for_an_unknown_task_is_refused(runtime, now, trace):
    lease = await claimed(runtime, now, trace)
    stray = lease.model_copy(update={"task_id": uuid4()})
    with pytest.raises(WorkerFailure) as caught:
        await runtime.report_task_wait(stray, WAIT)
    assert caught.value.code == WorkerErrorCode.TASK_NOT_FOUND


# ------------------------------------------------------------- bounded


async def test_a_watch_that_runs_out_of_checks_stops(runtime, now, trace):
    """A system that would watch forever has no way to say it has stopped."""
    trade_case = await open_case(runtime.cases, now, trace, key="wait-bounded")
    tasks = {item.role: item for item in await runtime.cases.tasks(trade_case.id)}
    task_id = tasks[AgentRole.ATLAS].task_id
    async with runtime.sessions.begin() as session:
        from src.data.tables import TradeCaseTaskRow

        row = await session.get(TradeCaseTaskRow, task_id)
        row.max_attempts = 1

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="wait-bounded-worker",
            role=AgentRole.ATLAS,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    disposition = await runtime.report_task_wait(lease, WAIT)
    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert disposition.reason_code == "TASK_WATCH_EXHAUSTED"
    assert disposition.retry_scheduled is False
    assert disposition.next_eligible_at is None

    refreshed = {item.role: item for item in await runtime.cases.tasks(trade_case.id)}
    assert refreshed[AgentRole.ATLAS].status == SpecialistTaskStatus.FAILED
