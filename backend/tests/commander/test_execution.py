"""The full lifecycle a control plane coordinates, end to end, and where it stops.

Drives one real TradeCase through every specialist stage against PostgreSQL, and
asserts at each step that COMMANDER read the workflow's verdict rather than
forming its own — and that at the end, where a risk request would go, it reports
an architectural gap instead of inventing the number that would fill it.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.commander.decision import decide
from src.orchestration.commander.models import CommanderDisposition, CommanderReason
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1
from src.orchestration.workflow.models import (
    EvidenceType,
    LiquidityExecutionPayload,
    TradeCaseStatus,
)
from tests.commander.conftest import (
    build_stack,
    inject_pre_trigger_evidence,
    inject_trigger,
    open_case,
    record,
)

pytestmark = pytest.mark.usefixtures("worker_db")


async def inject_anchor_evidence(cases, trade_case, now, setup_evidence, **kw):
    """ANCHOR's finding, in the legacy scalar shape the workflow accepts."""
    trigger = next(
        item
        for item in await cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.TRIGGER
    )
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        LiquidityExecutionPayload(
            setup_evidence_id=setup_evidence.evidence_id,
            trigger_evidence_id=trigger.evidence_id,
            quoted_price=Decimal("1"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
            price_impact_bps=Decimal("20"),
            # A tested capacity, deliberately a round number a sizing strategy
            # would find attractive. Nothing reads it.
            maximum_safe_size_usd=Decimal("50000"),
            routing_provenance="quoted-route-v1",
        ),
        key=f"cmd-anchor-{trade_case.id}",
        **kw,
    )


async def observe(reader, trade_case, now):
    context = await reader.commander_context(trade_case.id, uuid4())
    return context, decide(context, now, COMMANDER_CONTROL_V1)


async def test_scenario_95_the_whole_lifecycle_up_to_the_risk_boundary(worker_db, now, trace):
    """§95. One case, every stage, and a coordinator that never oversteps.

    At each step the control plane is asked what to do and answers with the
    domain that owns the next move — never by evaluating the thing itself.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-e2e")

    # Opened: ORBIT's discovery is recorded, the rest is pending.
    context, decision = await observe(reader, trade_case, now)
    assert context.status == TradeCaseStatus.EVIDENCE_PENDING
    assert decision.disposition == CommanderDisposition.AWAIT_SPECIALISTS

    # ATLAS, SIGNAL, VECTOR.
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    context, decision = await observe(reader, trade_case, now)
    assert context.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert decision.disposition == CommanderDisposition.AWAIT_TRIGGER

    # PULSE.
    await inject_trigger(runtime.cases, trade_case, now, setup)
    context, decision = await observe(reader, trade_case, now)
    assert decision.disposition == CommanderDisposition.AWAIT_EXECUTION_EVIDENCE

    # ANCHOR.
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)
    context, decision = await observe(reader, trade_case, now)

    # Everything the workflow requires is present, and the control plane stops.
    assert context.status == TradeCaseStatus.READY_FOR_RISK
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING

    # And the case is exactly where the evaluator put it. Nothing was forced.
    final = await runtime.cases.get_trade_case(trade_case.id)
    assert final.status == TradeCaseStatus.READY_FOR_RISK
    assert final.risk_input_digest is not None


async def test_the_control_plane_never_wrote_anything_during_that_lifecycle(worker_db, now, trace):
    """Observation changes nothing. Asserted by revision, which counts writes."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-readonly")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    before = await runtime.cases.get_trade_case(trade_case.id)
    for _ in range(5):
        await observe(reader, trade_case, now)
    after = await runtime.cases.get_trade_case(trade_case.id)

    assert after.revision == before.revision
    assert after.status == before.status
    assert after.updated_at == before.updated_at


async def test_scenario_y_repeated_observation_produces_an_identical_digest(worker_db, now, trace):
    """§58, §93. Same state, same digest — so no churn and no audit spam."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-churn")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    digests = set()
    for _ in range(4):
        context, _ = await observe(reader, trade_case, now)
        digests.add(context.context_digest)
    assert len(digests) == 1


async def test_a_meaningful_change_moves_the_digest(worker_db, now, trace):
    """The control: the digest is not merely constant, it tracks real change."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-digest-moves")
    before, _ = await observe(reader, trade_case, now)
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    after, _ = await observe(reader, trade_case, now)
    assert before.context_digest != after.context_digest
