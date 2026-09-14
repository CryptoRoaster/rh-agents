"""FUSE stays advisory, SIGNAL stays non-risk-binding, and stale context cannot act.

Three Phase 2F/2K guarantees that a control plane is the natural place to break.
It is the first component that sees everything at once, so it is the first that
could quietly let an advisory reading gate a decision, or let a sentiment change
ripple into risk by reacting to it.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.commander.context import context_digest
from src.orchestration.commander.models import CommanderDisposition, CommanderReason
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import EvidenceType, TradeCaseStatus
from tests.commander.conftest import (
    build_stack,
    inject_pre_trigger_evidence,
    inject_trigger,
    open_case,
    record,
)
from tests.commander.test_execution import inject_anchor_evidence, observe
from tests.fuse.conftest import onchain, sentiment

pytestmark = pytest.mark.usefixtures("worker_db")


async def synthesis_for(sessions, runtime, trade_case, now, trace):
    """Record a real FUSE synthesis through the real worker."""
    from src.agents.fuse.context import FuseContextReader
    from src.agents.fuse.handler import FuseWorkerHandler
    from src.core.clock import FixedClock
    from src.orchestration.worker.capabilities import FuseCapabilities
    from tests.fuse.test_workflow import Fixed, NoSubmit, lease_for

    reader = FuseContextReader(cases=runtime.cases, clock=FixedClock(now))
    context = await reader.synthesis_context(trade_case.id, uuid4())
    lease = lease_for(trade_case, context.task_id, now, trace)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    await runtime.cases.record_evidence(trade_case.id, report.submission)
    return report


# ------------------------------------------------- P, Q: FUSE is advisory


async def test_scenario_p_an_absent_synthesis_blocks_nothing_and_implies_nothing(
    worker_db, now, trace
):
    """§84. Absence is not consensus, and it is not an obstacle either."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-nofuse")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    context, decision = await observe(reader, trade_case, now)

    assert context.advisory is None
    # The canonical workflow reached its own conclusion without it.
    assert context.status == TradeCaseStatus.READY_FOR_RISK
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    # And nothing inferred a positive reading from the silence.
    assert decision.reason_code is not CommanderReason.RISK_AUTHORIZATION_CURRENT


async def test_scenario_q_a_synthesis_of_superseded_evidence_is_marked_not_current(
    worker_db, now, trace
):
    """§10, §85. A stale synthesis describes a case that no longer exists.

    It is carried with the fact computed rather than handed over with a caveat
    a reader might skip — and the decision never consults it either way.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-stalefuse")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await synthesis_for(sessions, runtime, trade_case, now, trace)

    fresh, _ = await observe(reader, trade_case, now)
    assert fresh.advisory is not None
    assert fresh.advisory.describes_current_inputs is True

    # SIGNAL is replaced; the synthesis now describes an evidence set the case
    # has left behind.
    later = now + timedelta(minutes=5)
    runtime, reader = build_stack(sessions, later)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE"),
        key="cmd-stale-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    stale, decision = await observe(reader, trade_case, later)

    assert stale.advisory is not None
    assert stale.advisory.describes_current_inputs is False
    # Canonical source evidence governs regardless.
    assert decision.disposition == CommanderDisposition.AWAIT_TRIGGER


async def test_the_advisory_field_is_excluded_from_the_context_digest(worker_db, now, trace):
    """§19. FUSE must not be able to invalidate a COMMANDER action.

    Including the synthesis in the digest would give the advisory layer exactly
    that power: change the advisory, change the digest, invalidate the decision.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-advisory-digest")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    before, _ = await observe(reader, trade_case, now)
    await synthesis_for(sessions, runtime, trade_case, now, trace)
    after, _ = await observe(reader, trade_case, now)

    assert before.advisory is None
    assert after.advisory is not None
    assert before.context_digest == after.context_digest


# --------------------------------- I, J: risk transitivity through the plane


async def test_scenario_j_a_sentiment_change_moves_neither_risk_digest_nor_decision(
    worker_db, now, trace
):
    """§35, §78. SIGNAL is not risk-binding, and reacting to it must not make it so."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-signal-risk")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    before, first = await observe(reader, trade_case, now)
    case = await runtime.cases.get_trade_case(trade_case.id)
    risk_before = risk_input_digest(
        case, active_evidence(await runtime.cases.evidence(trade_case.id))
    )

    later = now + timedelta(minutes=5)
    runtime, reader = build_stack(sessions, later)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE"),
        key="cmd-signal-risk-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    after, second = await observe(reader, trade_case, later)
    risk_after = risk_input_digest(
        await runtime.cases.get_trade_case(trade_case.id),
        active_evidence(await runtime.cases.evidence(trade_case.id)),
    )

    assert risk_before == risk_after, "sentiment must not reach the risk snapshot"
    assert first.reason_code == second.reason_code
    # The context digest does move — coordination noticed — but risk did not.
    assert before.context_digest != after.context_digest


async def test_scenario_i_a_safety_change_moves_the_risk_digest(worker_db, now, trace):
    """§34, §77. The control: an old authorization stops describing the case."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-safety-risk")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    case = await runtime.cases.get_trade_case(trade_case.id)
    before = risk_input_digest(case, active_evidence(await runtime.cases.evidence(trade_case.id)))

    later = now + timedelta(minutes=5)
    runtime, reader = build_stack(sessions, later)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    await record(
        runtime.cases,
        trade_case,
        later,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        onchain(verdict="CLEAR"),
        key="cmd-safety-atlas-2",
        supersedes_id=current[EvidenceType.ONCHAIN].evidence_id,
    )
    after = risk_input_digest(
        await runtime.cases.get_trade_case(trade_case.id),
        active_evidence(await runtime.cases.evidence(trade_case.id)),
    )
    assert before != after


# ------------------------------------------------------- F: stale context


async def test_scenario_f_a_decision_carries_the_state_it_was_reached_from(worker_db, now, trace):
    """§20, §74. A decision derived from C1 is only valid for C1.

    There is no action port in this phase, so nothing can yet act on a stale
    decision — but the fencing material travels with the decision now, so the
    check exists before the thing that would need it.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-stale-ctx")
    first, decision = await observe(reader, trade_case, now)
    assert decision.context_digest == first.context_digest

    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    second, _ = await observe(reader, trade_case, now)

    assert second.context_digest != decision.context_digest
    # A server holding the new state can tell the old decision no longer applies.
    assert decision.context_digest != second.context_digest


def test_the_digest_ignores_who_looked_and_when(worker_db=None):
    """§19. Lease, worker, attempt and read time are excluded by construction."""
    from src.orchestration.commander.models import SystemControls

    controls = SystemControls(kill_switch=False, account_paused=False, trading_mode="PAPER")
    shared = dict(
        trade_case_id=uuid4(),
        workflow_version="trade-case-v1",
        policy_version="commander-control-v1",
        status="EVIDENCE_PENDING",
        revision=3,
        evidence=(),
        tasks=(),
        risk=None,
        controls=controls,
        current_risk_input_digest=None,
    )
    assert context_digest(**shared) == context_digest(**shared)
