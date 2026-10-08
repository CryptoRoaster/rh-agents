"""An early case through the real risk request, SENTINEL and the PAPER fill.

What differs from a normal case is exactly three things: the minimum liquidity
SENTINEL applies, the fixed ten-dollar notional, and the strategy's own caps.
Every other limit, every source bound and SENTINEL itself are the normal ones,
and a normal case beside it is judged as it always was.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.models import AgentRole, RiskOutcome
from src.data.tables import TradeCaseRiskRequestRow
from src.orchestration.riskrequest import service as risk_service
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.strategy.early import EarlyLedger, early_ledger
from src.orchestration.workflow.models import EvidenceType, TradeCaseStatus
from tests.casefill.conftest import build_fill_service
from tests.early.conftest import (
    early_setup,
    open_early_case,
    record_early_onchain,
    record_trigger,
    young_history,
)
from tests.paperexit.conftest import entered, market_feed, money
from tests.riskdata.conftest import RecordedMarkets, anchor_payload, record
from tests.riskrequest.conftest import build_service, fresh_snapshot


async def _execution_detail(now, capacity):
    """A real ANCHOR assessment of the early ladder, with its capacity restated."""
    from tests.anchor.conftest import source
    from tests.early.test_anchor import _assess

    report = await _assess(now, source(now))
    detail = report.submission.payload.execution
    return detail.model_copy(update={"largest_tested_acceptable_notional_usd": capacity})


async def early_ready(service, sessions, now, trace, *, key="early", capacity=Decimal(10)):
    """An early case carried by the real producers and workflow to READY_FOR_RISK."""
    cases = service.cases
    trade_case = await open_early_case(cases, sessions, now, trace, key=key)
    await record_early_onchain(cases, trade_case, now)
    result, setup = await early_setup(cases, trade_case, service.markets, now, young_history(now))
    assert setup is not None, result
    trigger = await record_trigger(cases, trade_case, now, setup)
    payload = anchor_payload(setup.evidence_id, trigger.evidence_id)
    if capacity != "legacy":
        payload = payload.model_copy(update={"execution": await _execution_detail(now, capacity)})
    await record(
        cases,
        trade_case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        payload,
        key=f"early-anchor-{trade_case.id}",
    )
    ready = await cases.get_trade_case(trade_case.id)
    assert ready.status is TradeCaseStatus.READY_FOR_RISK, ready.status
    return ready


def _service(sessions, now, *, liquidity="750000"):
    feed = RecordedMarkets(fresh_snapshot(now, liquidity=Decimal(liquidity)))
    # The operator's configured size is deliberately not ten dollars.
    return build_service(sessions, now, feed=feed, notional="500")


async def _requested_notional(sessions, trade_case_id):
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseRiskRequestRow.requested_notional_usd).where(
                TradeCaseRiskRequestRow.trade_case_id == trade_case_id
            )
        )


# --------------------------------------------------------- liquidity and size


async def test_an_early_case_at_ten_thousand_liquidity_is_approved_for_ten_dollars(
    risk_db, now, trace
):
    _, sessions = risk_db
    service = _service(sessions, now, liquidity="10000")
    trade_case = await early_ready(service, sessions, now, trace)

    result = await service.request_risk_evaluation(trade_case.id, request_key="early-approve")

    assert result.kind == "risk_request_evaluated", result
    assert result.outcome is RiskOutcome.APPROVE
    assert Decimal(await _requested_notional(sessions, trade_case.id)) == Decimal(10)


async def test_an_early_case_below_ten_thousand_liquidity_is_rejected_by_sentinel(
    risk_db, now, trace
):
    _, sessions = risk_db
    service = _service(sessions, now, liquidity="9999.99")
    trade_case = await early_ready(service, sessions, now, trace)

    result = await service.request_risk_evaluation(trade_case.id, request_key="early-thin")

    assert result.kind == "risk_request_evaluated", result
    assert result.outcome is RiskOutcome.REJECT
    assert any("LIQUIDITY" in code for code in result.reason_codes), result.reason_codes


async def test_a_normal_case_still_needs_one_hundred_thousand(risk_db, now, trace):
    from tests.riskrequest.conftest import ready_case

    _, sessions = risk_db
    service = _service(sessions, now, liquidity="50000")
    trade_case = await ready_case(service.cases, now, trace)

    result = await service.request_risk_evaluation(trade_case.id, request_key="normal-thin")

    assert result.kind == "risk_request_evaluated", result
    assert result.outcome is RiskOutcome.REJECT
    assert any("LIQUIDITY" in code for code in result.reason_codes), result.reason_codes
    # And still asks for the operator's size, not the early one.
    assert Decimal(await _requested_notional(sessions, trade_case.id)) == Decimal(500)


async def test_capacity_below_ten_dollars_is_refused_without_downsizing(risk_db, now, trace):
    _, sessions = risk_db
    service = _service(sessions, now)
    trade_case = await early_ready(service, sessions, now, trace, capacity=Decimal("9.99"))

    result = await service.request_risk_evaluation(trade_case.id, request_key="early-short")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.EARLY_EXECUTABLE_CAPACITY_INSUFFICIENT
    assert result.detail == "EARLY_EXECUTABLE_CAPACITY_BELOW_NOTIONAL"
    assert await _requested_notional(sessions, trade_case.id) is None


async def test_anchor_evidence_without_a_tested_ladder_proves_no_capacity(risk_db, now, trace):
    _, sessions = risk_db
    service = _service(sessions, now)
    trade_case = await early_ready(service, sessions, now, trace, capacity="legacy")

    result = await service.request_risk_evaluation(trade_case.id, request_key="early-legacy")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.EARLY_EXECUTABLE_CAPACITY_INSUFFICIENT
    assert result.detail == "EARLY_EXECUTABLE_CAPACITY_UNKNOWN"


# ------------------------------------------------------------ strategy caps


def _with_ledger(monkeypatch, ledger):
    async def stub(session):
        return ledger

    monkeypatch.setattr(risk_service, "early_ledger", stub)


async def _capped(risk_db, now, trace, monkeypatch, ledger):
    _, sessions = risk_db
    service = _service(sessions, now)
    trade_case = await early_ready(service, sessions, now, trace)
    _with_ledger(monkeypatch, ledger)
    return await service.request_risk_evaluation(trade_case.id, request_key="early-capped")


async def test_five_open_early_positions_block_a_sixth(risk_db, now, trace, monkeypatch):
    result = await _capped(risk_db, now, trace, monkeypatch, EarlyLedger(5, Decimal(30), exits=()))
    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.EARLY_STRATEGY_CAP_REACHED
    assert result.detail == "EARLY_MAX_OPEN_POSITIONS_REACHED"


async def test_early_exposure_may_not_pass_fifty_dollars(risk_db, now, trace, monkeypatch):
    result = await _capped(
        risk_db, now, trace, monkeypatch, EarlyLedger(4, Decimal("40.01"), exits=())
    )
    assert result.kind == "risk_request_refused"
    assert result.detail == "EARLY_MAX_EXPOSURE_REACHED"


async def test_thirty_dollars_of_early_loss_today_stops_early_entries(
    risk_db, now, trace, monkeypatch
):
    ledger = EarlyLedger(0, Decimal(0), exits=((now - timedelta(minutes=1), Decimal(-30)),))
    result = await _capped(risk_db, now, trace, monkeypatch, ledger)
    assert result.kind == "risk_request_refused"
    assert result.detail == "EARLY_DAILY_LOSS_CAP_REACHED"


async def test_yesterdays_early_loss_does_not_count_today(risk_db, now, trace, monkeypatch):
    from src.orchestration.strategy.early import utc_day_start

    yesterday = utc_day_start(now) - timedelta(seconds=1)
    ledger = EarlyLedger(0, Decimal(0), exits=((yesterday, Decimal(-500)),))
    result = await _capped(risk_db, now, trace, monkeypatch, ledger)
    assert result.kind == "risk_request_evaluated", result
    assert result.outcome is RiskOutcome.APPROVE


async def test_a_cap_never_engages_the_kill_switch(risk_db, now, trace, monkeypatch):
    from tests.riskrequest.conftest import read_account

    ledger = EarlyLedger(0, Decimal(0), exits=((now, Decimal(-100)),))
    await _capped(risk_db, now, trace, monkeypatch, ledger)
    _, sessions = risk_db
    assert (await read_account(sessions)).paused is False


# ---------------------------------------------------------- the real ledger


async def test_an_early_fill_is_ten_dollars_and_counts_in_the_early_book(risk_db, now, trace):
    _, sessions = risk_db
    service = _service(sessions, now, liquidity="10000")
    trade_case = await early_ready(service, sessions, now, trace)
    approval = await service.request_risk_evaluation(trade_case.id, request_key="early-fill")
    assert approval.outcome is RiskOutcome.APPROVE

    fill = await build_fill_service(sessions, now, feed=service.markets).execute_case_fill(
        trade_case.id, request_key="early-fill"
    )

    assert fill.kind == "paper_fill_recorded", getattr(fill, "detail", None)
    # Ten dollars of quantity at the reference price, filled with the configured
    # 25 bps of paper slippage — the same arithmetic a normal fill gets.
    assert money(fill.notional_usd) == money("10.025")
    async with sessions() as session:
        ledger = await early_ledger(session)
    assert ledger.open_positions == 1
    assert Decimal(10) < ledger.exposure_usd < Decimal("10.1")


async def test_normal_positions_and_losses_are_not_in_the_early_book(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace, key="normal", feed=market_feed(now))
    async with sessions() as session:
        ledger = await early_ledger(session)
    assert ledger == EarlyLedger(open_positions=0, exposure_usd=Decimal(0), exits=())
