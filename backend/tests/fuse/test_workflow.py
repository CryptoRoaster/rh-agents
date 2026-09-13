"""FUSE inside the real workflow and the real Phase 2B runtime.

The questions here are the ones a synthesis layer makes newly possible to get
wrong: whether summarising evidence quietly gives the summary authority, whether
a reading of superseded evidence can be recorded as current, and whether
mentioning SENTIMENT in a synthesis drags SENTIMENT into risk binding.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.agents.fuse.context import FuseContextReader
from src.agents.fuse.handler import FUSE_TASK_TYPE, FuseWorkerHandler
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import FuseCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskLease,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import authorized_task_type
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    SynthesisPayload,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from src.orchestration.workflow.service import TradeCaseService
from tests.fuse.conftest import (
    onchain,
    onchain_envelope_kwargs,
    sentiment,
    trade_setup,
)
from tests.worker.conftest import atlas_payload, submission

pytestmark = pytest.mark.usefixtures("worker_db")


# ---------------------------------------------------------------- harness


def build_stack(sessions, instant):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = FuseContextReader(cases=cases, clock=clock)
    return runtime, reader


async def open_case(cases, now, trace, key):
    from tests.worker.conftest import open_case as _open

    return await _open(cases, now, trace, key)


async def record(cases, trade_case, now, role, evidence_type, payload, *, key, **kw):
    """Record one envelope, honouring the workflow's own rules for its type.

    ATLAS evidence may not be AVAILABLE with an unestablished domain and may not
    report a measured failure without a reason code. Tests follow those rules
    rather than working around them, so nothing here asserts about a state the
    system would refuse to store.
    """
    if evidence_type is EvidenceType.ONCHAIN:
        kw = {**onchain_envelope_kwargs(payload), **kw}
    return await cases.record_evidence(
        trade_case.id,
        submission(trade_case, now, role, evidence_type, payload, key=key, **kw),
    )


async def full_evidence(cases, trade_case, now, **overrides):
    """Every pre-trigger requirement, recorded the way a real case would."""

    await record(
        cases,
        trade_case,
        now,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        overrides.get("onchain", onchain()),
        key="fuse-atlas",
    )
    await record(
        cases,
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        overrides.get("sentiment", sentiment()),
        key="fuse-signal",
    )
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        overrides.get("trade_setup", trade_setup(now)),
        key="fuse-setup",
    )


def lease_for(trade_case, task_id, now, trace):
    return TaskLease(
        lease_id=uuid4(),
        task_id=task_id,
        trade_case_id=trade_case.id,
        role=AgentRole.FUSE,
        task_type=FUSE_TASK_TYPE,
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace,
    )


class Fixed:
    def __init__(self, context) -> None:
        self._context = context

    async def synthesis_context(self, trade_case_id, task_id):
        return self._context


class NoSubmit:
    def __init__(self) -> None:
        self.lease = None

    async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
        raise AssertionError("the handler must not submit directly")


async def synthesize_evidence(sessions, trade_case, now, trace):
    _, reader = build_stack(sessions, now)
    context = await reader.synthesis_context(trade_case.id, uuid4())
    lease = lease_for(trade_case, context.task_id, now, trace)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    assert isinstance(report, EvidenceTaskResult)
    return report


# ------------------------------------------------- the requirement itself


def test_fuse_is_registered_as_optional_non_safety_pre_trigger_evidence():
    requirement = TRADE_CASE_V1.requirement(EvidenceType.SYNTHESIS)
    assert requirement.role == AgentRole.FUSE
    assert requirement.task_type == FUSE_TASK_TYPE
    assert requirement.required is False
    assert requirement.safety_critical is False
    assert requirement.before_trigger is True
    assert authorized_task_type(AgentRole.FUSE) == FUSE_TASK_TYPE


def test_synthesis_is_absent_from_the_risk_snapshot():
    """§43. The one check that keeps SENTIMENT out of risk binding.

    A safety-critical synthesis would hash its own fingerprint into the risk
    digest, and that fingerprint covers SENTIMENT facts — so social data would
    silently invalidate risk authorizations through the summary layer. Phase 2F
    decided SENTIMENT gates the workflow without binding risk, and a summariser
    must not be able to overturn that by summarising.
    """
    assert EvidenceType.SYNTHESIS not in TRADE_CASE_V1.safety_types
    assert TRADE_CASE_V1.safety_types == {
        EvidenceType.ONCHAIN,
        EvidenceType.TRADE_SETUP,
        EvidenceType.TRIGGER,
        EvidenceType.LIQUIDITY_EXECUTION,
    }


# --------------------------------------------- A, B: recorded as evidence


async def test_scenario_a_a_coherent_synthesis_is_recorded_and_accepted(worker_db, now, trace):
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-ok")
    await full_evidence(runtime.cases, trade_case, now)

    report = await synthesize_evidence(sessions, trade_case, now, trace)
    await runtime.cases.record_evidence(trade_case.id, report.submission)

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    stored = current[EvidenceType.SYNTHESIS]
    assert stored.producer_role == AgentRole.FUSE
    assert stored.status == EvidenceStatus.AVAILABLE
    assert stored.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert isinstance(stored.payload, SynthesisPayload)
    assert stored.payload.synthesis is not None
    assert len(stored.payload.source_evidence_ids) == 4


async def test_scenario_b_a_blocked_synthesis_is_recorded_as_available_and_blocking(
    worker_db, now, trace
):
    """Known-bad is a finding, and findings are available evidence.

    Recording it as UNKNOWN would make "we measured this and it is dangerous"
    indistinguishable from "we could not measure this".
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-blocked")
    await full_evidence(runtime.cases, trade_case, now, onchain=onchain(holder="FAIL"))

    report = await synthesize_evidence(sessions, trade_case, now, trace)
    await runtime.cases.record_evidence(trade_case.id, report.submission)

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    stored = current[EvidenceType.SYNTHESIS]
    assert stored.status == EvidenceStatus.AVAILABLE
    assert stored.payload.acceptance() == EvidenceAcceptance.BLOCKED
    assert stored.reason_codes == ("BLOCKED",)


async def test_a_synthesis_gates_nothing_on_its_own(worker_db, now, trace):
    """It is optional and not safety-critical, so the evaluator ignores it.

    The case's own state is decided by the canonical sources, before and after
    the synthesis is recorded. Commentary that could move a case would be
    commentary with authority.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-nogate")
    await full_evidence(runtime.cases, trade_case, now, onchain=onchain(holder="FAIL"))

    before = (await runtime.cases.get_trade_case(trade_case.id)).status
    report = await synthesize_evidence(sessions, trade_case, now, trace)
    await runtime.cases.record_evidence(trade_case.id, report.submission)
    after = (await runtime.cases.get_trade_case(trade_case.id)).status
    assert before == after


# ------------------------------------ O, P: risk transitivity, both ways


async def test_scenario_o_a_sentiment_change_does_not_move_the_risk_digest(worker_db, now, trace):
    """§66. SIGNAL changes; the risk snapshot must not notice.

    A synthesis mentioning SENTIMENT exists throughout, which is exactly the
    condition under which a transitive binding would show itself.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-signal-change")
    await full_evidence(runtime.cases, trade_case, now)
    report = await synthesize_evidence(sessions, trade_case, now, trace)
    await runtime.cases.record_evidence(trade_case.id, report.submission)

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))

    previous = active_evidence(await runtime.cases.evidence(trade_case.id))[EvidenceType.SENTIMENT]
    await record(
        runtime.cases,
        trade_case,
        now + timedelta(minutes=1),
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE", data_quality="DEGRADED"),
        key="fuse-signal-2",
        supersedes_id=previous.evidence_id,
    )
    after = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    assert before == after, "sentiment must not reach the risk snapshot"


async def test_scenario_p_a_safety_evidence_change_does_move_the_risk_digest(worker_db, now, trace):
    """The control for the test above: the mechanism works, it just excludes SIGNAL."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-atlas-change")
    await full_evidence(runtime.cases, trade_case, now)

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    previous = active_evidence(await runtime.cases.evidence(trade_case.id))[EvidenceType.ONCHAIN]
    await record(
        runtime.cases,
        trade_case,
        now + timedelta(minutes=1),
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        onchain(dev="UNKNOWN"),
        key="fuse-atlas-2",
        supersedes_id=previous.evidence_id,
    )
    after = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    assert before != after


async def test_recording_a_synthesis_never_changes_the_risk_digest(worker_db, now, trace):
    """Adding commentary to a case cannot invalidate an authorization."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-digest-stable")
    await full_evidence(runtime.cases, trade_case, now)

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    report = await synthesize_evidence(sessions, trade_case, now, trace)
    await runtime.cases.record_evidence(trade_case.id, report.submission)
    after = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    assert before == after


# ------------------------------------- I: a synthesis of superseded evidence


async def test_scenario_i_a_synthesis_of_a_replaced_setup_is_refused(worker_db, now, trace):
    """§60. The race a synthesis layer makes newly possible.

    FUSE reads setup A, VECTOR replaces it with B, and FUSE's answer arrives
    afterwards. Recording it would leave a current-looking synthesis describing a
    setup nobody is trading any more — which is exactly the sort of stale
    commentary that reads as fresh.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-superseded")
    first = await full_evidence(runtime.cases, trade_case, now)

    context = await reader.synthesis_context(trade_case.id, uuid4())
    assert first.evidence_id in {source.reference.evidence_id for source in context.sources}

    # VECTOR replaces the setup while the synthesis is in flight.
    await record(
        runtime.cases,
        trade_case,
        now + timedelta(minutes=1),
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        trade_setup(now + timedelta(minutes=1)),
        key="fuse-setup-2",
        supersedes_id=first.evidence_id,
    )

    worker, lease = await claim_fuse(runtime, trade_case, now, trace)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    assert isinstance(report, EvidenceTaskResult)
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.TASK_SUPERSEDED


async def test_a_synthesis_names_every_envelope_it_read(worker_db, now, trace):
    """Which is what makes the supersession check above possible at all."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-refs")
    await full_evidence(runtime.cases, trade_case, now)

    report = await synthesize_evidence(sessions, trade_case, now, trace)
    payload = report.submission.payload
    assert isinstance(payload, SynthesisPayload)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    expected = {
        current[evidence_type].evidence_id
        for evidence_type in (
            EvidenceType.DISCOVERY,
            EvidenceType.ONCHAIN,
            EvidenceType.SENTIMENT,
            EvidenceType.TRADE_SETUP,
        )
    }
    assert set(payload.source_evidence_ids) == expected
    assert payload.synthesis is not None
    stored = {source.evidence_id for source in payload.synthesis.sources}
    assert stored == expected


async def claim_fuse(runtime, trade_case, now, trace):
    """Register a FUSE worker and claim its synthesis task from the real runtime."""
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key=f"fuse-worker-{trade_case.id}",
            role=AgentRole.FUSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    assert lease.role == AgentRole.FUSE
    assert lease.task_type == FUSE_TASK_TYPE
    return worker, lease


# ---------------------------------------------- Q, R: runtime fencing


async def test_scenario_q_a_worker_that_lost_its_lease_cannot_submit(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-lease")
    await full_evidence(runtime.cases, trade_case, now)
    _, lease = await claim_fuse(runtime, trade_case, now, trace)

    context = await reader.synthesis_context(trade_case.id, lease.task_id)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    assert isinstance(report, EvidenceTaskResult)

    late_runtime, _ = build_stack(sessions, now + timedelta(hours=2))
    with pytest.raises(WorkerFailure) as caught:
        await late_runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED


async def test_scenario_r_the_same_synthesis_replayed_yields_one_evidence(worker_db, now, trace):
    """Determinism makes this free: the same evidence gives the same fingerprint."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-replay")
    await full_evidence(runtime.cases, trade_case, now)
    _, lease = await claim_fuse(runtime, trade_case, now, trace)

    context = await reader.synthesis_context(trade_case.id, lease.task_id)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    first = await runtime.submit_task_result(lease, report)
    second = await runtime.submit_task_result(lease, report)

    assert first.outcome == TaskAttemptOutcome.SUCCEEDED
    assert second.replayed is True
    stored = [
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.SYNTHESIS
    ]
    assert len(stored) == 1


async def test_two_workers_reading_the_same_evidence_agree_exactly(worker_db, now, trace):
    """No model, so no second valid answer to reconcile."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-agree")
    await full_evidence(runtime.cases, trade_case, now)

    first = await synthesize_evidence(sessions, trade_case, now, trace)
    second = await synthesize_evidence(sessions, trade_case, now, trace)
    assert first.submission.idempotency_key == second.submission.idempotency_key
    assert first.submission.fingerprint() == second.submission.fingerprint()


async def test_fuse_may_only_submit_synthesis_evidence(worker_db, now, trace):
    """One authorized evidence type, enforced by the runtime and not by the worker."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-wrongtype")
    await full_evidence(runtime.cases, trade_case, now)
    _, lease = await claim_fuse(runtime, trade_case, now, trace)

    wrong = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.FUSE,
            EvidenceType.ONCHAIN,
            atlas_payload(),
            key="fuse-impersonation",
        ),
        result_key="fuse-impersonation",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, wrong)
    assert caught.value.code in {
        WorkerErrorCode.EVIDENCE_TYPE_NOT_AUTHORIZED,
        WorkerErrorCode.ROLE_NOT_AUTHORIZED,
    }
