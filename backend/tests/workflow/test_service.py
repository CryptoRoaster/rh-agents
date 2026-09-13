import asyncio
import random
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from src.core.clock import FixedClock
from src.core.models import (
    AgentRole,
    RiskDecision,
    RiskMetrics,
    RiskOutcome,
)
from src.data.tables import TradeCaseRiskBindingRow
from src.markets.fake import fixture_snapshot
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    RISK_AUTHORIZED_CASE_STATUSES,
    TERMINAL_CASE_STATUSES,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    LiquidityExecutionPayload,
    OnchainPayload,
    SentimentPayload,
    SpecialistTaskStatus,
    TradeCaseStatus,
    TradeSetupPayload,
    TriggerPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService, risk_from_row
from src.risk.authorization import RiskAuthorization


async def open_case(service, now, trace, key="case-1"):
    return await service.open_trade_case(
        fixture_snapshot(now, trace).pair.market_identity,
        originating_discovery_reference=uuid5(NAMESPACE_URL, "discovery:" + key),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
        strategy_policy_id="paper-policy-v1",
    )


def submission(
    trade_case,
    now,
    role,
    evidence_type,
    payload,
    *,
    key,
    status=EvidenceStatus.AVAILABLE,
    supersedes_id=None,
    valid_until=None,
):
    return EvidenceSubmission(
        idempotency_key=key,
        producer_role=role,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test-source", reference_id=uuid4()),
        observed_at=now,
        valid_until=valid_until or now + timedelta(minutes=10),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_NOT_VERIFIED",),
        payload=payload,
        correlation_id=trade_case.correlation_id,
        supersedes_id=supersedes_id,
    )


def atlas(
    trade_case,
    now,
    *,
    key="atlas-1",
    status=EvidenceStatus.AVAILABLE,
    supersedes=None,
    valid_until=None,
):
    return submission(
        trade_case,
        now,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        OnchainPayload(
            holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="PASS"
        ),
        key=key,
        status=status,
        supersedes_id=supersedes,
        valid_until=valid_until,
    )


def signal(trade_case, now, *, key="signal-1", valid_until=None):
    return submission(
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        SentimentPayload(assessment="NEUTRAL"),
        key=key,
        valid_until=valid_until,
    )


def setup(trade_case, now, *, key="setup-1", supersedes=None, valid_until=None):
    return submission(
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        TradeSetupPayload(
            setup_id=uuid4(),
            side="BUY",
            entry_price=Decimal("1"),
            invalidation_price=Decimal("0.8"),
            target_prices=(Decimal("1.2"),),
        ),
        key=key,
        supersedes_id=supersedes,
        valid_until=valid_until,
    )


def trigger(
    trade_case,
    now,
    setup_id,
    *,
    key="trigger-1",
    supersedes=None,
    valid_until=None,
):
    return submission(
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        TriggerPayload(
            setup_evidence_id=setup_id,
            observed_price=Decimal("1"),
            trigger_code="ENTRY_LEVEL_REACHED",
        ),
        key=key,
        supersedes_id=supersedes,
        valid_until=valid_until,
    )


def anchor(
    trade_case,
    now,
    setup_id,
    trigger_id,
    *,
    key="anchor-1",
    status=EvidenceStatus.AVAILABLE,
    supersedes=None,
    valid_until=None,
):
    available = status == EvidenceStatus.AVAILABLE
    return submission(
        trade_case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        LiquidityExecutionPayload(
            setup_evidence_id=setup_id,
            trigger_evidence_id=trigger_id,
            quoted_price=Decimal("1") if available else None,
            liquidity_usd=Decimal("500000") if available else None,
            estimated_slippage_bps=Decimal("25") if available else None,
            price_impact_bps=Decimal("20") if available else None,
            maximum_safe_size_usd=Decimal("2500") if available else None,
            routing_provenance="quoted-route-v1" if available else None,
        ),
        key=key,
        status=status,
        supersedes_id=supersedes,
        valid_until=valid_until,
    )


async def ready_for_risk(
    service, now, trace, key="case-ready", *, pre_valid_until=None, anchor_valid_until=None
):
    trade_case = await open_case(service, now, trace, key)
    await service.record_evidence(
        trade_case.id,
        atlas(trade_case, now, key=key + "-atlas", valid_until=pre_valid_until),
    )
    await service.record_evidence(
        trade_case.id,
        signal(trade_case, now, key=key + "-signal", valid_until=pre_valid_until),
    )
    setup_evidence = await service.record_evidence(
        trade_case.id,
        setup(trade_case, now, key=key + "-setup", valid_until=pre_valid_until),
    )
    trigger_evidence = await service.record_evidence(
        trade_case.id,
        trigger(
            trade_case,
            now,
            setup_evidence.evidence_id,
            key=key + "-trigger",
            valid_until=pre_valid_until,
        ),
    )
    await service.record_evidence(
        trade_case.id,
        anchor(
            trade_case,
            now,
            setup_evidence.evidence_id,
            trigger_evidence.evidence_id,
            key=key + "-anchor",
            valid_until=anchor_valid_until,
        ),
    )
    return await service.get_trade_case(trade_case.id), setup_evidence


def risk_decision(
    trade_case,
    now,
    outcome=RiskOutcome.APPROVE,
    capacity=Decimal("2500"),
    *,
    reason_codes=None,
    position_limit=Decimal("2500"),
    slippage=Decimal("100"),
):
    if reason_codes is None:
        reason_codes = (
            ("WITHIN_LIMITS",) if outcome == RiskOutcome.APPROVE else ("MAX_POSITION_SIZE",)
        )
    return RiskDecision(
        source="SENTINEL",
        correlation_id=trade_case.correlation_id,
        created_at=now,
        updated_at=now,
        intent_id=uuid4(),
        intent_fingerprint="intent",
        market_snapshot_id=uuid4(),
        market_fingerprint="market",
        outcome=outcome,
        reason_codes=reason_codes,
        position_size_limit_usd=position_limit,
        max_additional_notional_usd=capacity,
        max_slippage_bps=slippage,
        metrics=RiskMetrics(
            requested_notional_usd=Decimal("100"),
            worst_case_notional_usd=Decimal("101"),
            exposure_usd=Decimal("0"),
            daily_loss_usd=Decimal("0"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
        ),
        evaluated_at=now,
        expires_at=now + timedelta(minutes=1),
    )


async def test_open_is_idempotent_and_creates_team_tasks(workflow_service, now, trace):
    first = await open_case(workflow_service, now, trace)
    second = await open_case(workflow_service, now, trace)
    assert first == second
    assert first.status == TradeCaseStatus.EVIDENCE_PENDING
    tasks = await workflow_service.tasks(first.id)
    assert {task.role for task in tasks} == set(AgentRole)
    # ORBIT verifies the candidate the case was opened from, so its task is real
    # claimable work; only COMMANDER's open step is complete on arrival.
    assert (
        next(task for task in tasks if task.role == AgentRole.ORBIT).status
        == SpecialistTaskStatus.PENDING
    )
    assert (
        next(task for task in tasks if task.role == AgentRole.COMMANDER).status
        == SpecialistTaskStatus.SUCCEEDED
    )
    assert next(task for task in tasks if task.role == AgentRole.FUSE).required is False
    assert len(await workflow_service.evidence(first.id)) == 1
    assert await workflow_service.create_required_tasks(first.id) == tasks
    with pytest.raises(ValidationError):
        first.status = TradeCaseStatus.READY_FOR_RISK  # type: ignore[misc]


async def test_conflicting_open_and_evidence_replay_reject(workflow_service, now, trace):
    trade_case = await open_case(workflow_service, now, trace)
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.open_trade_case(
            trade_case.market,
            originating_discovery_reference=uuid4(),
            correlation_id=trace,
            idempotency_key="case-1",
            expires_at=now + timedelta(hours=1),
        )
    assert caught.value.code == WorkflowErrorCode.IDEMPOTENCY_CONFLICT

    evidence = atlas(trade_case, now)
    assert await workflow_service.record_evidence(
        trade_case.id, evidence
    ) == await workflow_service.record_evidence(trade_case.id, evidence)
    conflict = evidence.model_copy(
        update={"provenance": EvidenceProvenance(source="another-source", reference_id=uuid4())}
    )
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_evidence(trade_case.id, conflict)
    assert caught.value.code == WorkflowErrorCode.IDEMPOTENCY_CONFLICT


async def test_happy_path_binds_sentinel_to_digest(workflow_service, now, trace):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace)
    assert trade_case.status == TradeCaseStatus.READY_FOR_RISK
    assert trade_case.risk_input_digest is not None
    approved = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now),
        risk_input_digest=trade_case.risk_input_digest,
        expected_revision=trade_case.revision,
    )
    assert approved.status == TradeCaseStatus.RISK_APPROVED
    replay = await workflow_service.evaluate_trade_case(approved.id)
    assert replay.status == TradeCaseStatus.RISK_APPROVED
    transitions = [
        event
        for event in await workflow_service.timeline(approved.id)
        if event.event_type == "STATE_TRANSITION"
    ]
    assert [event.payload["to_status"] for event in transitions] == [
        "EVIDENCE_PENDING",
        "READY_FOR_TRIGGER",
        "TRIGGERED",
        "EXECUTION_EVIDENCE_PENDING",
        "READY_FOR_RISK",
        "RISK_APPROVED",
    ]


async def test_unknown_atlas_blocks_and_valid_replacement_clears(workflow_service, now, trace):
    trade_case = await open_case(workflow_service, now, trace)
    unknown = await workflow_service.record_evidence(
        trade_case.id,
        atlas(trade_case, now, status=EvidenceStatus.UNKNOWN),
    )
    blocked = await workflow_service.get_trade_case(trade_case.id)
    assert blocked.status == TradeCaseStatus.BLOCKED
    assert blocked.blockers[0].code == "ATLAS_UNKNOWN"
    await workflow_service.record_evidence(
        trade_case.id,
        atlas(trade_case, now, key="atlas-2", supersedes=unknown.evidence_id),
    )
    current = await workflow_service.get_trade_case(trade_case.id)
    assert current.status == TradeCaseStatus.EVIDENCE_PENDING
    assert current.blockers
    assert all(blocker.role != AgentRole.ATLAS for blocker in current.blockers)


async def test_unknown_anchor_blocks_before_risk(workflow_service, now, trace):
    trade_case = await open_case(workflow_service, now, trace)
    await workflow_service.record_evidence(trade_case.id, atlas(trade_case, now))
    await workflow_service.record_evidence(trade_case.id, signal(trade_case, now))
    setup_evidence = await workflow_service.record_evidence(trade_case.id, setup(trade_case, now))
    trigger_evidence = await workflow_service.record_evidence(
        trade_case.id, trigger(trade_case, now, setup_evidence.evidence_id)
    )
    await workflow_service.record_evidence(
        trade_case.id,
        anchor(
            trade_case,
            now,
            setup_evidence.evidence_id,
            trigger_evidence.evidence_id,
            status=EvidenceStatus.UNKNOWN,
        ),
    )
    blocked = await workflow_service.get_trade_case(trade_case.id)
    assert blocked.status == TradeCaseStatus.BLOCKED
    assert blocked.blockers[0].code == "ANCHOR_UNKNOWN_EXECUTION_EVIDENCE"
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_risk_decision(
            trade_case.id, risk_decision(trade_case, now), risk_input_digest="0" * 64
        )
    assert caught.value.code == WorkflowErrorCode.RISK_BINDING


async def test_new_vector_setup_invalidates_old_trigger_and_approval(workflow_service, now, trace):
    trade_case, old_setup = await ready_for_risk(workflow_service, now, trace)
    approved = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now),
        risk_input_digest=trade_case.risk_input_digest,
    )
    replacement = await workflow_service.record_evidence(
        approved.id,
        setup(approved, now, key="setup-2", supersedes=old_setup.evidence_id),
    )
    waiting = await workflow_service.get_trade_case(approved.id)
    assert waiting.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert waiting.reason_code == "TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP"
    old_trigger = next(
        item
        for item in await workflow_service.evidence(approved.id)
        if item.evidence_type == EvidenceType.TRIGGER
    )
    new_trigger = await workflow_service.record_evidence(
        approved.id,
        trigger(
            approved,
            now,
            replacement.evidence_id,
            key="trigger-2",
            supersedes=old_trigger.evidence_id,
        ),
    )
    waiting_for_anchor = await workflow_service.get_trade_case(approved.id)
    assert waiting_for_anchor.status == TradeCaseStatus.EXECUTION_EVIDENCE_PENDING
    old_anchor = next(
        item
        for item in await workflow_service.evidence(approved.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    await workflow_service.record_evidence(
        approved.id,
        anchor(
            approved,
            now,
            replacement.evidence_id,
            new_trigger.evidence_id,
            key="anchor-2",
            supersedes=old_anchor.evidence_id,
        ),
    )
    ready = await workflow_service.get_trade_case(approved.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    assert ready.risk_input_digest != trade_case.risk_input_digest


async def test_stale_anchor_revokes_ready_and_approval(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(
        service,
        now,
        trace,
        pre_valid_until=now + timedelta(minutes=30),
        anchor_valid_until=now + timedelta(minutes=1),
    )
    approved = await service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now),
        risk_input_digest=trade_case.risk_input_digest,
    )
    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(minutes=1)))
    stale = await later.evaluate_trade_case(approved.id)
    assert stale.status == TradeCaseStatus.BLOCKED
    assert stale.blockers[0].code == "ANCHOR_STALE_EXECUTION_EVIDENCE"


@pytest.mark.parametrize(
    "label,outcome,capacity,reason_codes,expected",
    [
        ("approve", RiskOutcome.APPROVE, Decimal("2500"), ("WITHIN_LIMITS",), "RISK_APPROVED"),
        ("sizing", RiskOutcome.REJECT, Decimal("50"), ("MAX_POSITION_SIZE",), "RISK_LIMITED"),
        (
            "sizing-pair",
            RiskOutcome.REJECT,
            Decimal("50"),
            ("MAX_EXPOSURE", "INSUFFICIENT_CASH"),
            "RISK_LIMITED",
        ),
        ("no-capacity", RiskOutcome.REJECT, Decimal("0"), ("MAX_POSITION_SIZE",), "RISK_REJECTED"),
        (
            "mixed-blocker",
            RiskOutcome.REJECT,
            Decimal("50"),
            ("MAX_POSITION_SIZE", "INSUFFICIENT_LIQUIDITY"),
            "RISK_REJECTED",
        ),
        (
            "sell-shortfall",
            RiskOutcome.REJECT,
            Decimal("50"),
            ("INSUFFICIENT_POSITION",),
            "RISK_REJECTED",
        ),
        ("pause", RiskOutcome.PAUSE_SYSTEM, Decimal("50"), ("KILL_SWITCH",), "RISK_REJECTED"),
        (
            "pause-sizing",
            RiskOutcome.PAUSE_SYSTEM,
            Decimal("50"),
            ("MAX_POSITION_SIZE",),
            "RISK_REJECTED",
        ),
    ],
)
async def test_sentinel_decisions_map_through_one_classifier(
    workflow_service, now, trace, label, outcome, capacity, reason_codes, expected
):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace, key=f"map-{label}")
    result = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now, outcome, capacity, reason_codes=reason_codes),
        risk_input_digest=trade_case.risk_input_digest,
    )
    assert result.status == TradeCaseStatus(expected)


async def test_rejection_is_terminal_and_cannot_be_overridden(workflow_service, now, trace):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace, key="rejected-terminal")
    rejected = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now, RiskOutcome.REJECT, Decimal("0")),
        risk_input_digest=trade_case.risk_input_digest,
    )
    assert rejected.status == TradeCaseStatus.RISK_REJECTED
    # No later evidence, task move, evaluation, cancel or second decision may
    # walk a rejected case back toward authorization.
    for failing in (
        workflow_service.record_evidence(
            rejected.id, atlas(rejected, now, key="rejected-terminal-atlas-2")
        ),
        workflow_service.cancel_trade_case(rejected.id),
        workflow_service.record_risk_decision(
            rejected.id,
            risk_decision(rejected, now, RiskOutcome.APPROVE),
            risk_input_digest=rejected.risk_input_digest,
        ),
    ):
        with pytest.raises(WorkflowFailure) as caught:
            await failing
        assert caught.value.code in {
            WorkflowErrorCode.TERMINAL_CASE,
            WorkflowErrorCode.RISK_BINDING,
        }
    assert (
        await workflow_service.evaluate_trade_case(rejected.id)
    ).status == TradeCaseStatus.RISK_REJECTED


@pytest.mark.parametrize(
    "key,outcome,capacity,expected",
    [
        ("approved", RiskOutcome.APPROVE, Decimal("2500"), TradeCaseStatus.RISK_APPROVED),
        ("limited", RiskOutcome.REJECT, Decimal("50"), TradeCaseStatus.RISK_LIMITED),
    ],
)
async def test_authorized_states_stay_mutable_and_revalidatable(
    workflow_service, now, trace, key, outcome, capacity, expected
):
    trade_case, setup_evidence = await ready_for_risk(
        workflow_service, now, trace, key=f"mutable-{key}"
    )
    authorized = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now, outcome, capacity),
        risk_input_digest=trade_case.risk_input_digest,
    )
    assert authorized.status == expected
    assert expected not in TERMINAL_CASE_STATUSES
    assert expected in RISK_AUTHORIZED_CASE_STATUSES
    # Superseding the VECTOR setup must still be recordable, otherwise the
    # evaluator could never revoke the authorization it already granted.
    await workflow_service.record_evidence(
        authorized.id,
        setup(
            authorized,
            now,
            key=f"mutable-{key}-setup-2",
            supersedes=setup_evidence.evidence_id,
        ),
    )
    revoked = await workflow_service.get_trade_case(authorized.id)
    # Not blindly READY_FOR_RISK: the evaluator returns to the prerequisite the
    # changed evidence actually invalidated, and names why.
    assert revoked.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert revoked.reason_code == "TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP"
    assert (await workflow_service.cancel_trade_case(revoked.id)).status == (
        TradeCaseStatus.CANCELLED
    )


async def test_cancel_and_expire_are_terminal(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    cancelled = await service.cancel_trade_case((await open_case(service, now, trace)).id)
    assert cancelled.status == TradeCaseStatus.CANCELLED
    with pytest.raises(WorkflowFailure):
        await service.record_evidence(cancelled.id, atlas(cancelled, now))

    expiring = await open_case(service, now, uuid4(), "expiring")
    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(hours=2)))
    expired = await later.expire_trade_case(expiring.id)
    assert expired.status == TradeCaseStatus.EXPIRED


async def test_task_transitions_and_expiry(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_case(service, now, trace)
    atlas_task = next(
        task for task in await service.tasks(trade_case.id) if task.role == AgentRole.ATLAS
    )
    running = await service.transition_task(
        trade_case.id, atlas_task.task_id, SpecialistTaskStatus.RUNNING, reason_code="WORK_STARTED"
    )
    assert running.started_at == now
    failed = await service.transition_task(
        trade_case.id, running.task_id, SpecialistTaskStatus.FAILED, reason_code="SOURCE_FAILED"
    )
    assert failed.completed_at == now
    with pytest.raises(WorkflowFailure):
        await service.transition_task(
            trade_case.id,
            failed.task_id,
            SpecialistTaskStatus.RUNNING,
            reason_code="ILLEGAL_RESTART",
        )

    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(hours=2)))
    await later.evaluate_trade_case(trade_case.id)
    pending = [
        task
        for task in await later.tasks(trade_case.id)
        if task.status == SpecialistTaskStatus.EXPIRED
    ]
    assert pending


async def test_parallel_evidence_and_evaluator_are_serialized(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_case(service, now, trace)
    results = await asyncio.gather(
        service.record_evidence(trade_case.id, atlas(trade_case, now)),
        service.record_evidence(trade_case.id, signal(trade_case, now)),
        service.record_evidence(trade_case.id, setup(trade_case, now)),
        service.evaluate_trade_case(trade_case.id),
    )
    assert len(results) == 4
    assert (await service.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER
    transitions = [
        event
        for event in await service.timeline(trade_case.id)
        if event.event_type == "STATE_TRANSITION"
    ]
    revisions = [event.payload["revision"] for event in transitions]
    assert revisions == sorted(set(revisions))


async def test_expected_revision_rejects_stale_caller(workflow_service, now, trace):
    trade_case = await open_case(workflow_service, now, trace)
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_evidence(
            trade_case.id,
            atlas(trade_case, now),
            expected_revision=trade_case.revision - 1,
        )
    assert caught.value.code == WorkflowErrorCode.CONCURRENCY_CONFLICT


@pytest.mark.parametrize(
    "status",
    [
        EvidenceStatus.UNKNOWN,
        EvidenceStatus.UNAVAILABLE,
        EvidenceStatus.INVALID,
        EvidenceStatus.STALE,
    ],
)
async def test_every_nonavailable_critical_status_blocks(workflow_service, now, status):
    trade_case = await open_case(workflow_service, now, uuid4(), "critical-" + status.value)
    await workflow_service.record_evidence(
        trade_case.id,
        atlas(trade_case, now, key="atlas-" + status.value, status=status),
    )
    blocked = await workflow_service.get_trade_case(trade_case.id)
    assert blocked.status == TradeCaseStatus.BLOCKED
    assert blocked.blockers[0].code == "ATLAS_" + status.value


def test_available_evidence_cannot_hide_unknown_payload(now, trace):
    trade_case = type("Case", (), {"correlation_id": trace})()
    with pytest.raises(ValidationError):
        submission(
            trade_case,
            now,
            AgentRole.ATLAS,
            EvidenceType.ONCHAIN,
            OnchainPayload(
                holder_integrity="UNKNOWN",
                dev_wallet_integrity="PASS",
                contract_integrity="PASS",
            ),
            key="hidden-unknown",
        )


async def test_wrong_role_case_and_supersession_reject(workflow_service, now, trace):
    first = await open_case(workflow_service, now, trace, "binding-first")
    second = await open_case(workflow_service, now, uuid4(), "binding-second")
    wrong_role = atlas(first, now, key="wrong-role").model_copy(
        update={"producer_role": AgentRole.SIGNAL}
    )
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_evidence(first.id, wrong_role)
    assert caught.value.code == WorkflowErrorCode.EVIDENCE_BINDING

    first_setup = await workflow_service.record_evidence(
        first.id, setup(first, now, key="first-setup")
    )
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_evidence(
            second.id,
            trigger(second, now, first_setup.evidence_id, key="cross-case-trigger"),
        )
    assert caught.value.code == WorkflowErrorCode.EVIDENCE_BINDING

    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_evidence(
            first.id,
            setup(first, now, key="bad-supersession", supersedes=uuid4()),
        )
    assert caught.value.code == WorkflowErrorCode.EVIDENCE_SUPERSESSION


async def test_expired_setup_and_stale_trigger_cannot_progress(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_case(service, now, trace, "expiry-evidence")
    await service.record_evidence(trade_case.id, atlas(trade_case, now, key="expiry-atlas"))
    await service.record_evidence(trade_case.id, signal(trade_case, now, key="expiry-signal"))
    setup_evidence = await service.record_evidence(
        trade_case.id,
        setup(
            trade_case,
            now,
            key="expiry-setup",
            valid_until=now + timedelta(minutes=1),
        ),
    )
    await service.record_evidence(
        trade_case.id,
        trigger(
            trade_case,
            now,
            setup_evidence.evidence_id,
            key="expiry-trigger",
            valid_until=now + timedelta(minutes=1),
        ),
    )
    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(minutes=1)))
    result = await later.evaluate_trade_case(trade_case.id)
    assert result.status == TradeCaseStatus.BLOCKED
    assert any(blocker.code == "VECTOR_STALE" for blocker in result.blockers)

    second = await open_case(service, now, uuid4(), "stale-trigger")
    long = now + timedelta(minutes=30)
    await service.record_evidence(
        second.id, atlas(second, now, key="stale-trigger-atlas", valid_until=long)
    )
    await service.record_evidence(
        second.id, signal(second, now, key="stale-trigger-signal", valid_until=long)
    )
    setup_evidence = await service.record_evidence(
        second.id,
        setup(second, now, key="stale-trigger-setup", valid_until=long),
    )
    await service.record_evidence(
        second.id,
        trigger(
            second,
            now,
            setup_evidence.evidence_id,
            key="stale-trigger-value",
            valid_until=now + timedelta(minutes=1),
        ),
    )
    stale_trigger = await later.evaluate_trade_case(second.id)
    assert stale_trigger.status == TradeCaseStatus.BLOCKED
    assert stale_trigger.blockers[0].code == "PULSE_STALE"


async def test_anchor_payload_and_risk_replay_remain_bound(workflow_service, now, trace):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace, "anchor-binding")
    anchor_evidence = next(
        item
        for item in await workflow_service.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    assert isinstance(anchor_evidence.payload, LiquidityExecutionPayload)
    assert anchor_evidence.payload.maximum_safe_size_usd == Decimal("2500")
    decision = risk_decision(trade_case, now)
    approved = await workflow_service.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    replay = await workflow_service.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    assert replay == approved
    assert (
        len(
            [
                event
                for event in await workflow_service.timeline(trade_case.id)
                if event.event_type == "RISK_DECISION_BOUND"
            ]
        )
        == 1
    )


async def test_expired_risk_decision_replay_revalidates_case(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(service, now, trace, "expired-risk-replay")
    decision = risk_decision(trade_case, now)
    approved = await service.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    assert approved.status == TradeCaseStatus.RISK_APPROVED

    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(minutes=2)))
    replayed = await later.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    assert replayed.status == TradeCaseStatus.READY_FOR_RISK
    assert replayed.reason_code == "SENTINEL_EVALUATION_REQUIRED"


async def test_simultaneous_evaluators_do_not_duplicate_transitions(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_case(service, now, trace, "evaluator-race")
    before = len(
        [
            event
            for event in await service.timeline(trade_case.id)
            if event.event_type == "STATE_TRANSITION"
        ]
    )
    await asyncio.gather(
        service.evaluate_trade_case(trade_case.id),
        service.evaluate_trade_case(trade_case.id),
    )
    after = len(
        [
            event
            for event in await service.timeline(trade_case.id)
            if event.event_type == "STATE_TRANSITION"
        ]
    )
    assert after == before


async def test_setup_supersession_and_trigger_race_is_safe(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_case(service, now, trace, "setup-trigger-race")
    await service.record_evidence(trade_case.id, atlas(trade_case, now, key="race-atlas"))
    await service.record_evidence(trade_case.id, signal(trade_case, now, key="race-signal"))
    setup_a = await service.record_evidence(trade_case.id, setup(trade_case, now, key="setup-a"))
    results = await asyncio.gather(
        service.record_evidence(
            trade_case.id,
            setup(trade_case, now, key="setup-b", supersedes=setup_a.evidence_id),
        ),
        service.record_evidence(
            trade_case.id,
            trigger(trade_case, now, setup_a.evidence_id, key="trigger-a"),
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(result, Exception) for result in results)
    waiting = await service.get_trade_case(trade_case.id)
    assert waiting.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert waiting.reason_code == "TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP"


async def test_evidence_update_and_risk_decision_race_never_reuses_old_approval(
    workflow_db, now, trace
):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(service, now, trace, "risk-race")
    old_digest = trade_case.risk_input_digest
    old_anchor = next(
        item
        for item in await service.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    assert isinstance(old_anchor.payload, LiquidityExecutionPayload)
    replacement = anchor(
        trade_case,
        now,
        old_anchor.payload.setup_evidence_id,
        old_anchor.payload.trigger_evidence_id,
        key="risk-race-anchor-2",
        supersedes=old_anchor.evidence_id,
    )
    results = await asyncio.gather(
        service.record_evidence(trade_case.id, replacement),
        service.record_risk_decision(
            trade_case.id,
            risk_decision(trade_case, now),
            risk_input_digest=old_digest,
        ),
        return_exceptions=True,
    )
    current = await service.get_trade_case(trade_case.id)
    assert current.risk_input_digest != old_digest
    assert current.status == TradeCaseStatus.READY_FOR_RISK
    if not any(isinstance(result, WorkflowFailure) for result in results):
        assert any(
            event.event_type == "RISK_DECISION_BOUND"
            for event in await service.timeline(trade_case.id)
        )


async def test_postgresql_timeline_is_append_only(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL append-only trigger")
    trade_case = await open_case(
        TradeCaseService(sessions, clock=FixedClock(now)), now, trace, "immutable-timeline"
    )
    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            await session.execute(
                text("DELETE FROM trade_case_events WHERE trade_case_id = :case_id"),
                {"case_id": trade_case.id},
            )


async def stored_binding(sessions, trade_case_id):
    """Read the persisted SENTINEL binding back as its typed domain object."""
    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseRiskBindingRow)
            .where(TradeCaseRiskBindingRow.trade_case_id == trade_case_id)
            .order_by(TradeCaseRiskBindingRow.case_revision.desc())
            .limit(1)
        )
        assert row is not None
        return risk_from_row(row)


async def reach_limited(service, now, trace, key, **decision_kwargs):
    trade_case, setup_evidence = await ready_for_risk(service, now, trace, key=key)
    decision = risk_decision(
        trade_case, now, RiskOutcome.REJECT, Decimal("137.125"), **decision_kwargs
    )
    limited = await service.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    assert limited.status == TradeCaseStatus.RISK_LIMITED
    return limited, setup_evidence, decision


async def test_limited_binding_persists_every_deterministic_cap(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(service, now, trace, key="limited-caps")
    decision = risk_decision(
        trade_case,
        now,
        RiskOutcome.REJECT,
        Decimal("137.125"),
        reason_codes=("MAX_POSITION_SIZE", "INSUFFICIENT_CASH"),
        position_limit=Decimal("2500.5"),
        slippage=Decimal("75.25"),
    )
    limited = await service.record_risk_decision(
        trade_case.id, decision, risk_input_digest=trade_case.risk_input_digest
    )
    assert limited.status == TradeCaseStatus.RISK_LIMITED

    binding = await stored_binding(sessions, trade_case.id)
    # The Phase 0 verdict is preserved verbatim; LIMITED never rewrites a
    # rejection into an approval.
    assert binding.outcome == RiskOutcome.REJECT
    assert binding.authorization == RiskAuthorization.LIMITED
    assert binding.reason_codes == ("MAX_POSITION_SIZE", "INSUFFICIENT_CASH")
    # Both sizing constraints survive independently and exactly, as Decimal.
    assert binding.position_size_limit_usd == Decimal("2500.5")
    assert binding.max_additional_notional_usd == Decimal("137.125")
    assert binding.position_size_limit_usd != binding.max_additional_notional_usd
    assert binding.max_slippage_bps == Decimal("75.25")
    assert all(
        isinstance(value, Decimal)
        for value in (
            binding.position_size_limit_usd,
            binding.max_additional_notional_usd,
            binding.max_slippage_bps,
        )
    )
    assert binding.risk_input_digest == trade_case.risk_input_digest
    assert binding.trade_case_id == trade_case.id
    assert binding.correlation_id == trade_case.correlation_id
    assert binding.risk_decision_id == decision.id
    assert binding.evaluated_at == decision.evaluated_at
    assert binding.expires_at == decision.expires_at
    # The untyped payload stays audit provenance only.
    assert binding.decision_payload["outcome"] == RiskOutcome.REJECT.value


@pytest.mark.parametrize(
    "key,outcome,capacity,authorized",
    [
        ("approved", RiskOutcome.APPROVE, Decimal("2500"), TradeCaseStatus.RISK_APPROVED),
        ("limited", RiskOutcome.REJECT, Decimal("137.125"), TradeCaseStatus.RISK_LIMITED),
    ],
)
async def test_stale_anchor_revokes_every_authorization(
    workflow_db, now, trace, key, outcome, capacity, authorized
):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(
        service, now, trace, key=f"stale-{key}", anchor_valid_until=now + timedelta(seconds=30)
    )
    result = await service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now, outcome, capacity),
        risk_input_digest=trade_case.risk_input_digest,
    )
    assert result.status == authorized

    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(minutes=1)))
    revoked = await later.evaluate_trade_case(trade_case.id)
    assert revoked.status == TradeCaseStatus.BLOCKED
    assert revoked.blockers[0].code == "ANCHOR_STALE_EXECUTION_EVIDENCE"


@pytest.mark.parametrize(
    "key,outcome,capacity,authorized",
    [
        ("approved", RiskOutcome.APPROVE, Decimal("2500"), TradeCaseStatus.RISK_APPROVED),
        ("limited", RiskOutcome.REJECT, Decimal("137.125"), TradeCaseStatus.RISK_LIMITED),
    ],
)
async def test_superseded_anchor_changes_digest_and_revokes(
    workflow_service, now, trace, key, outcome, capacity, authorized
):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace, key=f"digest-{key}")
    old_digest = trade_case.risk_input_digest
    result = await workflow_service.record_risk_decision(
        trade_case.id,
        risk_decision(trade_case, now, outcome, capacity),
        risk_input_digest=old_digest,
    )
    assert result.status == authorized

    old_anchor = next(
        item
        for item in await workflow_service.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    assert isinstance(old_anchor.payload, LiquidityExecutionPayload)
    await workflow_service.record_evidence(
        trade_case.id,
        anchor(
            trade_case,
            now,
            old_anchor.payload.setup_evidence_id,
            old_anchor.payload.trigger_evidence_id,
            key=f"digest-{key}-anchor-2",
            supersedes=old_anchor.evidence_id,
        ),
    )
    revoked = await workflow_service.get_trade_case(trade_case.id)
    assert revoked.risk_input_digest != old_digest
    assert revoked.status == TradeCaseStatus.READY_FOR_RISK
    assert revoked.reason_code == "SENTINEL_EVALUATION_REQUIRED"


async def test_valid_limited_replay_is_idempotent(workflow_service, now, trace):
    limited, _, decision = await reach_limited(workflow_service, now, trace, "limited-replay")
    replay = await workflow_service.record_risk_decision(
        limited.id, decision, risk_input_digest=limited.risk_input_digest
    )
    assert replay == limited
    bound = [
        event
        for event in await workflow_service.timeline(limited.id)
        if event.event_type == "RISK_DECISION_BOUND"
    ]
    assert len(bound) == 1
    assert bound[0].payload["authorization"] == RiskAuthorization.LIMITED.value


async def test_expired_limited_replay_never_resurrects_authorization(workflow_db, now, trace):
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    limited, _, decision = await reach_limited(service, now, trace, "limited-expiry")

    later = TradeCaseService(sessions, clock=FixedClock(now + timedelta(minutes=2)))
    replayed = await later.record_risk_decision(
        limited.id, decision, risk_input_digest=limited.risk_input_digest
    )
    assert replayed.status == TradeCaseStatus.READY_FOR_RISK
    assert replayed.reason_code == "SENTINEL_EVALUATION_REQUIRED"
    # Plain re-evaluation must not drift back either.
    assert (await later.evaluate_trade_case(limited.id)).status == TradeCaseStatus.READY_FOR_RISK


async def test_conflicting_limited_replay_is_rejected(workflow_service, now, trace):
    limited, _, decision = await reach_limited(workflow_service, now, trace, "limited-conflict")
    tampered = decision.model_copy(update={"max_additional_notional_usd": Decimal("999999")})
    assert tampered.id == decision.id
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_risk_decision(
            limited.id, tampered, risk_input_digest=limited.risk_input_digest
        )
    assert caught.value.code == WorkflowErrorCode.IDEMPOTENCY_CONFLICT


async def test_limited_decision_against_a_superseded_digest_is_rejected(
    workflow_service, now, trace
):
    trade_case, setup_evidence = await ready_for_risk(workflow_service, now, trace, "stale-digest")
    old_digest = trade_case.risk_input_digest
    await workflow_service.record_evidence(
        trade_case.id,
        setup(trade_case, now, key="stale-digest-setup-2", supersedes=setup_evidence.evidence_id),
    )
    with pytest.raises(WorkflowFailure) as caught:
        await workflow_service.record_risk_decision(
            trade_case.id,
            risk_decision(trade_case, now, RiskOutcome.REJECT, Decimal("137.125")),
            risk_input_digest=old_digest,
        )
    assert caught.value.code == WorkflowErrorCode.RISK_BINDING


async def test_evidence_update_races_a_limited_decision_safely(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(service, now, trace, "limited-race")
    old_digest = trade_case.risk_input_digest
    old_anchor = next(
        item
        for item in await service.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    assert isinstance(old_anchor.payload, LiquidityExecutionPayload)
    results = await asyncio.gather(
        service.record_evidence(
            trade_case.id,
            anchor(
                trade_case,
                now,
                old_anchor.payload.setup_evidence_id,
                old_anchor.payload.trigger_evidence_id,
                key="limited-race-anchor-2",
                supersedes=old_anchor.evidence_id,
            ),
        ),
        service.record_risk_decision(
            trade_case.id,
            risk_decision(trade_case, now, RiskOutcome.REJECT, Decimal("137.125")),
            risk_input_digest=old_digest,
        ),
        return_exceptions=True,
    )
    current = await service.get_trade_case(trade_case.id)
    assert current.risk_input_digest != old_digest
    # Whichever order the two commands land in, the superseded digest can never
    # leave the case sitting on a LIMITED authorization.
    assert current.status == TradeCaseStatus.READY_FOR_RISK
    assert len(results) == 2


async def test_parallel_evaluation_after_limit_adds_no_transition(workflow_db, now, trace):
    engine, sessions = workflow_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    service = TradeCaseService(sessions, clock=FixedClock(now))
    limited, _, _ = await reach_limited(service, now, trace, "limited-evaluator-race")
    before = len(
        [
            event
            for event in await service.timeline(limited.id)
            if event.event_type == "STATE_TRANSITION"
        ]
    )
    await asyncio.gather(
        service.evaluate_trade_case(limited.id),
        service.evaluate_trade_case(limited.id),
    )
    after = [
        event
        for event in await service.timeline(limited.id)
        if event.event_type == "STATE_TRANSITION"
    ]
    assert len(after) == before
    assert (await service.get_trade_case(limited.id)).status == TradeCaseStatus.RISK_LIMITED


async def test_scenario_g_resizable_sentinel_limit(workflow_db, now, trace):
    """Scenario G: discovery to a resizable SENTINEL cap, then revocation."""
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))

    # Opening records ORBIT discovery evidence, so the case settles straight
    # onto the outstanding ATLAS, SIGNAL and VECTOR requirements.
    trade_case = await open_case(service, now, trace, "scenario-g")
    assert trade_case.status == TradeCaseStatus.EVIDENCE_PENDING
    assert {blocker.role for blocker in trade_case.blockers} == {
        AgentRole.ATLAS,
        AgentRole.SIGNAL,
        AgentRole.VECTOR,
    }

    await service.record_evidence(trade_case.id, atlas(trade_case, now, key="scenario-g-atlas"))
    await service.record_evidence(trade_case.id, signal(trade_case, now, key="scenario-g-signal"))
    setup_evidence = await service.record_evidence(
        trade_case.id, setup(trade_case, now, key="scenario-g-setup")
    )
    assert (await service.get_trade_case(trade_case.id)).status == TradeCaseStatus.READY_FOR_TRIGGER

    trigger_evidence = await service.record_evidence(
        trade_case.id,
        trigger(trade_case, now, setup_evidence.evidence_id, key="scenario-g-trigger"),
    )
    # The evaluator settles to a fixed point, so the case passes through
    # TRIGGERED and stops on the outstanding ANCHOR assessment.
    triggered = await service.get_trade_case(trade_case.id)
    assert triggered.status == TradeCaseStatus.EXECUTION_EVIDENCE_PENDING
    assert [
        event.payload["to_status"]
        for event in await service.timeline(trade_case.id)
        if event.event_type == "STATE_TRANSITION"
    ] == [
        TradeCaseStatus.EVIDENCE_PENDING.value,
        TradeCaseStatus.READY_FOR_TRIGGER.value,
        TradeCaseStatus.TRIGGERED.value,
        TradeCaseStatus.EXECUTION_EVIDENCE_PENDING.value,
    ]

    await service.record_evidence(
        trade_case.id,
        anchor(
            trade_case,
            now,
            setup_evidence.evidence_id,
            trigger_evidence.evidence_id,
            key="scenario-g-anchor",
        ),
    )
    ready = await service.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK

    # SENTINEL rejects the requested size only, and leaves a bounded capacity.
    limited = await service.record_risk_decision(
        ready.id,
        risk_decision(
            ready,
            now,
            RiskOutcome.REJECT,
            Decimal("137.125"),
            reason_codes=("MAX_POSITION_SIZE",),
            position_limit=Decimal("2500"),
        ),
        risk_input_digest=ready.risk_input_digest,
    )
    assert limited.status == TradeCaseStatus.RISK_LIMITED
    assert limited.reason_code == "SENTINEL_SIZE_LIMITED"
    binding = await stored_binding(sessions, limited.id)
    assert binding.outcome == RiskOutcome.REJECT
    assert binding.authorization == RiskAuthorization.LIMITED
    assert binding.max_additional_notional_usd == Decimal("137.125")
    assert binding.position_size_limit_usd == Decimal("2500")

    # Nothing in Phase 2A executes: there is no order, fill or position event.
    assert {event.event_type for event in await service.timeline(limited.id)} <= {
        "CASE_OPENED",
        "TASK_CREATED",
        "TASK_COMPLETED",
        "TASK_STATUS_CHANGED",
        "EVIDENCE_RECORDED",
        "EVIDENCE_SUPERSEDED",
        "BLOCKERS_CHANGED",
        "STATE_TRANSITION",
        "CASE_REEVALUATED",
        "RISK_DECISION_BOUND",
    }

    # Safety evidence changes: the limit stops authorizing anything.
    await service.record_evidence(
        limited.id,
        atlas(
            limited,
            now,
            key="scenario-g-atlas-2",
            status=EvidenceStatus.UNKNOWN,
            supersedes=next(
                item.evidence_id
                for item in await service.evidence(limited.id)
                if item.evidence_type == EvidenceType.ONCHAIN
            ),
        ),
    )
    revoked = await service.get_trade_case(limited.id)
    assert revoked.status == TradeCaseStatus.BLOCKED
    assert revoked.blockers[0].code == "ATLAS_UNKNOWN"


async def test_scenario_g_neighbour_true_rejection_is_not_rescued(workflow_db, now, trace):
    """A non-sizing blocker rejects even when sizing guidance stays positive."""
    _, sessions = workflow_db
    service = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case, _ = await ready_for_risk(service, now, trace, "scenario-g-reject")
    rejected = await service.record_risk_decision(
        trade_case.id,
        risk_decision(
            trade_case,
            now,
            RiskOutcome.REJECT,
            Decimal("137.125"),
            reason_codes=("MAX_POSITION_SIZE", "HOLDER_CONCENTRATION_LIMIT"),
        ),
        risk_input_digest=trade_case.risk_input_digest,
    )
    assert rejected.status == TradeCaseStatus.RISK_REJECTED
    assert rejected.reason_code == "SENTINEL_REJECTED"
    binding = await stored_binding(sessions, rejected.id)
    assert binding.authorization == RiskAuthorization.REJECTED
    assert binding.max_additional_notional_usd == Decimal("137.125")


async def test_persisted_digest_is_independent_of_retrieval_order(workflow_service, now, trace):
    trade_case, _ = await ready_for_risk(workflow_service, now, trace, "digest-order")
    stored = await workflow_service.evidence(trade_case.id)
    for seed in range(8):
        shuffled = list(stored)
        random.Random(seed).shuffle(shuffled)
        recomputed = risk_input_digest(trade_case, active_evidence(tuple(shuffled)))
        assert recomputed == trade_case.risk_input_digest
