"""What the control plane concludes, and everything it declines to conclude.

Most of these assert an absence. That is the shape of the component: a
coordinator's risk is not that it decides badly but that it decides at all —
about liquidity, about safety, about size — in a system where four other things
already decided each of those, and where its own reasoning would be the only one
nobody recorded.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.commander.decision import decide
from src.orchestration.commander.models import CommanderDisposition, CommanderReason
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1
from src.orchestration.workflow.models import EvidenceType, TradeCaseStatus
from tests.commander.conftest import (
    build_stack,
    context_for,
    open_case,
    pre_trigger_evidence,
    record,
    triggered,
)
from tests.fuse.conftest import onchain

pytestmark = pytest.mark.usefixtures("worker_db")

# A fixed instant for the synthetic contexts below, which touch no database.
NOW_FOR_SYNTHETIC = datetime(2026, 9, 14, 12, tzinfo=UTC)


async def decided(sessions, now, trace, key, *, build=None, kill_switch=False):
    runtime, reader = build_stack(sessions, now, kill_switch=kill_switch)
    trade_case = await open_case(runtime.cases, now, trace, key)
    if build is not None:
        await build(runtime, trade_case)
    context = await context_for(runtime, reader, trade_case)
    return context, decide(context, now, COMMANDER_CONTROL_V1)


# ------------------------------------------------------- A: evidence pending


async def test_scenario_a_a_case_missing_evidence_waits_for_specialists(worker_db, now, trace):
    """No risk request, no status write, no failure. Waiting is the answer."""
    _, sessions = worker_db
    context, decision = await decided(sessions, now, trace, "cmd-pending")

    assert context.status == TradeCaseStatus.EVIDENCE_PENDING
    assert decision.disposition == CommanderDisposition.AWAIT_SPECIALISTS
    assert decision.reason_code == CommanderReason.REQUIRED_EVIDENCE_PENDING
    assert decision.is_progression is False


async def test_a_partially_evidenced_case_is_not_nearly_ready(worker_db, now, trace):
    """There is no "most evidence is present" branch, and never will be."""
    _, sessions = worker_db

    async def build(runtime, trade_case):
        await record(
            runtime.cases,
            trade_case,
            now,
            AgentRole.ATLAS,
            EvidenceType.ONCHAIN,
            onchain(),
            key="cmd-partial-atlas",
        )

    _, decision = await decided(sessions, now, trace, "cmd-partial", build=build)
    assert decision.disposition == CommanderDisposition.AWAIT_SPECIALISTS


# ----------------------------------------------------------------- B: blocked


async def test_scenario_b_a_blocked_case_is_not_something_coordination_fixes(worker_db, now, trace):
    """ATLAS measured a failure. No coordinator opinion outranks that."""
    _, sessions = worker_db

    async def build(runtime, trade_case):
        await pre_trigger_evidence(runtime.cases, trade_case, now, onchain=onchain(holder="FAIL"))

    context, decision = await decided(sessions, now, trace, "cmd-blocked", build=build)

    assert context.status == TradeCaseStatus.BLOCKED
    assert decision.disposition == CommanderDisposition.HALTED
    assert decision.reason_code == CommanderReason.SAFETY_EVIDENCE_BLOCKED


async def test_an_advisory_synthesis_cannot_unblock_anything(worker_db, now, trace):
    """§99. FUSE has no blocker authority, and the decision never reads it.

    The advisory field exists for an operator to look at. Proving it is inert
    means proving the decision is identical whether or not it is present.
    """
    _, sessions = worker_db

    async def build(runtime, trade_case):
        await pre_trigger_evidence(runtime.cases, trade_case, now, onchain=onchain(holder="FAIL"))

    context, decision = await decided(sessions, now, trace, "cmd-advisory", build=build)
    without = decide(context.model_copy(update={"advisory": None}), now, COMMANDER_CONTROL_V1)
    assert decision.disposition == without.disposition
    assert decision.reason_code == without.reason_code


# ------------------------------------------------- C, D: other roles' domains


async def test_scenario_c_a_ready_case_waits_for_pulse_not_for_a_price_check(worker_db, now, trace):
    """COMMANDER never reads a price. PULSE owns the watch."""
    _, sessions = worker_db

    async def build(runtime, trade_case):
        await pre_trigger_evidence(runtime.cases, trade_case, now)

    context, decision = await decided(sessions, now, trace, "cmd-trigger", build=build)

    assert context.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert decision.disposition == CommanderDisposition.AWAIT_TRIGGER
    assert decision.reason_code == CommanderReason.WAITING_FOR_TRIGGER


async def test_scenario_d_a_triggered_case_waits_for_anchor_not_for_a_quote(worker_db, now, trace):
    """COMMANDER never quotes. ANCHOR owns execution liquidity."""
    _, sessions = worker_db

    async def build(runtime, trade_case):
        setup = await pre_trigger_evidence(runtime.cases, trade_case, now)
        await triggered(runtime.cases, trade_case, now, setup)

    context, decision = await decided(sessions, now, trace, "cmd-triggered", build=build)

    assert context.status in (
        TradeCaseStatus.TRIGGERED,
        TradeCaseStatus.EXECUTION_EVIDENCE_PENDING,
    )
    assert decision.disposition == CommanderDisposition.AWAIT_EXECUTION_EVIDENCE
    assert decision.reason_code == CommanderReason.WAITING_FOR_EXECUTION_EVIDENCE


# ------------------------------------------ L, M: system stops outrank all


@pytest.mark.parametrize("status_build", ["pending", "ready"])
async def test_scenario_m_a_kill_switch_outranks_every_case_level_eligibility(
    worker_db, now, trace, status_build
):
    """Checked first, so no later branch can reach past it."""
    _, sessions = worker_db

    async def build(runtime, trade_case):
        if status_build == "ready":
            await pre_trigger_evidence(runtime.cases, trade_case, now)

    _, decision = await decided(
        sessions, now, trace, f"cmd-kill-{status_build}", build=build, kill_switch=True
    )
    assert decision.disposition == CommanderDisposition.PAUSED
    assert decision.reason_code == CommanderReason.SYSTEM_PAUSED


async def test_scenario_l_a_recorded_system_pause_stops_progression(worker_db, now, trace):
    """§38. The durable pause a SENTINEL PAUSE_SYSTEM verdict records.

    Supplied through a port rather than queried directly, because that pause
    lives in the Phase 0 accounting subsystem — one the TradeCase workflow has
    no link to and whose schema a workflow-only deployment does not carry.
    Making the dependency explicit is what keeps it from being a query that
    merely happens to work wherever both schemas coexist.
    """
    from src.core.clock import FixedClock
    from src.orchestration.commander.context import CommanderContextReader
    from src.orchestration.workflow.service import TradeCaseService

    class Paused:
        async def system_paused(self) -> bool:
            return True

    _, sessions = worker_db
    clock = FixedClock(now)
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-paused")
    reader = CommanderContextReader(
        cases=TradeCaseService(sessions, clock=clock),
        sessions=sessions,
        clock=clock,
        pause=Paused(),
    )
    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, now, COMMANDER_CONTROL_V1)

    assert context.controls.account_paused is True
    assert decision.disposition == CommanderDisposition.PAUSED
    assert decision.reason_code == CommanderReason.SYSTEM_PAUSED


def test_a_pause_port_can_only_be_observed_never_lifted():
    """A control plane may see a stop and must never be able to clear one."""
    from src.orchestration.commander.context import SystemPausePort

    methods = {
        name
        for name in dir(SystemPausePort)
        if not name.startswith("_") and callable(getattr(SystemPausePort, name, None))
    }
    assert methods == {"system_paused"}


# --------------------------------- H, K, R: risk verdicts and terminal cases


def synthetic(status, *, risk=None, controls=None, digest="a" * 64):
    """A context at a given status, for the branches a real case is slow to reach."""
    from src.orchestration.commander.models import CommanderContext, SystemControls

    return CommanderContext(
        trade_case_id=uuid4(),
        task_id=uuid4(),
        workflow_version="trade-case-v1",
        policy_version=COMMANDER_CONTROL_V1.version,
        status=status,
        revision=4,
        reason_code="TEST",
        controls=controls
        or SystemControls(kill_switch=False, account_paused=False, trading_mode="PAPER"),
        risk=risk,
        observed_at=NOW_FOR_SYNTHETIC,
        context_digest=digest,
    )


def risk_state(authorization, *, matches: bool):
    from src.orchestration.commander.models import RiskState
    from src.risk.authorization import RiskAuthorization

    return RiskState(
        binding_id=uuid4(),
        risk_decision_id=uuid4(),
        authorization=RiskAuthorization(authorization),
        risk_input_digest="b" * 64,
        matches_current_inputs=matches,
        expires_at=NOW_FOR_SYNTHETIC,
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (TradeCaseStatus.RISK_REJECTED, CommanderReason.RISK_REJECTED),
        (TradeCaseStatus.EXPIRED, CommanderReason.TRADE_CASE_TERMINAL),
        (TradeCaseStatus.CANCELLED, CommanderReason.TRADE_CASE_TERMINAL),
    ],
)
def test_scenario_r_a_terminal_case_progresses_no_further(status, reason):
    """§44, §86. Including a rejection, which is an answer rather than a setback."""
    decision = decide(synthetic(status), NOW_FOR_SYNTHETIC, COMMANDER_CONTROL_V1)
    assert decision.disposition == CommanderDisposition.HALTED
    assert decision.reason_code == reason
    assert decision.is_progression is False


def test_scenario_k_a_rejection_is_never_retried_into_an_approval():
    """§36, §79. There is no path that reconsiders a rejection.

    Not a loop that gives up after N attempts — no loop at all. The only thing
    that could make a fresh assessment appropriate is the canonical risk inputs
    genuinely changing, and that arrives as a different case state.
    """
    rejected = synthetic(TradeCaseStatus.RISK_REJECTED, risk=risk_state("REJECTED", matches=True))
    for _ in range(3):
        decision = decide(rejected, NOW_FOR_SYNTHETIC, COMMANDER_CONTROL_V1)
        assert decision.disposition == CommanderDisposition.HALTED
        assert decision.reason_code == CommanderReason.RISK_REJECTED


@pytest.mark.parametrize("status", [TradeCaseStatus.RISK_APPROVED, TradeCaseStatus.RISK_LIMITED])
def test_scenario_h_an_authorization_covering_the_current_inputs_is_not_reasked(status):
    """§33, §76. Idempotent by state: the same evidence needs no second verdict."""
    decision = decide(
        synthetic(status, risk=risk_state("APPROVED", matches=True)),
        NOW_FOR_SYNTHETIC,
        COMMANDER_CONTROL_V1,
    )
    assert decision.disposition == CommanderDisposition.RISK_CURRENT
    assert decision.reason_code == CommanderReason.RISK_AUTHORIZATION_CURRENT


@pytest.mark.parametrize("status", [TradeCaseStatus.RISK_APPROVED, TradeCaseStatus.RISK_LIMITED])
def test_scenario_i_an_authorization_for_other_evidence_does_not_carry_over(status):
    """§34, §77. Safety evidence changed, so the old verdict describes another case.

    It is not treated as a weaker approval or a stale one to be extended. The
    control plane falls through to the same honest stop as an unassessed case —
    which is to say it does not continue on the old APPROVE at all.
    """
    decision = decide(
        synthetic(status, risk=risk_state("APPROVED", matches=False)),
        NOW_FOR_SYNTHETIC,
        COMMANDER_CONTROL_V1,
    )
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING


def test_a_rejected_authorization_at_the_risk_gate_is_not_read_as_current():
    """A REJECTED binding never satisfies "an authorization already covers this"."""
    decision = decide(
        synthetic(TradeCaseStatus.READY_FOR_RISK, risk=risk_state("REJECTED", matches=True)),
        NOW_FOR_SYNTHETIC,
        COMMANDER_CONTROL_V1,
    )
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY


def test_an_unmapped_status_is_not_a_licence_to_improvise():
    """Fails toward waiting, never toward progression."""
    decision = decide(
        synthetic(TradeCaseStatus.DISCOVERED), NOW_FOR_SYNTHETIC, COMMANDER_CONTROL_V1
    )
    assert decision.disposition == CommanderDisposition.AWAIT_SPECIALISTS


def test_every_reason_has_a_statement_and_every_statement_a_reason():
    from src.orchestration.commander.decision import STATEMENTS

    assert set(STATEMENTS) == set(CommanderReason)


@pytest.mark.parametrize(
    "broken",
    [
        {"enabled_chains": frozenset()},
        {"max_candidate_age": timedelta(0)},
        {"max_cases_per_cycle": 0},
        {"max_cases_per_cycle": 999},
        {"case_lifetime": timedelta(0)},
    ],
)
def test_an_unsafe_control_policy_refuses_to_exist(broken):
    """Bounds are facts about the configuration, not runtime branches."""
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(COMMANDER_CONTROL_V1, **broken)
