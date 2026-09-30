"""TRADE_CASE_V2: SENTIMENT is advisory; everything else is TRADE_CASE_V1.

New cases open under V2. A case keeps the workflow it was opened with for life,
so every V1 case is still evaluated under V1 — whichever service reads it — and
a version this code does not implement fails closed. SIGNAL keeps its task and
its evidence stays honest: UNKNOWN stays UNKNOWN, a failed task records nothing,
and no social reading enters the risk digest.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.data.tables import TradeCaseEvidenceRow, TradeCaseRow
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceStatus,
    EvidenceType,
    SentimentPayload,
    SpecialistTaskStatus,
    TradeCaseStatus,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import (
    CURRENT_WORKFLOW,
    TRADE_CASE_V1,
    TRADE_CASE_V2,
    policy_for,
)
from src.orchestration.workflow.service import TradeCaseService
from tests.workflow.test_service import (
    anchor,
    atlas,
    open_case,
    setup,
    signal,
    submission,
    trigger,
)


def service(sessions, now, policy=None):
    extra = {} if policy is None else {"policy": policy}
    return TradeCaseService(sessions, clock=FixedClock(now), **extra)


def sentiment(trade_case, now, *, key, assessment, status=EvidenceStatus.AVAILABLE):
    return submission(
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        SentimentPayload(assessment=assessment),
        key=key,
        status=status,
    )


async def pre_trigger_without_signal(cases, now, trace, key):
    """ORBIT (recorded at open), ATLAS and VECTOR — and no SENTIMENT at all."""
    trade_case = await open_case(cases, now, trace, key)
    await cases.record_evidence(trade_case.id, atlas(trade_case, now, key=key + "-atlas"))
    setup_evidence = await cases.record_evidence(
        trade_case.id, setup(trade_case, now, key=key + "-setup")
    )
    return await cases.get_trade_case(trade_case.id), setup_evidence


# ------------------------------------------------------- the policies


def test_v2_differs_from_v1_only_in_sentiment():
    assert CURRENT_WORKFLOW is TRADE_CASE_V2
    changed = [
        (old, new)
        for old, new in zip(TRADE_CASE_V1.requirements, TRADE_CASE_V2.requirements, strict=True)
        if old != new
    ]
    assert [(old.evidence_type, new.required, new.safety_critical) for old, new in changed] == [
        (EvidenceType.SENTIMENT, False, False)
    ]
    for evidence_type in (
        EvidenceType.DISCOVERY,
        EvidenceType.ONCHAIN,
        EvidenceType.TRADE_SETUP,
        EvidenceType.TRIGGER,
        EvidenceType.LIQUIDITY_EXECUTION,
    ):
        assert TRADE_CASE_V2.requirement(evidence_type).required is True
    assert TRADE_CASE_V2.safety_types == TRADE_CASE_V1.safety_types
    assert EvidenceType.SENTIMENT not in TRADE_CASE_V2.safety_types
    assert TRADE_CASE_V2.refreshable_sources == TRADE_CASE_V1.refreshable_sources
    # The SIGNAL task still exists; only its slot is marked optional.
    assert [task.role for task in TRADE_CASE_V2.tasks] == [
        task.role for task in TRADE_CASE_V1.tasks
    ]
    signal_task = next(task for task in TRADE_CASE_V2.tasks if task.role is AgentRole.SIGNAL)
    assert signal_task.required is False


def test_an_unknown_workflow_version_fails_closed():
    with pytest.raises(WorkflowFailure) as caught:
        policy_for("trade-case-v9")
    assert caught.value.code is WorkflowErrorCode.UNSUPPORTED_WORKFLOW_VERSION


# ------------------------------------------------------- V2


async def test_new_cases_open_as_v2_and_reload_as_v2(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case = await open_case(cases, now, trace, "v2-open")
    assert trade_case.workflow_version == "trade-case-v2"
    async with sessions() as session:
        stored = await session.scalar(select(TradeCaseRow.workflow_version))
    assert stored == "trade-case-v2"
    assert (await service(sessions, now).get_trade_case(trade_case.id)).workflow_version == (
        "trade-case-v2"
    )
    # The SIGNAL task is created and claimable, as an optional slot.
    signal_task = next(
        task for task in await cases.tasks(trade_case.id) if task.role is AgentRole.SIGNAL
    )
    assert (signal_task.status, signal_task.required) == (SpecialistTaskStatus.PENDING, False)


async def test_without_any_sentiment_a_v2_case_reaches_risk(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case, setup_evidence = await pre_trigger_without_signal(cases, now, trace, "v2-none")
    assert trade_case.status == TradeCaseStatus.READY_FOR_TRIGGER
    trigger_evidence = await cases.record_evidence(
        trade_case.id, trigger(trade_case, now, setup_evidence.evidence_id, key="v2-none-trigger")
    )
    await cases.record_evidence(
        trade_case.id,
        anchor(
            trade_case,
            now,
            setup_evidence.evidence_id,
            trigger_evidence.evidence_id,
            key="v2-none-anchor",
        ),
    )
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_RISK


async def test_unknown_sentiment_is_kept_honest_and_does_not_block(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case, _ = await pre_trigger_without_signal(cases, now, trace, "v2-unknown")
    recorded = await cases.record_evidence(
        trade_case.id,
        sentiment(
            trade_case,
            now,
            key="v2-unknown-signal",
            assessment="UNKNOWN",
            status=EvidenceStatus.UNKNOWN,
        ),
    )
    assert (recorded.status, recorded.payload.assessment) == (EvidenceStatus.UNKNOWN, "UNKNOWN")
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER
    # Unknown sentiment still cannot be dressed up as available.
    with pytest.raises(ValueError):
        sentiment(trade_case, now, key="x", assessment="UNKNOWN")


async def test_a_failed_signal_task_records_nothing_and_blocks_nothing(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case = await open_case(cases, now, trace, "v2-failed")
    task = next(task for task in await cases.tasks(trade_case.id) if task.role is AgentRole.SIGNAL)
    running = await cases.transition_task(
        trade_case.id, task.task_id, SpecialistTaskStatus.RUNNING, reason_code="WORK_STARTED"
    )
    await cases.transition_task(
        trade_case.id, running.task_id, SpecialistTaskStatus.FAILED, reason_code="SOURCE_FAILED"
    )
    await cases.record_evidence(trade_case.id, atlas(trade_case, now, key="v2-failed-atlas"))
    await cases.record_evidence(trade_case.id, setup(trade_case, now, key="v2-failed-setup"))
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER
    async with sessions() as session:
        types = set(
            (
                await session.scalars(
                    select(TradeCaseEvidenceRow.evidence_type).where(
                        TradeCaseEvidenceRow.trade_case_id == trade_case.id
                    )
                )
            ).all()
        )
    assert EvidenceType.SENTIMENT.value not in types


@pytest.mark.parametrize("assessment", ["NEUTRAL", "NEGATIVE"])
async def test_available_sentiment_is_kept_read_and_never_a_veto(
    workflow_db, now, trace, assessment
):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case, _ = await pre_trigger_without_signal(cases, now, trace, f"v2-{assessment}")
    await cases.record_evidence(
        trade_case.id,
        sentiment(trade_case, now, key=f"v2-{assessment}-signal", assessment=assessment),
    )
    current = active_evidence(await cases.evidence(trade_case.id))
    assert current[EvidenceType.SENTIMENT].payload.assessment == assessment
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER


async def test_sentiment_never_enters_the_v2_risk_digest(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case, setup_evidence = await pre_trigger_without_signal(cases, now, trace, "v2-digest")
    trigger_evidence = await cases.record_evidence(
        trade_case.id, trigger(trade_case, now, setup_evidence.evidence_id, key="v2-digest-trigger")
    )
    await cases.record_evidence(
        trade_case.id,
        anchor(
            trade_case, now, setup_evidence.evidence_id, trigger_evidence.evidence_id, key="v2-d-a"
        ),
    )
    ready = await cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    before = ready.risk_input_digest
    await cases.record_evidence(
        trade_case.id, sentiment(ready, now, key="v2-digest-signal", assessment="NEGATIVE")
    )
    after = await cases.get_trade_case(trade_case.id)
    assert after.status == TradeCaseStatus.READY_FOR_RISK
    assert after.risk_input_digest == before
    current = active_evidence(await cases.evidence(trade_case.id))
    without = {key: value for key, value in current.items() if key is not EvidenceType.SENTIMENT}
    assert risk_input_digest(after, current, TRADE_CASE_V2) == risk_input_digest(
        after, without, TRADE_CASE_V2
    )


async def test_atlas_and_vector_still_gate_a_v2_case(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case = await open_case(cases, now, trace, "v2-gates")
    assert trade_case.status == TradeCaseStatus.EVIDENCE_PENDING
    assert {blocker.role for blocker in trade_case.blockers} == {AgentRole.ATLAS, AgentRole.VECTOR}
    blocked = await cases.record_evidence(
        trade_case.id,
        atlas(trade_case, now, key="v2-gates-atlas", status=EvidenceStatus.UNKNOWN),
    )
    assert blocked is not None
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.BLOCKED


# ------------------------------------------------------- V1 unchanged


async def test_v1_still_waits_for_sentiment(workflow_db, now, trace):
    _, sessions = workflow_db
    v1 = service(sessions, now, TRADE_CASE_V1)
    trade_case, _ = await pre_trigger_without_signal(v1, now, trace, "v1-none")
    assert trade_case.workflow_version == "trade-case-v1"
    assert trade_case.status == TradeCaseStatus.EVIDENCE_PENDING
    assert {blocker.role for blocker in trade_case.blockers} == {AgentRole.SIGNAL}
    await v1.record_evidence(
        trade_case.id,
        sentiment(
            trade_case, now, key="v1-unknown", assessment="UNKNOWN", status=EvidenceStatus.UNKNOWN
        ),
    )
    assert (await v1.get_trade_case(trade_case.id)).status == TradeCaseStatus.EVIDENCE_PENDING


async def test_a_v1_case_keeps_v1_rules_under_a_v2_service(workflow_db, now, trace):
    """A later default never re-judges an old case."""
    _, sessions = workflow_db
    v1 = service(sessions, now, TRADE_CASE_V1)
    trade_case, _ = await pre_trigger_without_signal(v1, now, trace, "v1-kept")
    current = service(sessions, now + timedelta(minutes=1))
    evaluated = await current.evaluate_trade_case(trade_case.id)
    assert evaluated.workflow_version == "trade-case-v1"
    assert evaluated.status == TradeCaseStatus.EVIDENCE_PENDING
    # Recording evidence through the V2 service still applies V1's rules.
    await current.record_evidence(trade_case.id, signal(trade_case, now, key="v1-kept-signal"))
    assert (await current.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER


async def test_a_v1_open_replays_unchanged(workflow_db, now, trace):
    _, sessions = workflow_db
    v1 = service(sessions, now, TRADE_CASE_V1)
    first = await open_case(v1, now, trace, "v1-replay")
    again = await open_case(v1, now, trace, "v1-replay")
    assert (again.id, again.open_fingerprint) == (first.id, first.open_fingerprint)
    # The same key under V2 is a different request, refused rather than merged.
    with pytest.raises(WorkflowFailure) as caught:
        await open_case(service(sessions, now), now, trace, "v1-replay")
    assert caught.value.code is WorkflowErrorCode.IDEMPOTENCY_CONFLICT


async def test_a_stored_unknown_version_is_refused_on_read(workflow_db, now, trace):
    _, sessions = workflow_db
    cases = service(sessions, now)
    trade_case = await open_case(cases, now, trace, "v9")
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseRow)
            .where(TradeCaseRow.id == trade_case.id)
            .values(workflow_version="trade-case-v9")
        )
    with pytest.raises(WorkflowFailure) as caught:
        await cases.get_trade_case(trade_case.id)
    assert caught.value.code is WorkflowErrorCode.UNSUPPORTED_WORKFLOW_VERSION
