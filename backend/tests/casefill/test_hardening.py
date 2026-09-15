"""Three execution-boundary defects an independent review reproduced against 4f7045d.

Each let a fill rest on something it should not: a decision that did not exist,
limits that were not the ones applied, and an instant past the authorization the
order ran under.
"""

import inspect
import textwrap
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.models import MarketSnapshot, RiskLimits, TradeIntent
from src.data.repository import aware
from src.data.tables import (
    ExecutionRow,
    OrderRow,
    PositionRow,
    RiskRow,
    TradeCaseExecutionRow,
    TradeCaseRiskBindingRow,
    TradeCaseRiskRequestRow,
)
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.casefill.service import CaseFillService
from src.orchestration.workflow.models import TradeCaseStatus
from tests.casefill.conftest import approved_case, build_fill_service
from tests.riskrequest.conftest import read_account


class SteppingClock:
    """A trusted clock that advances on every read, without any real sleeping.

    Time passing between the authorization checks and the fill is the situation
    being reproduced. A real sleep would prove it slowly and flakily instead of
    exactly.
    """

    def __init__(self, instant, step=timedelta(seconds=3)) -> None:
        self.instant = instant
        self.step = step
        self.reads = 0

    def now(self):
        self.reads += 1
        current = self.instant
        self.instant = self.instant + self.step
        return current


def stepping(sessions, now, *, feed, step=timedelta(seconds=3), **overrides):
    """A fill service whose every clock read moves time forward."""
    clock = SteppingClock(now, step=step)
    service = build_fill_service(sessions, now, feed=feed, clock=clock, **overrides)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    return service, clock


def _instant(value):
    """One stored ISO instant, parsed back for comparison."""
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def counts(sessions):
    async with sessions() as session:
        fills = await session.scalar(select(func.count()).select_from(ExecutionRow))
        bound = await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))
    return fills, bound


async def rows(sessions):
    async with sessions() as session:
        return (
            await session.scalar(select(TradeCaseExecutionRow)),
            await session.scalar(select(OrderRow)),
            await session.scalar(select(RiskRow)),
            await session.scalar(select(ExecutionRow)),
            await session.scalar(select(TradeCaseRiskBindingRow)),
        )


# ============================================== 1. the real recheck decision


async def test_the_execution_names_the_decision_it_was_built_on(risk_db, now, trace):
    """The reproduction: a second identifier for a decision nobody made.

    `recheck_decision_id` was derived from the request key, so it pointed at
    nothing. The order was built on a real `RiskDecision`, and that is the one
    the case execution has to name.
    """
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    execution, order, risk, fill, _ = await rows(sessions)
    assert execution.recheck_decision_id == order.risk_id == risk.id
    assert result.recheck_decision_id == risk.id
    assert risk.intent_id == execution.intent_id


async def test_intent_order_and_execution_line_up_after_a_reload(risk_db, now, trace):
    """Case → request → intent → order → execution, checked from the rows."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    execution, order, risk, fill, _ = await rows(sessions)
    assert execution.intent_id == approval.intent_id == fill.intent_id == order.intent_id
    assert execution.order_id == order.id == fill.order_id
    assert execution.execution_id == fill.id
    assert order.risk_id == risk.id == execution.recheck_decision_id
    assert execution.request_id == approval.request_id


async def test_the_basis_keeps_the_recheck_decision_itself(risk_db, now, trace):
    """Recorded, not merely referenced: the row can be checked on its own."""
    from src.core.models import MarketSnapshot, RiskDecision

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    execution, _, risk, _, _ = await rows(sessions)
    decision = RiskDecision.model_validate(execution.basis["recheck_decision"])
    market = MarketSnapshot.model_validate(execution.basis["market_snapshot"])
    assert decision.id == risk.id == execution.recheck_decision_id
    assert decision.market_fingerprint == market.fingerprint()
    assert decision.intent_id == execution.intent_id


async def test_a_replay_returns_the_same_references(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    first = await service.execute_case_fill(case.id, request_key="fill-req")

    again = await service.execute_case_fill(case.id, request_key="fill-req")

    assert again.replayed is True
    assert again.recheck_decision_id == first.recheck_decision_id
    assert (again.order_id, again.execution_id, again.intent_id) == (
        first.order_id,
        first.execution_id,
        first.intent_id,
    )
    assert (await counts(sessions)) == (1, 1)


# ============================================== 2. one authoritative limits set


def test_the_service_cannot_be_given_a_second_limits_set():
    """The structural half. Two settable copies could disagree.

    The failure that produces is the worst available: pre-checks refusing under
    one set while the evaluation that gates the fill runs under another, and the
    stricter recorded in the basis as if it had applied.
    """
    assert "limits" not in CaseFillService.__dataclass_fields__
    assert isinstance(CaseFillService.limits, property)


async def test_the_prechecks_and_the_recheck_share_one_limits_object(risk_db, now, trace):
    _, sessions = risk_db
    _, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)

    assert service.limits is service.paper.limits


async def test_stricter_limits_refuse_rather_than_being_only_documented(risk_db, now, trace):
    """The reproduction: the fill ran under 2500 while 100 was written down."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    strict = RiskLimits(max_position_size_usd=Decimal("100"))
    service = build_fill_service(sessions, now, feed=feed, limits=strict)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert "MAX_POSITION_SIZE" in result.reason_codes
    assert (await counts(sessions)) == (0, 0)
    assert (await read_account(sessions)).cash_usd == Decimal("10000")


async def test_the_basis_documents_exactly_the_limits_that_applied(risk_db, now, trace):
    """What was recorded is what the evaluation used, provably."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    await service.execute_case_fill(case.id, request_key="fill-req")

    execution, _, risk, _, _ = await rows(sessions)
    assert execution.basis["risk_limits"] == service.limits.model_dump(mode="json")
    # The decision's own copy of the per-position ceiling is the same number.
    assert Decimal(risk.payload["position_size_limit_usd"]) == service.limits.max_position_size_usd


async def test_a_paused_account_still_tightens_the_evaluation(risk_db, now, trace):
    """The durable pause is folded into the same limits, under the same lock."""
    from tests.riskrequest.conftest import set_account

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    await set_account(sessions, paused=True)
    service = build_fill_service(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.reason is ExecutionRefusal.SYSTEM_PAUSED
    assert (await counts(sessions)) == (0, 0)


# =========================================== 3. validity at the actual fill


class WaitingClock:
    """Time that moves when real work happens, not when it is read.

    A clock that advances on reads cannot express the situation that matters
    here: the wait is caused by database round trips between the evaluation and
    the fill, and those neither read nor care about the clock.
    """

    def __init__(self, instant) -> None:
        self.instant = instant
        self.reads = 0

    def now(self):
        self.reads += 1
        return self.instant

    def wait(self, delta) -> None:
        self.instant = self.instant + delta


def waiting(sessions, at, *, feed, **overrides):
    clock = WaitingClock(at)
    service = build_fill_service(sessions, at, feed=feed, clock=clock, **overrides)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    return service, clock


def slow_persistence(monkeypatch, clock, delta):
    """Make every real persistence access inside the fill path take time."""
    import src.orchestration.paper as module
    from src.data.repository import append as real_append

    async def slow_append(session, record):
        await real_append(session, record)
        clock.wait(delta)

    monkeypatch.setattr(module, "append", slow_append)


async def test_the_order_is_requested_at_the_instant_the_fill_happens(risk_db, now, trace):
    """The order time is read at the boundary, not carried from before it.

    Persisting the decision, the intent and the market is three database round
    trips, and they take real time. Recording the evaluation instant on the
    order would report a moment that had already passed and hide exactly the
    wait that matters — an older timestamp is an expiry bypass, not a fix.
    """
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service, clock = waiting(sessions, now, feed=feed)

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "paper_fill_recorded"
    _, order, risk, fill, binding = await rows(sessions)
    requested_at = _instant(fill.payload["timing"]["execution_requested_at"])
    assert requested_at == clock.instant
    assert requested_at < aware(binding.expires_at)
    assert requested_at <= _instant(risk.payload["expires_at"])
    assert (await counts(sessions)) == (1, 1)


async def stored_request(sessions):
    async with sessions() as session:
        return await session.scalar(select(TradeCaseRiskRequestRow))


async def short_trigger_case(sessions, now, trace, *, seconds, limits):
    """An approved case whose trigger ages out shortly after the approval."""
    from src.core.models import AgentRole
    from src.orchestration.workflow.models import EvidenceType
    from tests.riskdata.conftest import anchor_payload, record
    from tests.riskrequest.conftest import build_service, ready_case
    from tests.worker.conftest import trigger_payload

    risk = build_service(sessions, now, limits=limits)
    case = await ready_case(risk.cases, now, trace, key="wait-case")
    current = {item.evidence_type: item for item in await risk.cases.evidence(case.id)}
    setup = current[EvidenceType.TRADE_SETUP]
    trigger = await record(
        risk.cases,
        case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"wait-trigger-{case.id}",
        supersedes_id=current[EvidenceType.TRIGGER].evidence_id,
        valid_until=now + timedelta(seconds=seconds),
    )
    # A new trigger is a new instant, so the execution assessment naming the old
    # one is no longer current. The workflow says so and the setup satisfies it.
    await record(
        risk.cases,
        case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        anchor_payload(setup.evidence_id, trigger.evidence_id),
        key=f"wait-anchor-{case.id}",
        supersedes_id=current[EvidenceType.LIQUIDITY_EXECUTION].evidence_id,
    )
    approval = await risk.request_risk_evaluation(case.id, request_key="wait-req")
    assert approval.kind == "risk_request_evaluated"
    return case, risk.markets


@pytest.mark.parametrize(
    ("request_ttl", "fill_ttl", "lifetime", "trigger_seconds", "expected"),
    [
        # The re-check's own window runs out while its records are written.
        (5, 5, None, None, "APPROVAL_EXPIRED"),
        # The scenario the review names: the *original* authorization lapses in
        # that span while the new SENTINEL approval is still open.
        (5, 60, None, None, "AUTHORIZATION_EXPIRED"),
        # Long-lived approvals, and the case itself lapses instead.
        (60, 60, timedelta(seconds=6), None, "TRADE_CASE_TERMINAL"),
        # Long-lived approvals, and a safety envelope lapses instead.
        (60, 60, None, 6, "TRADE_CASE_NOT_AUTHORIZED"),
    ],
)
async def test_time_passing_during_persistence_stops_the_fill(
    risk_db, now, trace, monkeypatch, request_ttl, fill_ttl, lifetime, trigger_seconds, expected
):
    """The reproduction: the wait is between the evaluation and the executor.

    `evaluate()` is followed by three `append` calls, each a database round
    trip. With a clock that moves when work happens rather than when it is read,
    an authorization runs out in that span while the re-check's own window may
    still be open — and the fill was booked anyway, timestamped from before the
    wait.
    """
    approving = RiskLimits(approval_ttl_seconds=request_ttl)
    filling = RiskLimits(approval_ttl_seconds=fill_ttl)
    _, sessions = risk_db
    if trigger_seconds is None:
        case, _, feed = await approved_case(
            sessions, now, trace, lifetime=lifetime, limits=approving, key="wait"
        )
    else:
        case, feed = await short_trigger_case(
            sessions, now, trace, seconds=trigger_seconds, limits=approving
        )
    service, clock = waiting(sessions, now, feed=feed, limits=filling)
    slow_persistence(monkeypatch, clock, timedelta(seconds=3))

    result = await service.execute_case_fill(case.id, request_key="wait-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.EXECUTION_WINDOW_EXPIRED
    assert result.detail == expected
    assert clock.instant > now, "the persistence actually took time"
    # Nothing started survives: no fill, no order, no decision, no position.
    assert (await counts(sessions)) == (0, 0)
    async with sessions() as session:
        for table in (RiskRow, OrderRow, PositionRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
    after = await read_account(sessions)
    assert after.cash_usd == Decimal("10000")
    assert after.fees_paid_usd == Decimal("0")
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.RISK_APPROVED


async def test_a_short_wait_still_books_exactly_one_fill(risk_db, now, trace, monkeypatch):
    """The control. The boundary refuses lapsed windows, not ordinary waits."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace, key="brief")
    service, clock = waiting(sessions, now, feed=feed)
    slow_persistence(monkeypatch, clock, timedelta(milliseconds=200))

    result = await service.execute_case_fill(case.id, request_key="brief-req")

    assert result.kind == "paper_fill_recorded"
    _, _, _, fill, binding = await rows(sessions)
    requested_at = _instant(fill.payload["timing"]["execution_requested_at"])
    # Truthful: after the persistence that preceded it, before the writes that
    # followed, and inside the authorization it ran under.
    assert now < requested_at < clock.instant
    assert requested_at < aware(binding.expires_at)
    assert (await counts(sessions)) == (1, 1)


async def test_the_standalone_paper_path_is_protected_too(risk_db, now, trace, monkeypatch):
    """An expired approval stops the plain service as well, and rolls back."""
    from src.core.models import TradingMode
    from src.data.tables import IntentRow
    from src.orchestration.paper import PaperExecutionExpired, PaperTradingService

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace, key="plain")
    request = await stored_request(sessions)
    intent = TradeIntent.model_validate(request.basis["intent"])
    market = MarketSnapshot.model_validate(request.basis["market_snapshot"])
    clock = WaitingClock(now)
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=clock)
    slow_persistence(monkeypatch, clock, timedelta(seconds=3))

    with pytest.raises(PaperExecutionExpired, match="APPROVAL_EXPIRED"):
        await service.process(intent, market)

    async with sessions() as session:
        for table in (IntentRow, RiskRow, OrderRow, ExecutionRow, PositionRow):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
    assert (await read_account(sessions)).cash_usd == Decimal("10000")


async def test_a_case_lapsing_before_the_boundary_is_not_filled(risk_db, now, trace):
    """Corrected: this previously accepted the late fill as correct.

    It asserted that a case lapsing "in the span" was still booked, attributed
    to the earlier instant everything had been checked at. That reasoning only
    held while no time passed in the span — and time does pass, because the span
    is three database round trips. A case that has lapsed by the time the
    execution boundary is reached is not filled.
    """
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace, lifetime=timedelta(seconds=5))
    service, _ = stepping(sessions, now, feed=feed, step=timedelta(seconds=3))

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "execution_refused"
    assert (await counts(sessions)) == (0, 0)
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.RISK_APPROVED


async def test_a_case_already_lapsed_at_the_decision_instant_is_refused(risk_db, now, trace):
    """The other side of the boundary: no fill, and no artificial rejection.

    Nothing is persisted — not a risk row, not an order, not a position — so an
    expiry never becomes a terminal risk verdict about the market.
    """
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace, lifetime=timedelta(seconds=2))
    service, _ = stepping(sessions, now, feed=feed, step=timedelta(seconds=3))

    result = await service.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind == "execution_refused"
    assert result.reason in (
        ExecutionRefusal.TRADE_CASE_TERMINAL,
        ExecutionRefusal.AUTHORIZATION_EXPIRED,
    )
    assert (await counts(sessions)) == (0, 0)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RiskRow)) == 0
        assert await session.scalar(select(func.count()).select_from(OrderRow)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionRow)) == 0


async def test_safety_evidence_lapsing_before_the_decision_instant_is_refused(risk_db, now, trace):
    """A trigger that ages out in the span withdraws the authorization."""
    from src.core.models import AgentRole
    from src.orchestration.workflow.models import EvidenceType
    from tests.riskdata.conftest import record
    from tests.riskrequest.conftest import build_service, ready_case
    from tests.worker.conftest import trigger_payload

    _, sessions = risk_db
    risk = build_service(sessions, now)
    case = await ready_case(risk.cases, now, trace, key="lapse-case")
    setup = next(
        item
        for item in await risk.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.TRADE_SETUP
    )
    previous = next(
        item
        for item in await risk.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.TRIGGER
    )
    trigger = await record(
        risk.cases,
        case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"lapse-trigger-{case.id}",
        supersedes_id=previous.evidence_id,
        valid_until=now + timedelta(seconds=2),
    )
    # A new trigger is a new instant, so the execution assessment that named the
    # old one is no longer current. The workflow says so, and the setup has to
    # satisfy it rather than talk around it.
    from tests.riskdata.conftest import anchor_payload

    old_anchor = next(
        item
        for item in await risk.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.LIQUIDITY_EXECUTION
    )
    await record(
        risk.cases,
        case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        anchor_payload(setup.evidence_id, trigger.evidence_id),
        key=f"lapse-anchor-{case.id}",
        supersedes_id=old_anchor.evidence_id,
    )
    approval = await risk.request_risk_evaluation(case.id, request_key="lapse-req")
    assert approval.kind == "risk_request_evaluated"

    service, _ = stepping(sessions, now, feed=risk.markets, step=timedelta(seconds=3))
    result = await service.execute_case_fill(case.id, request_key="lapse-req")

    assert result.kind == "execution_refused"
    assert (await counts(sessions)) == (0, 0)


async def test_an_aborted_run_books_nothing(risk_db, now, trace, monkeypatch):
    """One transaction, so there is no partial execution to reconcile."""
    import src.orchestration.casefill.service as module

    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    before = await read_account(sessions)
    service, _ = waiting(sessions, now, feed=feed)

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


def test_nothing_touches_the_database_between_the_boundary_and_the_fill():
    """The ordering, asserted on the source rather than only in behaviour.

    The clock *is* read at the execution boundary — that is the correction. What
    must not happen is a database access between that read and the fill: each
    one waits, and the checks would then describe an instant the execution never
    took place at.
    """
    from src.orchestration.paper import PaperTradingService

    source = textwrap.dedent(inspect.getsource(PaperTradingService.execute_in_session))
    lines = source.splitlines()
    boundary = next(
        index
        for index, line in enumerate(lines)
        if "execution_requested_at = self._clock.now()" in line
    )
    simulation = next(index for index, line in enumerate(lines) if "self.executor.execute(" in line)
    between = lines[boundary:simulation]
    for forbidden in ("await append(", "session.get(", "session.scalar(", "session.execute("):
        assert not any(forbidden in line for line in between), forbidden
    # And the boundary read exists at all, which is the point of the change.
    assert boundary < simulation


async def test_a_completed_fill_still_replays_after_everything_expired(risk_db, now, trace):
    """The boundary tightened; history did not move."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    first = await service.execute_case_fill(case.id, request_key="fill-req")

    from tests.riskdata.conftest import RecordedMarkets
    from tests.riskrequest.conftest import fresh_snapshot

    much_later = now + timedelta(days=4)
    later = build_fill_service(
        sessions, much_later, feed=RecordedMarkets(fresh_snapshot(much_later))
    )
    again = await later.execute_case_fill(case.id, request_key="fill-req")

    assert again.replayed is True
    assert again.execution_id == first.execution_id
    assert again.recheck_decision_id == first.recheck_decision_id
    assert (await counts(sessions)) == (1, 1)
