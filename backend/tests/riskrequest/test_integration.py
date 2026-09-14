"""A complete case, the real SENTINEL, and a durable binding.

Everything below runs the production path: the real workflow service against a
real database, the real completeness check, the real sizing calculation and
`src.risk.engine.evaluate` itself. What is stubbed is the market feed's single
read and the stop source — the two ports, supplied as values rather than as
providers this suite is not allowed to reach.

The evidence is fixture evidence. No specialist worker runs here except through
ATLAS's own suite; these tests prove the integration, not the collectors.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import AgentRole, RiskLimits, RiskOutcome
from src.data.tables import TradeCaseRiskBindingRow, TradeCaseRiskRequestRow
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.riskrequest.service import RiskRequestUnavailable
from src.orchestration.workflow.models import (
    EvidenceType,
    SentimentPayload,
    TradeCaseStatus,
)
from src.risk.authorization import RiskAuthorization
from tests.riskdata.conftest import (
    RecordedMarkets,
    configured_costs,
    holder_block,
    record,
    record_onchain,
)
from tests.riskrequest.conftest import (
    FRESH,
    build_service,
    fresh_onchain,
    fresh_snapshot,
    read_account,
    ready_case,
    set_account,
)


async def counts(sessions):
    async with sessions() as session:
        requests = await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        bindings = await session.scalar(select(func.count()).select_from(TradeCaseRiskBindingRow))
    return requests, bindings


# ------------------------------------------------------- the approved path


async def test_a_complete_case_reaches_a_bound_sentinel_verdict(risk_db, now, trace):
    """The whole phase, end to end over real state."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    assert case.status is TradeCaseStatus.READY_FOR_RISK

    result = await service.request_risk_evaluation(case.id, request_key="rr-approve")

    assert result.kind == "risk_request_evaluated"
    assert result.outcome is RiskOutcome.APPROVE
    assert result.authorization is RiskAuthorization.APPROVED
    assert result.replayed is False
    assert result.risk_input_digest == case.risk_input_digest
    assert (await counts(sessions)) == (1, 1)

    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_APPROVED


async def test_the_binding_carries_the_whole_basis(risk_db, now, trace):
    """Sizing, intent, sources, costs, limits, portfolio and the verdict."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-basis")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    basis = row.basis
    assert set(basis) >= {
        "sizing",
        "readiness",
        "cost_assumptions",
        "risk_limits",
        "portfolio",
        "intent",
        "decision",
        "safety_risk_input_digest",
    }
    assert basis["risk_limits"]["max_position_size_usd"] == str(RiskLimits().max_position_size_usd)
    assert basis["cost_assumptions"]["basis"] == "OPERATOR_CONFIGURED_ASSUMPTION"
    assert basis["portfolio"]["accounting"] == "PASS"
    assert row.intent_fingerprint == result.intent_fingerprint
    assert row.risk_request_digest == result.risk_request_digest
    assert row.risk_request_digest != row.risk_input_digest


async def test_an_approval_reserves_nothing_and_authorises_no_fill(risk_db, now, trace):
    """A verdict is not an execution, and the contract says so in its own type."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    before = await read_account(sessions)
    cash = before.cash_usd

    result = await service.request_risk_evaluation(case.id, request_key="rr-noreserve")

    assert result.outcome is RiskOutcome.APPROVE
    assert result.authorizes_execution is False
    assert result.reserves_cash is False
    after = await read_account(sessions)
    assert after.cash_usd == cash
    assert after.fees_paid_usd == before.fees_paid_usd


async def test_a_risk_check_writes_no_execution_and_moves_no_position(risk_db, now, trace):
    """A risk evaluation is a question. Nothing about the ledger answers it."""
    from src.data.tables import ExecutionRow, PositionRow, TradeRow

    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    await service.request_risk_evaluation(case.id, request_key="rr-noexec")

    async with sessions() as session:
        for table in (ExecutionRow, PositionRow, TradeRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


# ------------------------------------------------- the four verdicts, separated


async def test_a_resizable_shortfall_is_limited_and_not_an_approval(risk_db, now, trace):
    """LIMITED is a rejected size with a recorded capacity, never a permission."""
    _, sessions = risk_db
    service = build_service(sessions, now, notional="3000")
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-limited")

    assert result.outcome is RiskOutcome.REJECT
    assert result.authorization is RiskAuthorization.LIMITED
    assert result.reason_codes == ("MAX_POSITION_SIZE",)
    assert result.authorizes_execution is False
    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_LIMITED


async def test_no_automatic_downsize_follows_a_limited_verdict(risk_db, now, trace):
    """The size asked for stays the size asked for.

    A system that asks again for less after a refusal searches until it gets a
    yes, and the case has exactly one request to spend.
    """
    _, sessions = risk_db
    service = build_service(sessions, now, notional="3000")
    case = await ready_case(service.cases, now, trace)
    first = await service.request_risk_evaluation(case.id, request_key="rr-nodownsize")

    smaller = build_service(sessions, now, notional="100", feed=service.markets)
    again = await smaller.request_risk_evaluation(case.id, request_key="rr-nodownsize-2")

    assert again.kind == "risk_request_refused"
    assert again.reason is RiskRequestRefusal.RISK_REQUEST_ALREADY_EXISTS
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    assert row.requested_notional_usd == Decimal("3000")
    assert row.request_id == first.request_id


async def test_a_measured_danger_is_rejected_terminally(risk_db, now, trace):
    """A real judgement about the market, which is what REJECT is for."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    payload = fresh_onchain(now)
    payload = payload.model_copy(
        update={
            "intelligence": payload.intelligence.model_copy(
                update={"holders": holder_block(now, age=FRESH, top_ten=Decimal("0.5"))}
            )
        }
    )
    case = await ready_case(service.cases, now, trace, onchain=payload)

    result = await service.request_risk_evaluation(case.id, request_key="rr-reject")

    assert result.outcome is RiskOutcome.REJECT
    assert result.authorization is RiskAuthorization.REJECTED
    assert "HOLDER_CONCENTRATION_LIMIT" in result.reason_codes
    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_REJECTED


async def test_a_pause_verdict_stops_the_system_in_the_same_transaction(risk_db, now, trace):
    """The stop this flow could previously only observe.

    Recorded with the rejection it came with, so no window exists where the
    verdict stands and the pause does not. Nothing here ever clears it.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    # The loss day is set alongside the loss: a figure carried over from
    # another UTC day is reset before it is read, which is the point of the
    # rollover and would otherwise silently defeat this test.
    await set_account(sessions, realized_loss_today_usd=Decimal("600"), loss_day=now.date())

    result = await service.request_risk_evaluation(case.id, request_key="rr-pause")

    assert result.outcome is RiskOutcome.PAUSE_SYSTEM
    assert result.authorization is RiskAuthorization.REJECTED
    assert "DAILY_LOSS_LIMIT" in result.reason_codes
    assert (await read_account(sessions)).paused is True
    assert (await counts(sessions)) == (1, 1)


async def test_a_paused_system_refuses_before_asking_anything(risk_db, now, trace):
    """A stop outranks every case-level question, and is checked first.

    The pause is read from the account row this request already holds locked,
    not through the port. An unsynchronised snapshot read would leave a window
    for a pause committed between the check and the binding.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await set_account(sessions, paused=True)

    result = await service.request_risk_evaluation(case.id, request_key="rr-paused")

    assert result.reason is RiskRequestRefusal.SYSTEM_PAUSED
    assert (await counts(sessions)) == (0, 0)


async def test_an_unreadable_stop_is_not_permission(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now, pause=None)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-nostop")

    assert result.reason is RiskRequestRefusal.SYSTEM_STOP_UNREADABLE
    assert (await counts(sessions)) == (0, 0)


async def test_a_kill_switch_refuses_before_asking_anything(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now, kill_switch=True)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-kill")

    assert result.reason is RiskRequestRefusal.KILL_SWITCH_ENGAGED
    assert (await counts(sessions)) == (0, 0)


# --------------------------------------------------- refusals, never rejections


async def test_a_missing_size_asks_sentinel_nothing(risk_db, now, trace):
    """The standing gap, still a gap. Never spent as a risk verdict."""
    _, sessions = risk_db
    service = build_service(sessions, now, notional=None)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-nosize")

    assert result.reason is RiskRequestRefusal.SIZING_INPUT_UNAVAILABLE
    assert result.sizing_reason.value == "AUTONOMOUS_SIZING_INPUT_MISSING"
    assert (await counts(sessions)) == (0, 0)
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.READY_FOR_RISK


async def test_a_missing_fact_asks_sentinel_nothing(risk_db, now, trace):
    """An architectural hole must never spend a terminal rejection."""
    _, sessions = risk_db
    service = build_service(sessions, now, costs=configured_costs(fee=None, slippage=None))
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-nocost")

    assert result.reason is RiskRequestRefusal.RISK_DATA_INCOMPLETE
    assert {item.code.value for item in result.data_gaps} == {"NOT_CONFIGURED"}
    assert (await counts(sessions)) == (0, 0)
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.READY_FOR_RISK


async def test_a_source_older_than_sentinel_s_own_bound_asks_it_nothing(risk_db, now, trace):
    """Provable, attributed, and still too old for the evaluation itself.

    The completeness check tolerates ninety seconds because that is the
    recorder's cadence; SENTINEL tolerates thirty. Asking anyway would come back
    as a terminal rejection of the market.
    """
    _, sessions = risk_db
    aged = timedelta(seconds=45)
    feed = RecordedMarkets(fresh_snapshot(now, age=aged, metadata_age=aged))
    service = build_service(sessions, now, feed=feed)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(case.id, request_key="rr-stale")

    assert result.reason is RiskRequestRefusal.SOURCE_OLDER_THAN_RISK_LIMIT
    assert result.detail == "MARKET_OLDER_THAN_RISK_LIMIT"
    assert (await counts(sessions)) == (0, 0)


async def test_aged_holder_metrics_are_refused_rather_than_judged(risk_db, now, trace):
    """Each source is measured on its own instant, not the newest one present."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    payload = fresh_onchain(now, holders=holder_block(now, age=timedelta(seconds=60)))
    case = await ready_case(service.cases, now, trace, onchain=payload)

    result = await service.request_risk_evaluation(case.id, request_key="rr-oldholders")

    assert result.reason is RiskRequestRefusal.SOURCE_OLDER_THAN_RISK_LIMIT
    assert result.detail == "HOLDERS_OLDER_THAN_RISK_LIMIT"


async def test_a_case_that_is_not_ready_is_not_asked_about(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, anchor=False)

    result = await service.request_risk_evaluation(case.id, request_key="rr-notready")

    assert result.reason is RiskRequestRefusal.TRADE_CASE_NOT_READY_FOR_RISK
    assert (await counts(sessions)) == (0, 0)


async def test_a_terminal_case_is_not_asked_about(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await service.cases.cancel_trade_case(case.id)

    result = await service.request_risk_evaluation(case.id, request_key="rr-terminal")

    assert result.reason is RiskRequestRefusal.TRADE_CASE_TERMINAL


async def test_an_unknown_case_cannot_be_requested(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    with pytest.raises(RiskRequestUnavailable):
        await service.request_risk_evaluation(uuid4(), request_key="rr-missing")


async def test_a_held_position_that_cannot_be_valued_stops_the_request(risk_db, now, trace):
    """`PORTFOLIO_DATA_UNKNOWN` would be terminal, so it is never reached.

    No mark source exists for a holding in another market, and a holding this
    system cannot value is a missing capability rather than a judgement.
    """
    from src.core.models import Position
    from src.data.repository import save_position

    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id="robinhood:mainnet:0x" + "f1" * 20,
                quantity=Decimal("5"),
                cost_basis_usd=Decimal("50"),
                created_at=now,
                updated_at=now,
            ),
        )

    result = await service.request_risk_evaluation(case.id, request_key="rr-unmarked")

    assert result.reason is RiskRequestRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert (await counts(sessions)) == (0, 0)


# ----------------------------------------------------- blockers stay visible


async def test_known_blockers_survive_a_refusal(risk_db, now, trace):
    """Measured danger and a data gap are two statements, not one."""
    _, sessions = risk_db
    service = build_service(sessions, now, notional=None)
    payload = fresh_onchain(now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT",))
    case = await ready_case(service.cases, now, trace, onchain=payload, anchor=False)

    result = await service.request_risk_evaluation(case.id, request_key="rr-blocked")

    assert result.kind == "risk_request_refused"
    codes = {item.code for item in result.blockers}
    assert "CONTRACT_CODE_ABSENT" in codes
    assert "ATLAS_EVIDENCE_BLOCKED" in codes


# --------------------------------------------------- advisory has no authority


async def test_an_advisory_change_alone_changes_no_risk_authority(risk_db, now, trace):
    """FUSE and SIGNAL cannot reach the verdict, the binding or the digest."""
    from tests.riskdata.conftest import record_synthesis

    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    before_digest = case.risk_input_digest

    await record_synthesis(service.cases, case, now, blocking=True)
    await record(
        service.cases,
        case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        SentimentPayload(assessment="NEGATIVE"),
        key=f"rr-signal-2-{case.id}",
        supersedes_id=next(
            item.evidence_id
            for item in await service.cases.evidence(case.id)
            if item.evidence_type is EvidenceType.SENTIMENT
        ),
    )

    after = await service.cases.get_trade_case(case.id)
    assert after.risk_input_digest == before_digest
    result = await service.request_risk_evaluation(case.id, request_key="rr-advisory")
    assert result.outcome is RiskOutcome.APPROVE
    assert "FUSE_EVIDENCE_BLOCKED" not in {item.code for item in result.blockers}


# --------------------------------------------------------- request identity


async def test_a_repeated_request_replays_rather_than_recomputing(risk_db, now, trace):
    """The same key finds the same bound inputs and the same verdict."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    first = await service.request_risk_evaluation(case.id, request_key="rr-replay")
    later = build_service(sessions, now + timedelta(minutes=5), feed=service.markets)
    second = await later.request_risk_evaluation(case.id, request_key="rr-replay")

    assert second.replayed is True
    assert (second.request_id, second.intent_id) == (first.request_id, first.intent_id)
    assert second.risk_request_digest == first.risk_request_digest
    assert second.intent_fingerprint == first.intent_fingerprint
    assert (await counts(sessions)) == (1, 1)


async def test_a_changed_data_situation_is_not_a_second_request(risk_db, now, trace):
    """A case gets one canonical trade request. New facts do not mint another."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await service.request_risk_evaluation(case.id, request_key="rr-one")

    moved = build_service(sessions, now, feed=RecordedMarkets(fresh_snapshot(now)))
    result = await moved.request_risk_evaluation(case.id, request_key="rr-two")

    assert result.reason is RiskRequestRefusal.RISK_REQUEST_ALREADY_EXISTS
    assert (await counts(sessions)) == (1, 1)


async def test_a_caller_naming_a_revision_it_has_left_is_refused(risk_db, now, trace):
    """Submission-time checking, so a verdict cannot bind to a stale basis."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(
        case.id, request_key="rr-revision", expected_revision=case.revision + 5
    )

    assert result.reason is RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST
    assert (await counts(sessions)) == (0, 0)


async def test_a_caller_naming_a_superseded_safety_digest_is_refused(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(
        case.id, request_key="rr-digest", expected_risk_input_digest="f" * 64
    )

    assert result.reason is RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST


async def test_superseding_safety_evidence_changes_the_digest_and_the_basis(risk_db, now, trace):
    """A new canonical source is a new basis, and the caller's old one is gone."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    original = case.risk_input_digest

    previous = next(
        item
        for item in await service.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.ONCHAIN
    )
    await record_onchain(
        service.cases,
        case,
        now,
        fresh_onchain(now, holders=holder_block(now, age=FRESH, holder_count=99)),
        key=f"rr-atlas-2-{case.id}",
        supersedes_id=previous.evidence_id,
    )

    refused = await service.request_risk_evaluation(
        case.id, request_key="rr-superseded", expected_risk_input_digest=original
    )
    assert refused.reason is RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST

    accepted = await service.request_risk_evaluation(case.id, request_key="rr-superseded")
    assert accepted.kind == "risk_request_evaluated"
    assert accepted.risk_input_digest != original


# ------------------------------------------------------------------- rollback


async def test_a_failure_mid_request_leaves_nothing_behind(risk_db, now, trace, monkeypatch):
    """One transaction, so there is no half-finished binding to recover from."""
    import src.orchestration.riskrequest.service as module

    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    def explode(basis):
        raise RuntimeError("interrupted after the binding was staged")

    monkeypatch.setattr(module, "risk_request_digest", explode)
    with pytest.raises(RuntimeError):
        await service.request_risk_evaluation(case.id, request_key="rr-crash")

    assert (await counts(sessions)) == (0, 0)
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.READY_FOR_RISK

    monkeypatch.undo()
    recovered = await service.request_risk_evaluation(case.id, request_key="rr-crash")
    assert recovered.kind == "risk_request_evaluated"
    assert recovered.replayed is False
    assert (await counts(sessions)) == (1, 1)


# ------------------------------------------------------------------ the call


def test_the_call_accepts_no_risk_input_session_or_provider():
    """A caller names a case and a key. Everything else is assembled here."""
    import inspect

    from src.orchestration.riskrequest.service import RiskRequestService

    parameters = set(inspect.signature(RiskRequestService.request_risk_evaluation).parameters)
    assert parameters == {
        "self",
        "trade_case_id",
        "request_key",
        "expected_revision",
        "expected_risk_input_digest",
    }


def test_the_service_never_mutates_sentinel_or_its_classification():
    """One risk engine and one interpretation, both reused rather than rebuilt."""
    import ast
    from pathlib import Path

    names: set[str] = set()
    for path in sorted(Path("src/orchestration/riskrequest").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    for forbidden in ("evaluate", "classify_decision", "classify_risk_authorization"):
        assert forbidden not in names


def test_only_the_risk_request_service_constructs_a_trade_intent():
    """The assertion Phase 2M-A left standing, now inverted rather than deleted.

    Exactly one place in the source tree may build the object that carries a
    size into a risk evaluation, and this names it.
    """
    import subprocess

    found = subprocess.run(
        ["git", "grep", "-l", "--untracked", "TradeIntent(", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert sorted(found) == [
        "backend/src/core/models.py",
        "backend/src/orchestration/riskrequest/service.py",
    ]
