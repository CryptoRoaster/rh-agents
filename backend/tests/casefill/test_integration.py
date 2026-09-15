"""A stored approval, the real paper service, and exactly one bound fill.

Everything below runs the production path: the real workflow service against a
real database, the real risk request, the real completeness check,
`src.risk.engine.evaluate` and `PaperExecutor` itself, with the real ledger
postings. What is stubbed is the market feed's single read and the stop source.

The evidence is fixture evidence and no specialist worker runs here. An
integration test is not autonomous operation.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.data.tables import (
    ExecutionRow,
    OrderRow,
    PositionRow,
    RiskRow,
    TradeCaseExecutionRow,
    TradeRow,
)
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.casefill.service import CaseFillUnavailable
from src.orchestration.workflow.models import EvidenceType, TradeCaseStatus
from src.risk.authorization import RiskAuthorization
from tests.casefill.conftest import approved_case, build_fill_service
from tests.riskdata.conftest import RecordedMarkets, holder_block, record_onchain
from tests.riskrequest.conftest import (
    FRESH,
    fresh_onchain,
    fresh_snapshot,
    read_account,
    set_account,
)


async def counts(sessions):
    async with sessions() as session:
        fills = await session.scalar(select(func.count()).select_from(ExecutionRow))
        bound = await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))
    return fills, bound


# ------------------------------------------------------------- the filled path


async def test_an_approved_request_produces_exactly_one_bound_fill(risk_db, now, trace):
    """The whole phase, end to end over real state."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "paper_fill_recorded"
    assert result.replayed is False
    assert result.is_simulated is True
    assert result.request_id == approval.request_id
    assert result.intent_id == approval.intent_id
    assert result.authorizing_binding_id == approval.binding_id
    assert result.quantity == Decimal("400")
    assert (await counts(sessions)) == (1, 1)


async def test_the_ledger_books_the_fill_exactly_once(risk_db, now, trace):
    """Cash, fees and the position move by the amounts the fill states."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    before = await read_account(sessions)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    # Compared at six decimal places, the convention this suite already uses for
    # money read back from the database: PostgreSQL stores `Numeric(38, 18)`
    # exactly, SQLite round-trips it through a float, and the difference is far
    # below anything an assertion about a fill should rest on.
    def cents(value):
        return value.quantize(Decimal("0.000001"))

    after = await read_account(sessions)
    assert cents(after.cash_usd) == cents(before.cash_usd - result.notional_usd - result.fees_usd)
    assert cents(after.fees_paid_usd) == cents(before.fees_paid_usd + result.fees_usd)
    async with sessions() as session:
        position = await session.scalar(select(PositionRow))
        trades = (await session.scalars(select(TradeRow))).all()
        orders = (await session.scalars(select(OrderRow))).all()
    assert cents(position.quantity) == cents(result.quantity)
    assert cents(position.cost_basis_usd) == cents(result.notional_usd + result.fees_usd)
    assert len(trades) == 1
    assert len(orders) == 1


async def test_the_chain_from_case_to_fill_is_one_row(risk_db, now, trace):
    """Case → request → intent → order → execution, answerable in one read."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="fill-req")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExecutionRow))
        execution = await session.scalar(select(ExecutionRow))
    assert row.trade_case_id == case.id
    assert row.request_id == approval.request_id
    assert row.intent_id == approval.intent_id
    assert row.execution_id == execution.id
    assert row.order_id == execution.order_id
    assert row.authorizing_binding_id == approval.binding_id
    assert row.risk_input_digest == approval.risk_input_digest
    assert result.case_execution_id == row.case_execution_id


async def test_the_fill_keeps_the_market_it_was_rechecked_against(risk_db, now, trace):
    """The re-check's own snapshot, stored whole rather than referenced."""
    from src.core.models import MarketSnapshot

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExecutionRow))
        stored_risk = await session.scalar(select(RiskRow))
    market = MarketSnapshot.model_validate(row.basis["market_snapshot"])
    assert market.fingerprint() == stored_risk.payload["market_fingerprint"]
    assert str(market.id) == stored_risk.payload["market_snapshot_id"]
    # A different reading at a different instant from the approval's own.
    assert row.basis["market_snapshot"]["id"] != row.basis["intent"]["id"]


async def test_the_quantity_comes_from_the_stored_request(risk_db, now, trace):
    """Nothing is re-sized. A moved price changes the fill price, not the order.

    Re-deriving the quantity would make the order that is filled a different
    order from the one that was authorised.
    """
    _, sessions = risk_db
    case, approval, _ = await approved_case(sessions, now, trace)
    moved = RecordedMarkets(fresh_snapshot(now, price=Decimal("2.50")))
    service = build_fill_service(sessions, now, feed=moved)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExecutionRow))
    assert result.quantity == Decimal("400")
    assert row.basis["intent"]["quantity"] == "400.000000000000000000"
    assert result.execution_price_usd > Decimal("2.4")


# ---------------------------------------------------------------------- replay


async def test_a_completed_fill_replays_unchanged(risk_db, now, trace):
    """History, not a new permission. Returned even once the approval expired."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    first = await service.execute_case_fill(case.id, request_key="fill-req")

    much_later = now + timedelta(days=3)
    later = build_fill_service(
        sessions, much_later, feed=RecordedMarkets(fresh_snapshot(much_later))
    )
    again = await later.execute_case_fill(case.id, request_key="fill-req")

    assert again.kind == "paper_fill_recorded"
    assert again.replayed is True
    assert again.execution_id == first.execution_id
    assert again.quantity == first.quantity
    assert again.filled_at == first.filled_at
    assert (await counts(sessions)) == (1, 1)


async def test_a_second_call_key_cannot_produce_a_second_fill(risk_db, now, trace):
    """One order, whatever a caller calls it."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    other = await service.execute_case_fill(case.id, request_key="some-other-key")

    assert other.kind == "execution_refused"
    assert other.reason is ExecutionRefusal.REQUEST_KEY_MISMATCH
    assert (await counts(sessions)) == (1, 1)


async def test_a_case_without_a_request_has_nothing_to_fill(risk_db, now, trace):
    from tests.riskrequest.conftest import build_service, ready_case

    _, sessions = risk_db
    risk = build_service(sessions, now)
    case = await ready_case(risk.cases, now, trace, key="unasked")
    service = build_fill_service(sessions, now, feed=risk.markets)

    result = await service.execute_case_fill(case.id, request_key="unasked-req")

    assert result.reason is ExecutionRefusal.REQUEST_NOT_FOUND
    assert (await counts(sessions)) == (0, 0)


async def test_an_unknown_case_cannot_be_filled(risk_db, now, trace):
    _, sessions = risk_db
    _, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    with pytest.raises(CaseFillUnavailable):
        await service.execute_case_fill(uuid4(), request_key="fill-req")


# ------------------------------------------------------- authorization refused


async def test_a_limited_request_is_not_executable(risk_db, now, trace):
    """A rejected size with a recorded capacity is still a rejected size."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace, notional="3000")
    assert approval.authorization is RiskAuthorization.LIMITED
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.REQUEST_NOT_APPROVED
    assert result.detail == "LIMITED"
    assert (await counts(sessions)) == (0, 0)


async def test_a_rejected_request_is_not_executable(risk_db, now, trace):
    _, sessions = risk_db
    payload = fresh_onchain(now)
    payload = payload.model_copy(
        update={
            "intelligence": payload.intelligence.model_copy(
                update={"holders": holder_block(now, age=FRESH, top_ten=Decimal("0.5"))}
            )
        }
    )
    case, approval, feed = await approved_case(sessions, now, trace, onchain=payload)
    assert approval.authorization is RiskAuthorization.REJECTED
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason in (
        ExecutionRefusal.REQUEST_NOT_APPROVED,
        ExecutionRefusal.TRADE_CASE_TERMINAL,
    )
    assert (await counts(sessions)) == (0, 0)


async def test_an_expired_approval_does_not_authorise_a_fill(risk_db, now, trace):
    """A decision issued with a short life is not an authorization afterwards."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    beyond = now + timedelta(seconds=6)
    service = build_fill_service(sessions, beyond, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.AUTHORIZATION_EXPIRED
    assert (await counts(sessions)) == (0, 0)


async def test_changed_safety_evidence_withdraws_the_authorization(risk_db, now, trace):
    """Not a weaker approval: one about a different case."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    previous = next(
        item
        for item in (await build_fill_service(sessions, now, feed=feed).cases.evidence(case.id))
        if item.evidence_type is EvidenceType.ONCHAIN
    )
    service = build_fill_service(sessions, now, feed=feed)
    await record_onchain(
        service.cases,
        case,
        now,
        fresh_onchain(now, holders=holder_block(now, age=FRESH, holder_count=77)),
        key=f"fill-atlas-2-{case.id}",
        supersedes_id=previous.evidence_id,
    )

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SAFETY_EVIDENCE_CHANGED
    assert (await counts(sessions)) == (0, 0)


async def test_an_expired_case_is_not_filled(risk_db, now, trace):
    _, sessions = risk_db
    from tests.riskrequest.conftest import build_service, ready_case

    risk = build_service(sessions, now)
    case = await ready_case(risk.cases, now, trace, key="short", lifetime=timedelta(seconds=4))
    approval = await risk.request_risk_evaluation(case.id, request_key="short-req")
    assert approval.kind == "risk_request_evaluated"

    beyond = now + timedelta(seconds=4)
    service = build_fill_service(sessions, beyond, feed=RecordedMarkets(fresh_snapshot(beyond)))
    result = await service.execute_case_fill(case.id, request_key="short-req")

    assert result.reason is ExecutionRefusal.TRADE_CASE_TERMINAL
    assert result.detail == "EXPIRED"
    assert (await counts(sessions)) == (0, 0)


async def test_a_caller_naming_another_binding_is_refused(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(
        case.id, request_key="fill-req", expected_binding_id=uuid4()
    )

    assert result.reason is ExecutionRefusal.AUTHORIZATION_SUPERSEDED
    assert (await counts(sessions)) == (0, 0)


async def test_a_later_case_sees_the_first_fill_s_committed_cash(risk_db, now, trace):
    """Sequentially, the cash check still bites: no overdraw, no second entry."""
    from uuid import uuid4 as _uuid4

    _, sessions = risk_db
    first, _, feed = await approved_case(sessions, now, trace, key="cash-one")
    service = build_fill_service(sessions, now, feed=feed)
    assert (await service.execute_case_fill(first.id, request_key="cash-one-req")).kind == (
        "paper_fill_recorded"
    )

    second, _, _ = await approved_case(sessions, now, _uuid4(), key="cash-two")
    await set_account(sessions, cash_usd=Decimal("10"))
    later = build_fill_service(sessions, now, feed=feed)

    result = await later.execute_case_fill(second.id, request_key="cash-two-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert "INSUFFICIENT_CASH" in result.reason_codes
    assert (await read_account(sessions)).cash_usd == Decimal("10")


# ------------------------------------------------------------- stops and risk


async def test_a_paused_account_stops_the_fill(risk_db, now, trace):
    """Read from the account row this call already holds locked."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    await set_account(sessions, paused=True)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SYSTEM_PAUSED
    assert (await counts(sessions)) == (0, 0)


async def test_a_kill_switch_stops_the_fill(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed, kill_switch=True)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.KILL_SWITCH_ENGAGED
    assert (await counts(sessions)) == (0, 0)


async def test_an_unreadable_stop_is_not_permission(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed, pause=None)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SYSTEM_STOP_UNREADABLE
    assert (await counts(sessions)) == (0, 0)


async def test_a_pause_verdict_at_fill_time_stops_the_system_without_filling(risk_db, now, trace):
    """The re-check may pause the system, and does so with its own rejection."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=Decimal("600"), loss_day=now.date())
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert result.outcome.value == "PAUSE_SYSTEM"
    assert "DAILY_LOSS_LIMIT" in result.reason_codes
    assert (await read_account(sessions)).paused is True
    assert (await counts(sessions)) == (0, 0)


async def test_a_fill_time_rejection_is_final_for_that_order(risk_db, now, trace):
    """One order, one verdict. A refused fill is not retried into a yes."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    await set_account(sessions, cash_usd=Decimal("10"))
    service = build_fill_service(sessions, now, feed=feed)

    first = await service.execute_case_fill(case.id, request_key="fill-req")
    assert first.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert "INSUFFICIENT_CASH" in first.reason_codes

    await set_account(sessions, cash_usd=Decimal("10000"))
    again = await service.execute_case_fill(case.id, request_key="fill-req")

    assert again.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert again.replayed is True
    assert (await counts(sessions)) == (0, 0)


async def test_an_unvaluable_holding_stops_the_fill(risk_db, now, trace):
    """A position this system cannot value is a missing capability, not a verdict."""
    from src.core.models import Position
    from src.data.repository import save_position

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
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
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert (await counts(sessions)) == (0, 0)


async def test_a_source_older_than_the_risk_limit_stops_the_fill(risk_db, now, trace):
    _, sessions = risk_db
    case, _, _ = await approved_case(sessions, now, trace)
    aged = timedelta(seconds=45)
    stale = RecordedMarkets(fresh_snapshot(now, age=aged, metadata_age=aged))
    service = build_fill_service(sessions, now, feed=stale)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SOURCE_OLDER_THAN_RISK_LIMIT
    assert (await counts(sessions)) == (0, 0)


# ------------------------------------------------------------------- rollback


async def test_a_failure_before_commit_leaves_nothing_behind(risk_db, now, trace, monkeypatch):
    """One transaction, so there is no half-booked fill to reconcile."""
    import src.orchestration.casefill.service as module

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    before = await read_account(sessions)
    service = build_fill_service(sessions, now, feed=feed)

    def explode(quantity, price):
        raise RuntimeError("interrupted after the fill was staged")

    monkeypatch.setattr(module, "notional_of", explode)
    with pytest.raises(RuntimeError):
        await service.execute_case_fill(case.id, request_key="fill-req")

    assert (await counts(sessions)) == (0, 0)
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PositionRow)) == 0
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.RISK_APPROVED

    monkeypatch.undo()
    recovered = await service.execute_case_fill(case.id, request_key="fill-req")
    assert recovered.kind == "paper_fill_recorded"
    assert (await counts(sessions)) == (1, 1)


# --------------------------------------------------------- workflow completion


async def test_a_filled_case_ends_as_executed(risk_db, now, trace):
    """The workflow stays the status owner; the transition matrix decides."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.EXECUTED
    assert after.revision > case.revision
    assert result.trade_case_status == "EXECUTED"
    timeline = await service.cases.timeline(case.id)
    assert "ENTRY_EXECUTED" in {item.event_type for item in timeline}


def test_executed_is_terminal_and_reachable_only_from_an_approval():
    """A booked fill is the only way a case ends this way."""
    from src.orchestration.workflow.models import (
        MARKET_BARRING_CASE_STATUSES,
        TERMINAL_CASE_STATUSES,
        TradeCaseStatus,
    )
    from src.orchestration.workflow.service import CASE_TRANSITIONS

    assert TradeCaseStatus.EXECUTED in TERMINAL_CASE_STATUSES
    assert TradeCaseStatus.EXECUTED in MARKET_BARRING_CASE_STATUSES
    sources = {
        status
        for status, targets in CASE_TRANSITIONS.items()
        if TradeCaseStatus.EXECUTED in targets
    }
    assert sources == {TradeCaseStatus.RISK_APPROVED}


async def test_an_executed_market_does_not_open_another_case(risk_db, now, trace):
    """No re-entry contract exists, so intake refuses rather than inventing one."""
    from tests.casefill.conftest import candidate_for
    from tests.commander.conftest import intake_service
    from tests.riskdata.conftest import recorded_snapshot

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    intake = intake_service(
        sessions,
        now,
        candidates=(candidate_for(snapshot),),
        snapshots={snapshot.pair.pair_id: snapshot},
    )
    outcome = await intake.run_cycle()

    assert outcome.opened == ()
    assert [reason for _, reason in outcome.refused] == ["POSITION_OPENED_FOR_MARKET"]


# --------------------------------------------------------------- the call shape


def test_the_call_accepts_no_quantity_price_limit_or_portfolio():
    """A caller names the stored request and, at most, what it expects."""
    import inspect

    from src.orchestration.casefill.service import CaseFillService

    parameters = set(inspect.signature(CaseFillService.execute_case_fill).parameters)
    assert parameters == {
        "self",
        "trade_case_id",
        "request_key",
        "expected_binding_id",
        "expected_risk_input_digest",
    }


def test_the_service_builds_no_second_risk_engine_or_executor():
    """One risk engine, one executor, one accounting path — all reused."""
    import ast
    from pathlib import Path

    names: set[str] = set()
    for path in sorted(Path("src/orchestration/casefill").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.rsplit(".", 1)[-1])
    for forbidden in ("evaluate", "apply_fill", "calculate_pnl", "PaperExecutor"):
        assert forbidden not in names
