"""ANCHOR is not leased before its case has a current trigger.

`ASSESS_EXECUTION` is marked `after_trigger` in the versioned workflow policy,
and the worker runtime enforces that at claim time with the workflow's own
`untriggered_reason`. Before a trigger there is no work: the task is never a
candidate, no attempt row is written, its attempt counter and schedule do not
move, and no failure budget is spent. Once a current trigger exists the same,
already existing task is claimed as attempt one.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.data.tables import TradeCaseTaskRow, WorkerTaskAttemptRow
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskFailureReport,
    TaskWaitReport,
    WorkerFailureCategory,
    WorkerRegistration,
)
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import (
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1, TRADE_CASE_V2
from src.orchestration.workflow.service import TradeCaseService
from tests.worker.conftest import (
    anchor_payload,
    atlas_payload,
    open_case,
    setup_payload,
    signal_payload,
    submission,
    trigger_payload,
)

ANCHOR_TASK = "ASSESS_EXECUTION"


def runtime_for(sessions, instant, policy=TRADE_CASE_V2):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock, policy=policy)
    return WorkerRuntimeService(sessions, cases, clock=clock)


async def register(runtime, role, key):
    return await runtime.register_worker(
        WorkerRegistration(registration_key=key, role=role, runtime_version="worker-runtime-v1")
    )


async def record(runtime, case, now, role, evidence_type, payload, key, **kw):
    return await runtime.cases.record_evidence(
        case.id, submission(case, now, role, evidence_type, payload, key=key, **kw)
    )


async def before_trigger(runtime, now, trace, key):
    """ORBIT at open, ATLAS, SIGNAL and VECTOR recorded; the setup is current."""
    case = await open_case(runtime.cases, now, trace, key)
    await record(
        runtime, case, now, AgentRole.ATLAS, EvidenceType.ONCHAIN, atlas_payload(), f"{key}-a"
    )
    await record(
        runtime, case, now, AgentRole.SIGNAL, EvidenceType.SENTIMENT, signal_payload(), f"{key}-s"
    )
    setup = await record(
        runtime, case, now, AgentRole.VECTOR, EvidenceType.TRADE_SETUP, setup_payload(), f"{key}-v"
    )
    return case, setup


async def trigger(runtime, case, now, setup_id, key, **kw):
    return await record(
        runtime,
        case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup_id),
        key,
        **kw,
    )


async def anchor_task(sessions, case_id):
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseTaskRow).where(
                TradeCaseTaskRow.trade_case_id == case_id,
                TradeCaseTaskRow.role == AgentRole.ANCHOR.value,
                TradeCaseTaskRow.task_type == ANCHOR_TASK,
            )
        )


async def anchor_attempts(sessions):
    async with sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(WorkerTaskAttemptRow)
            .where(WorkerTaskAttemptRow.role == AgentRole.ANCHOR.value)
        )


def snapshot(task):
    return (
        task.status,
        task.attempt,
        task.lease_id,
        task.next_eligible_at,
        task.reason_code,
        task.failure_category,
        task.started_at,
    )


# ------------------------------------------------------------------ before


@pytest.mark.parametrize("policy", [TRADE_CASE_V1, TRADE_CASE_V2], ids=["v1", "v2"])
async def test_no_anchor_lease_before_a_trigger_however_often_it_asks(
    worker_db, now, trace, policy
):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now, policy)
    case, _ = await before_trigger(runtime, now, trace, f"pre-{policy.version}")
    assert (await runtime.cases.get_trade_case(case.id)).status is TradeCaseStatus.READY_FOR_TRIGGER
    before = snapshot(await anchor_task(sessions, case.id))
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-1")

    for _ in range(25):
        assert await runtime.claim_next_task(anchor.worker_instance_id) is None

    assert snapshot(await anchor_task(sessions, case.id)) == before
    assert before[0] == "PENDING" and before[1] == 1 and before[3] is None
    assert await anchor_attempts(sessions) == 0
    # PULSE, the task whose job it is to find the trigger, is still claimable.
    pulse = await register(runtime, AgentRole.PULSE, "pulse-1")
    assert await runtime.claim_next_task(pulse.worker_instance_id) is not None


async def test_pulse_waits_never_turn_into_anchor_work(worker_db, now, trace):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, _ = await before_trigger(runtime, now, trace, "waits")
    pulse = await register(runtime, AgentRole.PULSE, "pulse-w")
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-w")

    instant = now
    for _ in range(6):
        clocked = runtime_for(sessions, instant)
        lease = await clocked.claim_next_task(pulse.worker_instance_id)
        assert lease is not None
        await clocked.report_task_wait(lease, TaskWaitReport(reason_code="CONDITION_NOT_MET"))
        assert await clocked.claim_next_task(anchor.worker_instance_id) is None
        instant += timedelta(seconds=91)

    assert await anchor_attempts(sessions) == 0
    task = await anchor_task(sessions, case.id)
    assert (task.status, task.attempt, task.next_eligible_at) == ("PENDING", 1, None)


# ------------------------------------------------------------------- after


@pytest.mark.parametrize("policy", [TRADE_CASE_V1, TRADE_CASE_V2], ids=["v1", "v2"])
async def test_a_current_trigger_makes_the_same_task_claimable_as_attempt_one(
    worker_db, now, trace, policy
):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now, policy)
    case, setup = await before_trigger(runtime, now, trace, f"post-{policy.version}")
    waiting = await anchor_task(sessions, case.id)
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-p")
    assert await runtime.claim_next_task(anchor.worker_instance_id) is None

    await trigger(runtime, case, now, setup.evidence_id, "post-trigger")
    lease = await runtime.claim_next_task(anchor.worker_instance_id)

    assert lease is not None
    assert lease.task_id == waiting.task_id, "the task that existed, not a new one"
    assert lease.attempt_number == 1
    async with sessions() as session:
        anchors = await session.scalar(
            select(func.count())
            .select_from(TradeCaseTaskRow)
            .where(
                TradeCaseTaskRow.trade_case_id == case.id,
                TradeCaseTaskRow.role == AgentRole.ANCHOR.value,
            )
        )
    assert anchors == 1


async def test_an_expired_trigger_unlocks_nothing(worker_db, now, trace):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "stale")
    await trigger(
        runtime, case, now, setup.evidence_id, "stale-t", valid_until=now + timedelta(minutes=1)
    )
    later = runtime_for(sessions, now + timedelta(minutes=2))
    anchor = await register(later, AgentRole.ANCHOR, "anchor-s")

    assert await later.claim_next_task(anchor.worker_instance_id) is None
    assert await anchor_attempts(sessions) == 0


@pytest.mark.parametrize("status", [EvidenceStatus.UNKNOWN, EvidenceStatus.INVALID])
async def test_an_unusable_trigger_unlocks_nothing(worker_db, now, trace, status):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, f"unusable-{status.value}")
    await trigger(
        runtime, case, now, setup.evidence_id, f"unusable-t-{status.value}", status=status
    )
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-u")

    assert await runtime.claim_next_task(anchor.worker_instance_id) is None
    assert await anchor_attempts(sessions) == 0


async def test_a_trigger_for_a_replaced_setup_unlocks_nothing(worker_db, now, trace):
    """An old trigger never unlocks a new setup."""
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "replaced")
    await trigger(runtime, case, now, setup.evidence_id, "replaced-t")
    await record(
        runtime,
        case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(),
        "replaced-v2",
        supersedes_id=setup.evidence_id,
    )
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-r")

    assert await runtime.claim_next_task(anchor.worker_instance_id) is None
    assert await anchor_attempts(sessions) == 0


async def test_a_trigger_naming_another_setup_is_refused_and_unlocks_nothing(worker_db, now, trace):
    """The workflow refuses to record it, so there is nothing for a claim to see."""
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, _ = await before_trigger(runtime, now, trace, "foreign")
    with pytest.raises(WorkflowFailure):
        await trigger(runtime, case, now, uuid4(), "foreign-t")
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-f")

    assert await runtime.claim_next_task(anchor.worker_instance_id) is None
    assert await anchor_attempts(sessions) == 0


# ------------------------------------------------------------ starvation, races


async def test_twelve_waiting_cases_do_not_starve_a_triggered_one(worker_db, now, trace):
    """More untriggered tasks than one claim batch, all older than the ready one."""
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    for index in range(12):
        await before_trigger(runtime, now, uuid4(), f"waiting-{index}")
    later = now + timedelta(seconds=1)
    fresh = runtime_for(sessions, later)
    ready, setup = await before_trigger(fresh, later, trace, "ready")
    await trigger(fresh, ready, later, setup.evidence_id, "ready-t")
    anchor = await register(fresh, AgentRole.ANCHOR, "anchor-starve")

    lease = await fresh.claim_next_task(anchor.worker_instance_id)

    assert lease is not None
    assert lease.trade_case_id == ready.id
    assert await anchor_attempts(sessions) == 1


async def trigger_for_a_replaced_setup(runtime, now, trace, key):
    """Setup A, a live trigger for A, then setup B replacing A — and no trigger for B."""
    case, first = await before_trigger(runtime, now, trace, key)
    await trigger(runtime, case, now, first.evidence_id, f"{key}-t")
    await record(
        runtime,
        case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(),
        f"{key}-v2",
        supersedes_id=first.evidence_id,
    )
    return case


@pytest.mark.parametrize("waiting", [12, 26])
async def test_triggers_for_replaced_setups_do_not_starve_a_ready_case(
    worker_db, now, trace, waiting
):
    """More mismatched cases than one claim batch, all older than the ready one.

    Each still holds a live, AVAILABLE, unexpired trigger — only not for its
    current setup. The candidate query must not take them at all.
    """
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    assert runtime.policy.claim_batch == 10
    stuck = [
        await trigger_for_a_replaced_setup(runtime, now, uuid4(), f"stuck-{index}")
        for index in range(waiting)
    ]
    later = now + timedelta(seconds=1)
    fresh = runtime_for(sessions, later)
    ready, setup = await before_trigger(fresh, later, trace, "ready-after-stuck")
    await trigger(fresh, ready, later, setup.evidence_id, "ready-after-stuck-t")
    anchor = await register(fresh, AgentRole.ANCHOR, f"anchor-stuck-{waiting}")

    lease = await fresh.claim_next_task(anchor.worker_instance_id)

    assert lease is not None and lease.trade_case_id == ready.id
    assert lease.attempt_number == 1
    assert await anchor_attempts(sessions) == 1
    for case in stuck:
        task = await anchor_task(sessions, case.id)
        assert (task.status, task.attempt, task.next_eligible_at, task.failure_category) == (
            "PENDING",
            1,
            None,
            None,
        )
    # And, asked again, none of them is ever leased.
    assert await fresh.claim_next_task(anchor.worker_instance_id) is None
    assert await anchor_attempts(sessions) == 1


async def test_two_concurrent_anchor_workers_take_one_lease(worker_db, now, trace):
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("SKIP LOCKED contention needs PostgreSQL")
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "race")
    await trigger(runtime, case, now, setup.evidence_id, "race-t")
    first = await register(runtime, AgentRole.ANCHOR, "anchor-a")
    second = await register(runtime, AgentRole.ANCHOR, "anchor-b")

    leases = await asyncio.gather(
        runtime_for(sessions, now).claim_next_task(first.worker_instance_id),
        runtime_for(sessions, now).claim_next_task(second.worker_instance_id),
    )

    assert len([item for item in leases if item is not None]) == 1
    assert await anchor_attempts(sessions) == 1


async def test_no_claim_while_the_trigger_is_not_committed(worker_db, now, trace):
    """A trigger inside an open transaction is not a trigger yet."""
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("concurrent visibility needs PostgreSQL")
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "uncommitted")
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-c")

    async with sessions.begin() as session:
        await runtime.cases.record_evidence_in_session(
            session,
            case.id,
            submission(
                case,
                now,
                AgentRole.PULSE,
                EvidenceType.TRIGGER,
                trigger_payload(setup.evidence_id),
                key="uncommitted-t",
            ),
        )
        # The trigger's transaction holds the case lock; the claim skips it.
        assert await runtime_for(sessions, now).claim_next_task(anchor.worker_instance_id) is None

    assert await runtime.claim_next_task(anchor.worker_instance_id) is not None


# ---------------------------------------------------- after the trigger: normal


async def test_a_genuine_failure_after_the_trigger_still_spends_the_budget(worker_db, now, trace):
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "genuine")
    await trigger(runtime, case, now, setup.evidence_id, "genuine-t")
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-g")
    lease = await runtime.claim_next_task(anchor.worker_instance_id)
    assert lease is not None and lease.attempt_number == 1

    disposition = await runtime.report_task_failure(
        lease,
        TaskFailureReport(
            category=WorkerFailureCategory.TRANSIENT, reason_code="QUOTE_UNAVAILABLE"
        ),
    )

    assert disposition.outcome is TaskAttemptOutcome.FAILED_RETRYABLE
    assert disposition.retry_scheduled
    task = await anchor_task(sessions, case.id)
    assert task.attempt == 2


async def test_a_completed_assessment_can_still_be_ordered_again(worker_db, now, trace):
    """The claim gate is not in the way of the execution-evidence refresh."""
    _, sessions = worker_db
    runtime = runtime_for(sessions, now)
    case, setup = await before_trigger(runtime, now, trace, "refresh")
    fired = await trigger(
        runtime, case, now, setup.evidence_id, "refresh-t", valid_until=now + timedelta(minutes=50)
    )
    anchor = await register(runtime, AgentRole.ANCHOR, "anchor-x")
    lease = await runtime.claim_next_task(anchor.worker_instance_id)
    assert lease is not None
    await runtime.submit_task_result(
        lease,
        EvidenceTaskResult(
            submission=submission(
                case,
                now,
                AgentRole.ANCHOR,
                EvidenceType.LIQUIDITY_EXECUTION,
                anchor_payload(setup.evidence_id, fired.evidence_id),
                key="refresh-anchor-1",
                valid_until=now + timedelta(minutes=5),
            ),
            result_key="r1",
        ),
    )

    later = runtime_for(sessions, now + timedelta(minutes=6))
    order = await later.cases.refresh_source(case.id, "ANCHOR_EXECUTION_EVIDENCE")
    assert order.outcome.value == "ORDERED", order

    again = await later.claim_next_task(anchor.worker_instance_id)
    assert again is not None and again.task_id == lease.task_id
