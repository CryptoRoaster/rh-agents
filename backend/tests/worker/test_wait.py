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
WAIT = TaskWaitReport(reason_code="CONDITION_NOT_MET")


async def claimed(runtime, now, trace, *, key="wait-worker", role=AgentRole.PULSE):
    await open_case(runtime.cases, now, trace, key=f"wait-case-{key}")
    worker = await runtime.register_worker(
        WorkerRegistration(registration_key=key, role=role, runtime_version="worker-runtime-v1")
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    return lease


# ------------------------------------------------------ the report itself


def test_a_worker_cannot_name_its_own_cadence():
    """There is no field for it. Scheduling is policy, not a worker's choice."""
    assert "retry_after" not in TaskWaitReport.model_fields
    assert "interval" not in TaskWaitReport.model_fields
    assert "delay" not in TaskWaitReport.model_fields
    assert set(TaskWaitReport.model_fields) == {"kind", "reason_code", "not_after"}


def test_the_only_thing_a_worker_may_say_about_timing_shortens_the_wait():
    """A monitor may say when watching stops being useful. Nothing else.

    The dangerous direction is postponement, and there is no way to express it:
    ``not_after`` can only bring the next check forward.
    """
    report = TaskWaitReport(reason_code="CONDITION_NOT_MET", not_after=WAIT_START)
    assert report.not_after == WAIT_START


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


async def test_the_schedule_comes_from_policy_not_from_the_worker(runtime, now, trace):
    """Whatever the worker reports, the interval is the task's own."""
    from src.core.models import AgentRole as Role
    from src.orchestration.workflow.policy import TRADE_CASE_V1

    lease = await claimed(runtime, now, trace)
    policy = TRADE_CASE_V1.task(Role.PULSE, "WAIT_FOR_TRIGGER").wait
    disposition = await runtime.report_task_wait(lease, WAIT)
    assert disposition.next_eligible_at == now + policy.interval


async def test_a_worker_may_bring_the_next_check_forward_but_never_push_it_out(runtime, now, trace):
    """``not_after`` shortens. There is no way to express postponement."""
    lease = await claimed(runtime, now, trace)
    soon = now + timedelta(seconds=20)
    disposition = await runtime.report_task_wait(
        lease, TaskWaitReport(reason_code="CONDITION_NOT_MET", not_after=soon)
    )
    assert disposition.next_eligible_at == soon


async def test_a_bound_in_the_past_ends_the_watch_rather_than_scheduling_it(runtime, now, trace):
    """Nothing left to watch is not the same as watching again immediately."""
    lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_wait(
        lease,
        TaskWaitReport(reason_code="CONDITION_NOT_MET", not_after=now - timedelta(seconds=1)),
    )
    assert disposition.retry_scheduled is False
    assert disposition.reason_code == "TASK_WATCH_ENDED"


# --------------------------------------------------------- who may wait


async def test_a_role_without_a_wait_policy_cannot_postpone_itself(runtime, now, trace):
    """The dangerous general case: any worker able to wait could stall any task."""
    lease = await claimed(runtime, now, trace, key="atlas-wait", role=AgentRole.ATLAS)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.report_task_wait(lease, WAIT)
    assert caught.value.code == WorkerErrorCode.WAIT_NOT_PERMITTED


@pytest.mark.parametrize(
    "role", [AgentRole.ORBIT, AgentRole.ATLAS, AgentRole.SIGNAL, AgentRole.VECTOR]
)
def test_no_one_shot_specialist_may_wait(role):
    from src.orchestration.workflow.policy import TRADE_CASE_V1

    definition = next(item for item in TRADE_CASE_V1.tasks if item.role == role)
    assert definition.wait is None


async def test_an_invented_reason_is_refused(runtime, now, trace):
    """A worker cannot make up a category to wait under."""
    lease = await claimed(runtime, now, trace)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.report_task_wait(lease, TaskWaitReport(reason_code="I_FEEL_LIKE_WAITING"))
    assert caught.value.code == WorkerErrorCode.WAIT_REASON_NOT_PERMITTED


async def test_a_terminal_reason_ends_the_watch(runtime, now, trace):
    """Which reasons end a watch is policy's decision, not the worker's."""
    lease = await claimed(runtime, now, trace)
    disposition = await runtime.report_task_wait(lease, TaskWaitReport(reason_code="SETUP_EXPIRED"))
    assert disposition.retry_scheduled is False
    assert disposition.reason_code == "TASK_WATCH_ENDED"


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


async def test_waiting_never_spends_the_failure_budget(runtime, now, trace):
    """A monitor that rechecked all day still has its full retry allowance.

    The two budgets are counted from immutable attempt history rather than from
    one shared claim counter, so ordinary patience cannot make the next genuine
    provider outage the last one.
    """
    from src.orchestration.worker.models import TaskFailureReport, WorkerFailureCategory

    lease = await claimed(runtime, now, trace)
    instant = now
    for _ in range(5):
        await runtime.report_task_wait(lease, WAIT)
        instant += timedelta(seconds=91)
        later = advanced(runtime.sessions, instant, timedelta(0))
        worker = await later.register_worker(
            WorkerRegistration(
                registration_key=f"budget-{instant.isoformat()}",
                role=lease.role,
                runtime_version="worker-runtime-v1",
            )
        )
        claimed_lease = await later.claim_next_task(worker.worker_instance_id)
        assert claimed_lease is not None
        lease, runtime = claimed_lease, later

    attempts = await runtime.attempts(task_id=lease.task_id)
    assert sum(1 for item in attempts if item.outcome == TaskAttemptOutcome.WAITING) == 5
    # The full failure budget is still there.
    disposition = await runtime.report_task_failure(
        lease,
        TaskFailureReport(category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_DOWN"),
    )
    assert disposition.retry_scheduled is True
    assert disposition.outcome == TaskAttemptOutcome.FAILED_RETRYABLE


def test_a_watch_horizon_must_cover_the_setup_it_watches():
    """If VECTOR's maximum lifetime changes, this fails rather than watching short."""
    from src.agents.vector.policy import VECTOR_SETUP_V1
    from src.core.models import AgentRole as Role
    from src.orchestration.workflow.policy import TRADE_CASE_V1

    wait = TRADE_CASE_V1.task(Role.PULSE, "WAIT_FOR_TRIGGER").wait
    assert wait.horizon >= VECTOR_SETUP_V1.max_setup_lifetime
    assert wait.max_waits * wait.interval >= VECTOR_SETUP_V1.max_setup_lifetime


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval", timedelta(0)),
        ("horizon", timedelta(seconds=1)),
        ("reasons", frozenset()),
        ("terminal_reasons", frozenset({"NOT_AN_ALLOWED_REASON"})),
    ],
)
def test_an_incoherent_wait_policy_refuses_to_exist(field, value):
    from dataclasses import replace

    from src.orchestration.workflow.policy import WaitPolicy

    base = WaitPolicy(
        interval=timedelta(seconds=90),
        horizon=timedelta(hours=5),
        reasons=frozenset({"CONDITION_NOT_MET"}),
    )
    with pytest.raises(ValueError):
        replace(base, **{field: value})
