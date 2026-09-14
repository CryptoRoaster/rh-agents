"""Two callers, one case, and the guarantees that survive them.

PostgreSQL only. Row locks are what these prove, and SQLite has none — a test
that ran there would report a pass it had not earned.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from src.core.models import RiskOutcome
from src.data.tables import AccountRow, TradeCaseRiskBindingRow, TradeCaseRiskRequestRow
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.workflow.models import EvidenceType, TradeCaseStatus
from tests.riskdata.conftest import holder_block, record_onchain
from tests.riskrequest.conftest import (
    FRESH,
    build_service,
    fresh_onchain,
    read_account,
    ready_case,
    set_account,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def counts(sessions):
    async with sessions() as session:
        requests = await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        bindings = await session.scalar(select(func.count()).select_from(TradeCaseRiskBindingRow))
    return requests, bindings


async def test_two_callers_with_one_key_produce_one_logical_result(risk_db, now, trace):
    """One evaluation, one replay, one binding. Never two verdicts."""
    _, sessions = risk_db
    first = build_service(sessions, now)
    case = await ready_case(first.cases, now, trace)
    second = build_service(sessions, now, feed=first.markets)

    results = await asyncio.gather(
        first.request_risk_evaluation(case.id, request_key="rr-race"),
        second.request_risk_evaluation(case.id, request_key="rr-race"),
    )

    assert all(item.kind == "risk_request_evaluated" for item in results)
    assert sorted(item.replayed for item in results) == [False, True]
    assert len({item.request_id for item in results}) == 1
    assert len({item.risk_decision_id for item in results}) == 1
    assert len({item.risk_request_digest for item in results}) == 1
    assert (await counts(sessions)) == (1, 1)


async def test_two_callers_with_two_keys_leave_one_request_standing(risk_db, now, trace):
    """A case has one canonical trade request, whoever asks second."""
    _, sessions = risk_db
    first = build_service(sessions, now)
    case = await ready_case(first.cases, now, trace)
    second = build_service(sessions, now, feed=first.markets, notional="750")

    results = await asyncio.gather(
        first.request_risk_evaluation(case.id, request_key="rr-key-a"),
        second.request_risk_evaluation(case.id, request_key="rr-key-b"),
    )

    kinds = sorted(item.kind for item in results)
    assert kinds == ["risk_request_evaluated", "risk_request_refused"]
    refused = next(item for item in results if item.kind == "risk_request_refused")
    assert refused.reason is RiskRequestRefusal.RISK_REQUEST_ALREADY_EXISTS
    assert (await counts(sessions)) == (1, 1)


async def test_a_pause_and_a_request_cannot_both_win(risk_db, now, trace):
    """They serialise on the account row, so one observes the other's commit.

    Either the pause committed first and the request refused, or the request
    committed first and the pause landed after it — never a binding written
    while the stop was already in force.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    async def pause() -> None:
        async with sessions.begin() as session:
            await session.execute(select(AccountRow).where(AccountRow.id == 1).with_for_update())
            await session.execute(update(AccountRow).where(AccountRow.id == 1).values(paused=True))

    result, _ = await asyncio.gather(
        service.request_risk_evaluation(case.id, request_key="rr-pause-race"), pause()
    )

    assert (await read_account(sessions)).paused is True
    requests, bindings = await counts(sessions)
    if result.kind == "risk_request_refused":
        assert result.reason is RiskRequestRefusal.SYSTEM_PAUSED
        assert (requests, bindings) == (0, 0)
    else:
        assert (requests, bindings) == (1, 1)
        assert result.outcome in (RiskOutcome.APPROVE, RiskOutcome.REJECT)


async def test_a_pause_committed_first_is_never_stepped_over(risk_db, now, trace):
    """The deterministic half of the race, with the ordering forced."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await set_account(sessions, paused=True)

    result = await service.request_risk_evaluation(case.id, request_key="rr-after-pause")

    assert result.reason is RiskRequestRefusal.SYSTEM_PAUSED
    assert (await counts(sessions)) == (0, 0)


async def test_evidence_cannot_change_under_a_request_in_flight(risk_db, now, trace):
    """The case lock makes the basis stable for the whole transaction.

    A concurrent supersession queues behind the request rather than landing
    inside it, so the binding can never rest on a source the case has left.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    previous = next(
        item
        for item in await service.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.ONCHAIN
    )

    async def supersede() -> None:
        await record_onchain(
            service.cases,
            case,
            now,
            fresh_onchain(now, holders=holder_block(now, age=FRESH, holder_count=7)),
            key=f"rr-race-atlas-{case.id}",
            supersedes_id=previous.evidence_id,
        )

    result, _ = await asyncio.gather(
        service.request_risk_evaluation(case.id, request_key="rr-evidence-race"), supersede()
    )

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    if result.kind == "risk_request_evaluated":
        # Whichever ordering won, the stored basis and the binding agree.
        assert row.risk_input_digest == result.risk_input_digest
        assert row.basis["safety_risk_input_digest"] == result.risk_input_digest
        assert (await counts(sessions)) == (1, 1)
    else:
        assert row is None


async def test_concurrent_requests_on_two_cases_stay_separate(risk_db, now, trace):
    """Serialising on one account must not merge two cases into one verdict."""
    from uuid import uuid4

    _, sessions = risk_db
    service = build_service(sessions, now)
    first = await ready_case(service.cases, now, trace, key="rr-multi-a")
    second = await ready_case(service.cases, now, uuid4(), key="rr-multi-b")

    results = await asyncio.gather(
        service.request_risk_evaluation(first.id, request_key="rr-multi-key-a"),
        service.request_risk_evaluation(second.id, request_key="rr-multi-key-b"),
    )

    assert all(item.kind == "risk_request_evaluated" for item in results)
    assert {item.trade_case_id for item in results} == {first.id, second.id}
    assert len({item.intent_id for item in results}) == 2
    assert (await counts(sessions)) == (2, 2)


async def test_a_second_case_sees_the_first_case_s_committed_portfolio(risk_db, now, trace):
    """Cash is read under the lock, so two requests never share a stale figure."""
    from uuid import uuid4

    _, sessions = risk_db
    service = build_service(sessions, now, notional="3000")
    first = await ready_case(service.cases, now, trace, key="rr-cash-a")
    await service.request_risk_evaluation(first.id, request_key="rr-cash-key-a")

    await set_account(sessions, cash_usd=Decimal("100"))
    second = await ready_case(service.cases, now, uuid4(), key="rr-cash-b")
    result = await service.request_risk_evaluation(second.id, request_key="rr-cash-key-b")

    assert result.kind == "risk_request_evaluated"
    assert "INSUFFICIENT_CASH" in result.reason_codes
    async with sessions() as session:
        rows = (await session.scalars(select(TradeCaseRiskRequestRow))).all()
    basis = next(row.basis for row in rows if row.trade_case_id == second.id)
    assert basis["portfolio"]["cash_usd"] == "100"


async def test_a_completed_request_leaves_the_case_where_the_evaluator_put_it(risk_db, now, trace):
    """The workflow evaluator remains the status owner, under concurrency too."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    await asyncio.gather(
        service.request_risk_evaluation(case.id, request_key="rr-status"),
        service.request_risk_evaluation(case.id, request_key="rr-status"),
    )

    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_APPROVED
    assert after.revision > case.revision
