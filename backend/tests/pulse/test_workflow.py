"""PULSE inside the real workflow and the real Phase 2B runtime.

A monitor's correctness is mostly about what happens between checks: a setup is
replaced, a lease expires, an acknowledgement is lost, a hundred cases wait at
once. None of that can be reasoned about from the evaluator alone, so everything
here drives the actual services.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.pulse.context import PulseContextReader
from src.agents.pulse.handler import PULSE_TASK_TYPE, PulseWorkerHandler
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.fake import fixture_snapshot
from src.markets.models import Availability
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskWaitReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1, authorized_task_type
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import (
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from src.orchestration.workflow.service import TradeCaseService
from tests.pulse.conftest import (
    LEVEL,
    PAIR_ID,
    SPOT,
    market_identity,
    setup_payload,
)
from tests.worker.conftest import (
    anchor_payload,
    atlas_payload,
    signal_payload,
    submission,
)

CASE_LIFETIME = timedelta(hours=6)


def snapshot_for(now, *, price=SPOT, seconds_ago: int = 30, pair_id: str = PAIR_ID):
    """A recorded snapshot shaped like the market layer actually stores them.

    Coherent enough to survive the recorder's revalidation: the pair id shares
    the fixture's own chain and network, so this is a snapshot the system would
    accept rather than one that only works against a stub.
    """
    base = fixture_snapshot(now - timedelta(seconds=seconds_ago), uuid4())
    pair = base.pair.model_copy(update={"pair_id": pair_id})
    price_snapshot = base.price.model_copy(
        update={
            "status": Availability.AVAILABLE if price is not None else Availability.UNKNOWN,
            "value_usd": price,
        }
    )
    return base.model_copy(update={"pair": pair, "price": price_snapshot})


from tests.pulse.conftest import StubMarkets  # noqa: E402


def build_stack(
    sessions, instant, *, price=SPOT, markets=None, seconds_ago=30, window=None, policy=None
):
    clock = FixedClock(instant)
    cases = (
        TradeCaseService(sessions, clock=clock)
        if policy is None
        else TradeCaseService(sessions, clock=clock, policy=policy)
    )
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    feed = (
        markets
        if markets is not None
        else StubMarkets(snapshot_for(instant, price=price, seconds_ago=seconds_ago), window=window)
    )
    reader = PulseContextReader(cases=cases, markets=feed, clock=clock, include_fixtures=True)
    return runtime, reader, feed


async def open_case(cases, now, trace, key):
    return await cases.open_trade_case(
        market_identity(),
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + CASE_LIFETIME,
    )


async def record_setup(cases, trade_case, now, *, key="setup", supersedes=None, **kw):
    # Evidence idempotency keys are globally unique, so every key is namespaced
    # by its case. Sharing one across cases would make the second write look like
    # a conflicting replay of the first.
    return await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            setup_payload(now, **kw),
            key=f"{trade_case.id}:{key}",
            supersedes_id=supersedes,
            valid_until=now + timedelta(hours=1),
        ),
    )


async def surround(cases, trade_case, now):
    """The other pre-trigger prerequisites, so the case can reach the trigger stage."""
    for role, evidence_type, payload, name in (
        (AgentRole.ATLAS, EvidenceType.ONCHAIN, atlas_payload(), "chain"),
        (AgentRole.SIGNAL, EvidenceType.SENTIMENT, signal_payload(), "sentiment"),
    ):
        await cases.record_evidence(
            trade_case.id,
            submission(
                trade_case, now, role, evidence_type, payload, key=f"{trade_case.id}:{name}"
            ),
        )


async def run_pulse(runtime, reader, key):
    runner = WorkerRunner(
        runtime,
        PulseWorkerHandler(),
        CapabilityProvider(service=runtime, pulse=reader),
        registration_key=key,
    )
    await runner.register()
    return await runner.run_once()


async def trigger_evidence(cases, trade_case_id):
    return [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type == EvidenceType.TRIGGER
    ]


# --------------------------------------------------- what the repository says


def test_pulse_is_required_and_safety_critical():
    """Audited, not assumed."""
    requirement = TRADE_CASE_V1.requirement(EvidenceType.TRIGGER)
    assert requirement.role == AgentRole.PULSE
    assert requirement.required is True
    assert requirement.safety_critical is True
    # After the trigger, not before it: this is the gate the pre-trigger
    # prerequisites lead to rather than one of them.
    assert requirement.before_trigger is False
    assert requirement.task_type == PULSE_TASK_TYPE
    assert authorized_task_type(AgentRole.PULSE) == PULSE_TASK_TYPE
    # A trigger is a safety input, so it participates in the risk snapshot.
    assert EvidenceType.TRIGGER in TRADE_CASE_V1.safety_types


def test_a_monitor_gets_a_watch_budget_separate_from_its_retry_budget():
    """Derived from the horizon, not a number somebody picked."""
    from src.agents.vector.policy import VECTOR_SETUP_V1

    definition = TRADE_CASE_V1.task(AgentRole.PULSE, PULSE_TASK_TYPE)
    assert definition is not None and definition.wait is not None
    wait = definition.wait
    # The horizon must cover the longest setup it could ever be asked to watch.
    # If VECTOR's maximum lifetime changes, this fails rather than silently
    # leaving a watch that stops early.
    assert wait.horizon >= VECTOR_SETUP_V1.max_setup_lifetime
    assert wait.max_waits == -(
        -int(wait.horizon.total_seconds()) // int(wait.interval.total_seconds())
    )
    assert wait.max_waits >= VECTOR_SETUP_V1.max_setup_lifetime / wait.interval
    # And waiting does not touch the failure allowance.
    assert definition.max_attempts is None


# ------------------------------------------------------ waiting, in the real runtime


async def test_scenario_o_an_unmet_condition_reschedules_without_failing(worker_db, now, trace):
    """The heart of the phase. A watch that found nothing is not an incident."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-wait")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    disposition = await run_pulse(runtime, reader, "pulse-wait-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert disposition.reason_code == "CONDITION_NOT_MET"
    assert disposition.retry_scheduled is True
    assert disposition.next_eligible_at == now + PULSE_TRIGGER_V1.poll_interval

    # No evidence, and no failure anywhere in the record.
    assert await trigger_evidence(runtime.cases, trade_case.id) == []
    attempts = await runtime.attempts(task_id=task_id_of(await runtime.cases.tasks(trade_case.id)))
    assert [item.outcome for item in attempts] == [TaskAttemptOutcome.WAITING]
    assert attempts[0].failure_category is None
    assert attempts[0].reason_code == "CONDITION_NOT_MET"


def task_id_of(tasks):
    return next(item.task_id for item in tasks if item.role == AgentRole.PULSE)


async def test_repeated_waiting_writes_no_evidence_and_no_failures(worker_db, now, trace):
    """Several checks with nothing happening leave a clean, quiet record."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-quiet")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    instant = now
    for index in range(4):
        later_runtime, later_reader, _ = build_stack(sessions, instant, price=Decimal("1.00"))
        disposition = await run_pulse(later_runtime, later_reader, f"pulse-quiet-{index}")
        assert disposition is not None
        assert disposition.outcome == TaskAttemptOutcome.WAITING
        instant += PULSE_TRIGGER_V1.poll_interval

    assert await trigger_evidence(runtime.cases, trade_case.id) == []
    attempts = await runtime.attempts(task_id=task_id_of(await runtime.cases.tasks(trade_case.id)))
    assert len(attempts) == 4
    assert all(item.outcome == TaskAttemptOutcome.WAITING for item in attempts)
    assert all(item.failure_category is None for item in attempts)


async def test_a_task_is_not_claimable_before_its_next_check(worker_db, now, trace):
    """The pause is durable, not a sleep inside a process."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-backoff")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)
    await run_pulse(runtime, reader, "pulse-backoff-a")

    # A moment later there is nothing to claim.
    soon = now + timedelta(seconds=5)
    early_runtime, early_reader, _ = build_stack(sessions, soon, price=Decimal("1.00"))
    assert await run_pulse(early_runtime, early_reader, "pulse-backoff-b") is None

    # After the interval it becomes claimable again.
    due = now + PULSE_TRIGGER_V1.poll_interval
    due_runtime, due_reader, _ = build_stack(sessions, due, price=Decimal("1.00"))
    assert await run_pulse(due_runtime, due_reader, "pulse-backoff-c") is not None


async def test_the_worker_never_sleeps_or_loops_inside_a_lease():
    """One claim performs exactly one check, so many cases cost rows not coroutines."""
    import inspect

    from src.agents.pulse import handler

    source = inspect.getsource(handler.PulseWorkerHandler)
    for forbidden in ("sleep", "while ", "for _ in", "asyncio"):
        assert forbidden not in source


# ------------------------------------------------------------ a crossing


async def test_a_crossing_produces_trigger_evidence_and_advances_the_case(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-cross")
    await surround(runtime.cases, trade_case, now)
    setup = await record_setup(runtime.cases, trade_case, now)

    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_TRIGGER

    disposition = await run_pulse(runtime, reader, "pulse-cross-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    evidence = await trigger_evidence(runtime.cases, trade_case.id)
    assert len(evidence) == 1
    assert evidence[0].status == EvidenceStatus.AVAILABLE
    assert evidence[0].payload.setup_evidence_id == setup.evidence_id

    # The evaluator settles in one transaction: it passes through TRIGGERED and
    # comes to rest waiting for execution evidence. The transition records that
    # the trigger fired; the resting state is what comes next.
    settled = await runtime.cases.get_trade_case(trade_case.id)
    assert settled.status == TradeCaseStatus.EXECUTION_EVIDENCE_PENDING
    timeline = await runtime.cases.timeline(trade_case.id)
    transitions = [
        item.payload.get("to_status") for item in timeline if item.event_type == "STATE_TRANSITION"
    ]
    assert TradeCaseStatus.TRIGGERED.value in transitions


async def test_the_case_then_waits_for_execution_evidence(worker_db, now, trace):
    """The ANCHOR handoff, without implementing ANCHOR."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-handoff")
    await surround(runtime.cases, trade_case, now)
    setup = await record_setup(runtime.cases, trade_case, now)
    await run_pulse(runtime, reader, "pulse-handoff-worker")
    trigger = (await trigger_evidence(runtime.cases, trade_case.id))[0]
    # The case settles at the execution stage in the same transaction, having
    # passed through TRIGGERED. That is the handoff, and ANCHOR is not involved
    # in producing it.
    settled = await runtime.cases.get_trade_case(trade_case.id)
    assert settled.status == TradeCaseStatus.EXECUTION_EVIDENCE_PENDING

    await runtime.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            anchor_payload(setup.evidence_id, trigger.evidence_id),
            key=f"{trade_case.id}:anchor",
        ),
    )
    # And with execution evidence present the case reaches the risk stage, which
    # is SENTINEL's question and not PULSE's.
    assert (
        await runtime.cases.get_trade_case(trade_case.id)
    ).status == TradeCaseStatus.READY_FOR_RISK


async def test_pulse_never_authorizes_anything(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-noauth")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)
    await run_pulse(runtime, reader, "pulse-noauth-worker")

    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status not in {TradeCaseStatus.RISK_APPROVED, TradeCaseStatus.RISK_LIMITED}


# ------------------------------------------------- L, S: the setup race


async def test_scenario_l_a_trigger_for_a_superseded_setup_is_rejected(worker_db, now, trace):
    """Read setup A, VECTOR publishes B, submit for A. The server refuses."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-race")
    await surround(runtime.cases, trade_case, now)
    first = await record_setup(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="pulse-race-worker",
            role=AgentRole.PULSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    capabilities = CapabilityProvider(service=runtime, pulse=reader).build(lease)
    report = await PulseWorkerHandler().handle(lease, capabilities)
    assert isinstance(report, EvidenceTaskResult)
    assert report.submission.payload.setup_evidence_id == first.evidence_id

    # VECTOR replaces the setup while the check was in flight.
    await record_setup(runtime.cases, trade_case, now, key="setup-b", supersedes=first.evidence_id)

    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.TASK_SUPERSEDED
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


async def test_scenario_s_a_scheduled_check_watches_the_new_setup(worker_db, now, trace):
    """An old waiting task wakes to find a different condition, and uses it."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-resched")
    await surround(runtime.cases, trade_case, now)
    first = await record_setup(runtime.cases, trade_case, now)
    await run_pulse(runtime, reader, "pulse-resched-a")

    # A new setup arrives with a lower level, which the current price satisfies.
    later = now + PULSE_TRIGGER_V1.poll_interval
    # A snapshot recorded after the new setup was published, which is what the
    # market watcher would have produced by the time of the next check.
    later_runtime, later_reader, _ = build_stack(
        sessions, later, price=Decimal("1.00"), seconds_ago=0
    )
    await record_setup(
        later_runtime.cases,
        trade_case,
        later,
        key="setup-b",
        supersedes=first.evidence_id,
        trigger={"reference_price": Decimal("0.95"), "valid_from": later},
    )
    disposition = await run_pulse(later_runtime, later_reader, "pulse-resched-b")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    evidence = await trigger_evidence(later_runtime.cases, trade_case.id)
    assert len(evidence) == 1
    # It watched the current condition, not the one it originally woke for.
    assert evidence[0].payload.setup_evidence_id != first.evidence_id
    assert evidence[0].payload.detail.reference_price == Decimal("0.95")


async def test_a_superseded_setup_is_invisible_to_the_monitor(worker_db, now, trace):
    """Selection is the workflow's, so an old setup is not merely unlikely."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-invisible")
    await surround(runtime.cases, trade_case, now)
    first = await record_setup(runtime.cases, trade_case, now)
    await record_setup(
        runtime.cases,
        trade_case,
        now,
        key="setup-b",
        supersedes=first.evidence_id,
        trigger={"reference_price": Decimal("5.00")},
    )
    context = await reader.trigger_context(trade_case.id, uuid4())
    assert context.trigger is not None
    assert context.trigger.setup_evidence_id != first.evidence_id
    assert context.trigger.reference_price == Decimal("5.00")


# ----------------------------------------------------- N: lease fencing


async def test_scenario_n_a_worker_that_lost_its_lease_cannot_submit(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-lease")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="pulse-lease-slow",
            role=AgentRole.PULSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await PulseWorkerHandler().handle(
        lease, CapabilityProvider(service=runtime, pulse=reader).build(lease)
    )

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    later_runtime, _, _ = build_stack(sessions, later, price=LEVEL)
    with pytest.raises(WorkerFailure) as caught:
        await later_runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


async def test_a_worker_that_lost_its_lease_cannot_reschedule_either(worker_db, now, trace):
    """A wait is an authoritative write too, and is fenced the same way."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-lease-wait")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="pulse-lease-wait-worker",
            role=AgentRole.PULSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await PulseWorkerHandler().handle(
        lease, CapabilityProvider(service=runtime, pulse=reader).build(lease)
    )
    assert isinstance(report, TaskWaitReport)

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    later_runtime, _, _ = build_stack(sessions, later)
    with pytest.raises(WorkerFailure) as caught:
        await later_runtime.report_task_wait(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED


# ---------------------------------------------------------- M: replay


async def test_scenario_m_a_replayed_trigger_creates_one_evidence(worker_db, now, trace):
    """The result committed, the acknowledgement was lost, the worker retried."""
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-replay")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="pulse-replay-worker",
            role=AgentRole.PULSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await PulseWorkerHandler().handle(
        lease, CapabilityProvider(service=runtime, pulse=reader).build(lease)
    )
    first = await runtime.submit_task_result(lease, report)
    replay = await runtime.submit_task_result(lease, report)

    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert len(await trigger_evidence(runtime.cases, trade_case.id)) == 1
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1


async def test_a_later_price_that_also_crosses_creates_no_second_trigger(worker_db, now, trace):
    """Scenario: once the setup has fired, it has fired.

    The task reached a terminal success, so no further check is claimable and no
    second authoritative trigger can exist for the same setup.
    """
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-once")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)
    await run_pulse(runtime, reader, "pulse-once-a")
    assert len(await trigger_evidence(runtime.cases, trade_case.id)) == 1

    later = now + timedelta(minutes=5)
    later_runtime, later_reader, _ = build_stack(sessions, later, price=Decimal("1.50"))
    assert await run_pulse(later_runtime, later_reader, "pulse-once-b") is None
    assert len(await trigger_evidence(later_runtime.cases, trade_case.id)) == 1


async def test_pulse_may_only_submit_trigger_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=LEVEL)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-role")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="pulse-role-worker",
            role=AgentRole.PULSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    smuggled = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.SIGNAL,
            EvidenceType.SENTIMENT,
            signal_payload(),
            key="ignored",
        ),
        result_key="smuggled",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, smuggled)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


# ------------------------------------------------------ the watch is bounded


async def test_a_closed_window_ends_the_watch_rather_than_rescheduling_it(worker_db, now, trace):
    """The realistic terminator. A setup that expired cannot become valid again.

    Rechecking it every ninety minutes until the case expires would be work
    queued to fail, so the policy names this reason terminal and the runtime
    stops the watch. The worker reports the fact; what the fact means for the
    task is the server's decision.
    """
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"))
    trade_case = await open_case(runtime.cases, now, trace, "pulse-window-closed")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    # Past the setup's own expiry, still inside the case's.
    after = now + timedelta(hours=2)
    later_runtime, later_reader, _ = build_stack(sessions, after, price=Decimal("1.00"))
    disposition = await run_pulse(later_runtime, later_reader, "pulse-window-closed-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert disposition.reason_code == "TASK_WATCH_ENDED"
    assert disposition.retry_scheduled is False
    assert disposition.next_eligible_at is None

    # Nothing further is claimed, and no trigger was ever produced.
    even_later = build_stack(sessions, after + timedelta(hours=1), price=Decimal("5.00"))
    assert await run_pulse(even_later[0], even_later[1], "pulse-window-closed-after") is None
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


async def test_a_watch_that_runs_out_of_checks_stops(worker_db, now, trace):
    """Bounded like everything else, with a horizon narrowed for the test.

    The shipped horizon affords two hundred checks, so exhausting it honestly
    means driving a policy that affords two. The termination path is the same
    one the real budget would reach.
    """
    from dataclasses import replace

    _, sessions = worker_db
    narrow_wait = replace(
        TRADE_CASE_V1.task(AgentRole.PULSE, PULSE_TASK_TYPE).wait,
        horizon=timedelta(minutes=3),
    )
    narrow = replace(
        TRADE_CASE_V1,
        tasks=tuple(
            replace(item, wait=narrow_wait) if item.role == AgentRole.PULSE else item
            for item in TRADE_CASE_V1.tasks
        ),
    )
    assert narrow_wait.max_waits == 2

    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.00"), policy=narrow)
    trade_case = await open_case(runtime.cases, now, trace, "pulse-exhaust")
    await surround(runtime.cases, trade_case, now)
    await record_setup(runtime.cases, trade_case, now)

    instant, outcomes = now, []
    for index in range(2):
        later = build_stack(sessions, instant, price=Decimal("1.00"), policy=narrow)
        disposition = await run_pulse(later[0], later[1], f"pulse-exhaust-{index}")
        assert disposition is not None
        outcomes.append(disposition.reason_code)
        instant += narrow_wait.interval

    assert outcomes == ["CONDITION_NOT_MET", "TASK_WATCH_EXHAUSTED"]
    after = build_stack(sessions, instant, price=Decimal("1.00"), policy=narrow)
    assert await run_pulse(after[0], after[1], "pulse-exhaust-final") is None
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


# ------------------------------------------- T: many cases at once


async def test_scenario_t_many_cases_are_watched_independently(worker_db, now, trace):
    """Concurrency, and the absence of cross-talk between watches.

    Three cases wait on the same market at different levels. One crosses. The
    other two must be unaffected: no trigger, no failure, and their own
    schedules intact.
    """
    _, sessions = worker_db
    runtime, reader, _ = build_stack(sessions, now, price=Decimal("1.10"))
    levels = {"a": Decimal("1.05"), "b": Decimal("1.50"), "c": Decimal("2.00")}
    cases = {}
    for name, level in levels.items():
        case = await open_case(runtime.cases, now, uuid4(), f"pulse-many-{name}")
        await surround(runtime.cases, case, now)
        await record_setup(
            runtime.cases,
            case,
            now,
            key="setup",
            trigger={"reference_price": level},
        )
        cases[name] = case

    dispositions = []
    for index in range(len(levels)):
        disposition = await run_pulse(runtime, reader, f"pulse-many-worker-{index}")
        assert disposition is not None
        dispositions.append(disposition)

    by_case = {item.trade_case_id: item for item in dispositions}
    assert len(by_case) == 3, "each case must be claimed independently"
    assert by_case[cases["a"].id].outcome == TaskAttemptOutcome.SUCCEEDED
    for name in ("b", "c"):
        assert by_case[cases[name].id].outcome == TaskAttemptOutcome.WAITING

    assert len(await trigger_evidence(runtime.cases, cases["a"].id)) == 1
    for name in ("b", "c"):
        assert await trigger_evidence(runtime.cases, cases[name].id) == []
        assert (
            await runtime.cases.get_trade_case(cases[name].id)
        ).status == TradeCaseStatus.READY_FOR_TRIGGER


# ---------------------------------------------------------- compatibility


def test_a_legacy_trigger_payload_still_parses():
    """Phase 2A evidence predates the detail and must stay readable."""
    from src.orchestration.workflow.models import TriggerPayload

    payload = TriggerPayload.model_validate(
        {
            "kind": "trigger",
            "setup_evidence_id": str(uuid4()),
            "observed_price": "1.20",
            "trigger_code": "ENTRY_LEVEL_REACHED",
        }
    )
    assert payload.detail is None
    assert payload.acceptance().value == "ACCEPTED"
    assert TriggerPayload.model_validate_json(payload.model_dump_json()) == payload


def test_the_other_specialists_are_untouched():
    from src.agents.atlas.prompt import ATLAS_PROMPT_VERSION
    from src.agents.orbit.prompt import ORBIT_PROMPT_VERSION
    from src.agents.signal.prompt import SIGNAL_PROMPT_VERSION
    from src.agents.vector.prompt import VECTOR_PROMPT_VERSION

    assert (
        ORBIT_PROMPT_VERSION,
        ATLAS_PROMPT_VERSION,
        SIGNAL_PROMPT_VERSION,
        VECTOR_PROMPT_VERSION,
    ) == ("orbit-v1", "atlas-v1", "signal-v1", "vector-v2")
