"""The derived-evidence lifecycle: derive, obsolete, re-derive.

FUSE is a view over four other pieces of evidence, and a view has a lifecycle
that an observation does not. ATLAS looking at a contract produces a fact that
stays true until somebody looks again; FUSE produces a reading that stops
describing the case the moment one of its inputs is replaced.

The first implementation got half of that right — an in-flight synthesis of
replaced evidence was refused — and the other half wrong: once a synthesis was
recorded the task was SUCCEEDED forever, so no fresh reading could ever be
derived, and the stale one stayed current and ACCEPTED while pointing at
superseded sources. These tests cover the whole loop rather than either end.
"""

from datetime import timedelta

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.models import (
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceType,
    SpecialistTaskStatus,
    SynthesisPayload,
    TradeCaseStatus,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from tests.fuse.conftest import onchain, sentiment, trade_setup
from tests.fuse.test_workflow import (
    build_stack,
    full_evidence,
    open_case,
    record,
    synthesize_evidence,
)

pytestmark = pytest.mark.usefixtures("worker_db")

SOURCE_TYPES = (
    EvidenceType.DISCOVERY,
    EvidenceType.ONCHAIN,
    EvidenceType.SENTIMENT,
    EvidenceType.TRADE_SETUP,
)


async def fuse_task(runtime, trade_case):
    tasks = await runtime.cases.tasks(trade_case.id)
    return next(task for task in tasks if task.role == AgentRole.FUSE)


async def claim_fuse(runtime, trade_case, key="lifecycle"):
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key=f"fuse-{key}-{trade_case.id}",
            role=AgentRole.FUSE,
            runtime_version="worker-runtime-v1",
        )
    )
    return await runtime.claim_next_task(worker.worker_instance_id)


async def live_sources(runtime, trade_case):
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    return {current[kind].evidence_id for kind in SOURCE_TYPES}


# ---------------------------------------------------------- the whole loop


async def test_scenario_34_the_full_derived_lifecycle(worker_db, now, trace):
    """Derive over S1, watch S1 become S2, derive again — end to end.

    The single most important test of this phase, because every individual
    guarantee below was already true of the first implementation except the one
    that makes them add up to something: that a fresh reading can be produced
    at all once the old one stops applying.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-lifecycle")
    await full_evidence(runtime.cases, trade_case, now)

    # The task is eligible before anything has been derived.
    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.PENDING
    lease = await claim_fuse(runtime, trade_case, key="first")
    assert lease is not None
    assert lease.role == AgentRole.FUSE

    # F1 over S1, submitted through the real runtime.
    report = await synthesize_evidence(sessions, trade_case, now, trace)
    disposition = await runtime.submit_task_result(lease, report)
    assert disposition.outcome.value == "SUCCEEDED"

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    first = current[EvidenceType.SYNTHESIS]
    assert first.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert set(first.payload.source_evidence_ids) == await live_sources(runtime, trade_case)
    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.SUCCEEDED

    # Recording the synthesis changed nothing about the case.
    case_before = await runtime.cases.get_trade_case(trade_case.id)
    assert case_before.status == TradeCaseStatus.READY_FOR_TRIGGER

    # S1 becomes S2: SIGNAL is replaced. The runtime's clock advances with it,
    # since evidence dated ahead of the reader's clock reads as observed in the
    # future and is refused as invalid.
    later = now + timedelta(minutes=5)
    runtime, _ = build_stack(sessions, later)
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE", data_quality="DEGRADED"),
        key="lifecycle-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )

    # The task re-arms, because its inputs are no longer the ones it read.
    rearmed = await fuse_task(runtime, trade_case)
    assert rearmed.status == SpecialistTaskStatus.PENDING
    assert rearmed.reason_code == "DERIVED_INPUT_CHANGED"
    assert rearmed.attempt == 2

    # A worker can claim it and derive a fresh reading over S2.
    second_lease = await claim_fuse(runtime, trade_case, key="second")
    assert second_lease is not None
    second_report = await synthesize_evidence(sessions, trade_case, later, trace)
    await runtime.submit_task_result(second_lease, second_report)

    # F2 supersedes F1, so the case holds exactly one current reading and it is
    # the one describing the evidence the case actually has.
    final = active_evidence(await runtime.cases.evidence(trade_case.id))
    second = final[EvidenceType.SYNTHESIS]
    assert second.evidence_id != first.evidence_id
    assert second.supersedes_id == first.evidence_id
    assert set(second.payload.source_evidence_ids) == await live_sources(runtime, trade_case)
    assert isinstance(second.payload, SynthesisPayload)
    assert second.payload.synthesis is not None
    assert second.payload.synthesis.input_digest != first.payload.synthesis.input_digest

    # And the case is still exactly where it was. A synthesis moves nothing.
    case_after = await runtime.cases.get_trade_case(trade_case.id)
    assert case_after.status == case_before.status


# ------------------------------------------------- 7, 8, 9: each source


@pytest.mark.parametrize(
    ("evidence_type", "role", "payload_for"),
    [
        (EvidenceType.SENTIMENT, AgentRole.SIGNAL, lambda now: sentiment(assessment="NEGATIVE")),
        (EvidenceType.ONCHAIN, AgentRole.ATLAS, lambda now: onchain(verdict="CLEAR")),
        (EvidenceType.TRADE_SETUP, AgentRole.VECTOR, lambda now: trade_setup(now)),
    ],
)
async def test_any_source_change_rearms_the_derived_task(
    worker_db, now, trace, evidence_type, role, payload_for
):
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, f"rearm-{evidence_type.value}")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="initial")
    await runtime.submit_task_result(
        lease, await synthesize_evidence(sessions, trade_case, now, trace)
    )
    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.SUCCEEDED

    later = now + timedelta(minutes=5)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    runtime, _ = build_stack(sessions, later)
    await record(
        runtime.cases,
        trade_case,
        later,
        role,
        evidence_type,
        payload_for(later),
        key=f"rearm-{evidence_type.value}-2",
        supersedes_id=current[evidence_type].evidence_id,
    )
    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.PENDING


async def test_scenario_4_the_same_source_set_does_not_rearm_anything(worker_db, now, trace):
    """§4. Re-derivation is driven by inputs changing, not by time passing.

    Recording the synthesis itself is the case that would loop: a derived output
    that counted as its own input would re-arm its own task forever. The policy
    declares the inputs explicitly, and the synthesis is not among them.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-no-churn")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="once")
    await runtime.submit_task_result(
        lease, await synthesize_evidence(sessions, trade_case, now, trace)
    )

    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.SUCCEEDED
    assert await claim_fuse(runtime, trade_case, key="again") is None


async def test_only_the_derived_task_rearms(worker_db, now, trace):
    """§3. Nothing else reruns. One supersession is not a cascade of work."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-scope")
    await full_evidence(runtime.cases, trade_case, now)

    before = {task.role: task.status for task in await runtime.cases.tasks(trade_case.id)}
    later = now + timedelta(minutes=5)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    runtime, _ = build_stack(sessions, later)
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEUTRAL"),
        key="scope-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    after = {task.role: task.status for task in await runtime.cases.tasks(trade_case.id)}

    for role in (AgentRole.ORBIT, AgentRole.ATLAS, AgentRole.VECTOR, AgentRole.PULSE):
        assert before[role] == after[role], role


def test_only_one_task_is_declared_derived():
    """The flag is opt-in and exactly one task opts in."""
    derived = [task for task in TRADE_CASE_V1.tasks if task.derived_from]
    assert len(derived) == 1
    assert derived[0].role == AgentRole.FUSE
    assert derived[0].derived_from == set(SOURCE_TYPES)
    # A synthesis is not an input to itself, so it cannot re-arm its own task.
    assert EvidenceType.SYNTHESIS not in derived[0].derived_from
    assert TRADE_CASE_V1.derived_tasks(EvidenceType.SYNTHESIS) == ()


# ------------------------------------------------------ 10: stage boundary


async def test_scenario_10_nothing_rearms_once_the_case_has_a_trigger(worker_db, now, trace):
    """§10. The pre-trigger question is over, so advisory refreshes stop.

    Without this, an advisory sentiment refresh during the execution stage would
    queue work to re-answer a question the case has moved past — endlessly, for
    as long as social data kept arriving.
    """
    from tests.worker.conftest import trigger_payload

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-stage")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="pre")
    await runtime.submit_task_result(
        lease, await synthesize_evidence(sessions, trade_case, now, trace)
    )

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    await record(
        runtime.cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(current[EvidenceType.TRADE_SETUP].evidence_id),
        key="stage-trigger",
    )

    later = now + timedelta(minutes=5)
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE"),
        key="stage-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )

    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.SUCCEEDED
    assert await claim_fuse(runtime, trade_case, key="post") is None


# --------------------------------------------- 20: risk stays where it was


async def test_scenario_20_rearming_does_not_make_sentiment_risk_binding(worker_db, now, trace):
    """The whole point of the re-arming being advisory.

    A sentiment change now causes real work — a fresh synthesis — which is
    exactly the shape of an accidental risk dependency. The risk snapshot must
    not move, and a synthesis being recorded must not move it either.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-rearm-risk")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="risk-first")
    await runtime.submit_task_result(
        lease, await synthesize_evidence(sessions, trade_case, now, trace)
    )

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))

    later = now + timedelta(minutes=5)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    runtime, _ = build_stack(sessions, later)
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE"),
        key="rearm-risk-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    second_lease = await claim_fuse(runtime, trade_case, key="risk-second")
    assert second_lease is not None
    await runtime.submit_task_result(
        second_lease, await synthesize_evidence(sessions, trade_case, later, trace)
    )

    after = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    assert before == after, "a re-derived synthesis must not move the risk snapshot"


async def test_a_vector_change_moves_the_risk_digest_through_vector(worker_db, now, trace):
    """The control. Risk binding still comes from the source, not the summary."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-rearm-vector")
    await full_evidence(runtime.cases, trade_case, now)

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    later = now + timedelta(minutes=5)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    runtime, _ = build_stack(sessions, later)
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        trade_setup(later),
        key="rearm-vector-2",
        supersedes_id=current[EvidenceType.TRADE_SETUP].evidence_id,
    )
    after = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))
    assert before != after
    assert (await fuse_task(runtime, trade_case)).status == SpecialistTaskStatus.PENDING


# ---------------------------------------- 37, 38, 39: time passes mid-flight


async def test_scenario_38_a_synthesis_that_outlived_its_inputs_is_refused(worker_db, now, trace):
    """§38. Identity is not freshness, and a derived result needs both.

    The context is built while every source is current. The worker computes. By
    the time the answer arrives a source has aged out — nobody superseded it, so
    the reference check sees nothing wrong, and without a freshness check the
    result would be stored claiming a currency its inputs no longer have.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-toctou")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="toctou")
    report = await synthesize_evidence(sessions, trade_case, now, trace)

    # Every reference is still current — nothing was superseded.
    assert set(report.submission.payload.source_evidence_ids) == await live_sources(
        runtime, trade_case
    )

    # But the synthesis has expired while the worker was thinking.
    expired_at = report.submission.valid_until + timedelta(seconds=1)
    late_runtime, _ = build_stack(sessions, expired_at)
    with pytest.raises(WorkerFailure) as caught:
        await late_runtime.submit_task_result(lease, report)
    assert caught.value.code in {
        WorkerErrorCode.EVIDENCE_STALE,
        WorkerErrorCode.LEASE_EXPIRED,
    }


async def test_a_synthesis_submitted_while_still_valid_is_accepted(worker_db, now, trace):
    """The control: the freshness rule refuses only what has actually expired."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-toctou-ok")
    await full_evidence(runtime.cases, trade_case, now)
    lease = await claim_fuse(runtime, trade_case, key="toctou-ok")
    report = await synthesize_evidence(sessions, trade_case, now, trace)

    soon = now + timedelta(seconds=30)
    assert report.submission.valid_until > soon
    in_time, _ = build_stack(sessions, soon)
    disposition = await in_time.submit_task_result(lease, report)
    assert disposition.outcome.value == "SUCCEEDED"


async def test_scenario_39_a_synthesis_cannot_outlive_the_setup_it_describes(worker_db, now, trace):
    """§11, §39. The setup's own expiry binds, not only its envelope's.

    VECTOR states when the geometry stops being true, and that instant can
    arrive well before the envelope carrying it goes stale. Without this a
    synthesis would look current while describing a setup that had already
    lapsed — and no identity check would catch it, because the setup's evidence
    id never changed.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "fuse-setup-expiry")
    # A setup that lapses in two minutes, inside an envelope valid for thirty.
    await full_evidence(
        runtime.cases,
        trade_case,
        now,
        trade_setup=trade_setup(now, expires_in=timedelta(minutes=2)),
    )

    report = await synthesize_evidence(sessions, trade_case, now, trace)
    assert report.submission.valid_until == now + timedelta(minutes=2)

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    envelope_expiry = current[EvidenceType.TRADE_SETUP].valid_until
    assert report.submission.valid_until < envelope_expiry, (
        "the setup's own expiry must bind before its envelope's"
    )
