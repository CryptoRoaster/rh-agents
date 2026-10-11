"""Every BUY path refuses under either entry-only halt; no SELL path does.

Over the real chain: the real intake, risk request, fill, re-entry and paper
executor, each with the real SENTINEL behind it. The halt is put in force the
way the system does it — with its audit event — and every assertion is about
what was or was not written.
"""

import asyncio
import os

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits, TradingMode
from src.data.tables import (
    AccountRow,
    ExecutionRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
    TradeCaseRiskRequestRow,
    TradeCaseRow,
)
from src.orchestration.commander.context import AccountPauseReader
from src.orchestration.commander.intake import IntakeRefusal
from src.orchestration.entryhalt.models import ActorKind
from src.orchestration.entryhalt.service import EntryHaltService, halt_entries_automatically
from src.orchestration.paper import EntryHaltedError, PaperTradingService
from tests.casefill.conftest import approved_case, build_fill_service
from tests.commander.test_intake import candidate_for, service_for
from tests.entryhalt.conftest import KINDS, account_of, halt, rows
from tests.paperexit.conftest import build_exit_service, entered, market_feed
from tests.reentry.conftest import build_reentry_service, closed_cycle, cycles
from tests.riskrequest.conftest import build_service, ready_case, set_account

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)
HALTS = pytest.mark.parametrize("kind", KINDS)


class SnapshotMissesTheHalt(AccountPauseReader):
    """The real reader, except its unlocked view: a halt set after the snapshot."""

    async def entry_halt(self):
        return None


# ------------------------------------------------------------------ intake


@HALTS
async def test_intake_opens_no_case_under_a_halt(risk_db, now, kind):
    _, sessions = risk_db
    await halt(sessions, kind, now)
    service = service_for(sessions, now, [candidate_for(now)], pause=AccountPauseReader(sessions))

    outcome = await service.run_cycle()

    assert outcome.opened_count == 0
    assert outcome.refused == (("*", IntakeRefusal.ENTRY_HALTED),)
    assert await rows(sessions, TradeCaseRow) == 0


@HALTS
async def test_intake_checks_the_halt_again_under_the_account_lock(risk_db, now, kind):
    _, sessions = risk_db
    await halt(sessions, kind, now)
    candidate, snapshot = candidate_for(now)
    service = service_for(
        sessions, now, [(candidate, snapshot)], pause=SnapshotMissesTheHalt(sessions)
    )

    outcome = await service.run_cycle()

    assert outcome.opened_count == 0
    assert outcome.refused == ((candidate.pair_id, IntakeRefusal.ENTRY_HALTED),)
    assert await rows(sessions, TradeCaseRow) == 0


# ------------------------------------------------------------ risk request


@HALTS
async def test_a_risk_request_is_refused_before_sentinel_and_spends_nothing(
    risk_db, now, trace, kind
):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await halt(sessions, kind, now)

    refused = await service.request_risk_evaluation(case.id, request_key="halted-req")

    assert refused.kind == "risk_request_refused"
    assert refused.reason.value == "ENTRY_HALTED"
    assert refused.detail == kind.upper()
    # Nothing recorded: the case keeps its one request for after the halt.
    assert await rows(sessions, TradeCaseRiskRequestRow) == 0


async def test_after_the_manual_halt_is_lifted_the_same_case_may_ask(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await halt(sessions, "manual", now)
    assert (await service.request_risk_evaluation(case.id, request_key="k")).kind == (
        "risk_request_refused"
    )

    cleared = await EntryHaltService(sessions, clock=FixedClock(now)).clear_manual(
        reason="REVIEW_DONE", actor="test"
    )
    assert cleared.changed
    answered = await service.request_risk_evaluation(case.id, request_key="k")

    assert answered.kind == "risk_request_evaluated", answered


# ---------------------------------------------------------------------- fill


@HALTS
async def test_an_approval_granted_before_the_halt_buys_nothing(risk_db, now, trace, kind):
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace)
    assert approval.kind == "risk_request_evaluated"
    await halt(sessions, kind, now)
    fills_before = await rows(sessions, ExecutionRow)

    refused = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="fill-req"
    )

    assert refused.kind == "execution_refused"
    assert refused.reason.value == "ENTRY_HALTED"
    assert await rows(sessions, ExecutionRow) == fills_before
    assert await rows(sessions, TradeCaseExecutionRow) == 0
    assert (await account_of(sessions)).cash_usd == 10000


@HALTS
async def test_a_booked_fill_replays_under_a_halt_and_books_nothing_new(risk_db, now, trace, kind):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    service = build_fill_service(sessions, now, feed=feed)
    first = await service.execute_case_fill(case.id, request_key="fill-req")
    assert first.kind == "paper_fill_recorded"
    cash = (await account_of(sessions)).cash_usd
    await halt(sessions, kind, now)

    again = await service.execute_case_fill(case.id, request_key="fill-req")

    assert again.kind == "paper_fill_recorded" and again.replayed
    assert await rows(sessions, TradeCaseExecutionRow) == 1
    assert (await account_of(sessions)).cash_usd == cash


# ----------------------------------------------------------------- re-entry


@HALTS
async def test_no_successor_cycle_opens_under_a_halt(risk_db, now, trace, kind):
    _, sessions = risk_db
    _, _, _, sale = await closed_cycle(sessions, now, trace)
    await halt(sessions, kind, now)
    before = len(await cycles(sessions))

    refused = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="reentry"
    )

    assert refused.reason.value == "ENTRY_HALTED", refused
    assert len(await cycles(sessions)) == before


# ------------------------------------------------- the executor's backstop


@HALTS
async def test_the_paper_executor_refuses_a_direct_buy_under_a_halt(
    risk_db, now, intent, market, kind
):
    _, sessions = risk_db
    await halt(sessions, kind, now)
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))

    with pytest.raises(EntryHaltedError):
        await service.process(intent, market)

    assert await rows(sessions, ExecutionRow) == 0
    assert (await account_of(sessions)).cash_usd == 10000


# ---------------------------------------------------------------------- SELL


@HALTS
async def test_a_sell_is_not_stopped_by_an_entry_halt(risk_db, now, trace, kind):
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    await halt(sessions, kind, now)

    sale = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="halted-sale"
    )

    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    assert await rows(sessions, TradeCaseExitRow) == 1


async def test_a_pause_still_stops_a_sell_under_a_halt(risk_db, now, trace):
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    await halt(sessions, "manual", now)
    await set_account(sessions, paused=True)

    refused = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="paused-sale"
    )

    assert refused.reason.value == "SYSTEM_PAUSED"
    assert await rows(sessions, TradeCaseExitRow) == 0


async def test_a_kill_switch_still_stops_a_sell_under_a_halt(risk_db, now, trace):
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    await halt(sessions, "auto", now)

    refused = await build_exit_service(
        sessions, now, feed=feed, kill_switch=True
    ).execute_position_exit(position.id, request_key="killed-sale")

    assert refused.reason.value == "KILL_SWITCH_ENGAGED"
    assert await rows(sessions, TradeCaseExitRow) == 0


# --------------------------------------------------------- unreadable state


async def test_a_missing_account_row_is_unreadable_never_unhalted(risk_db):
    from sqlalchemy import delete

    from src.orchestration.commander.context import SystemPauseUnavailable
    from src.orchestration.entryhalt.models import EntryHaltUnavailable, halt_detail

    _, sessions = risk_db
    async with sessions.begin() as session:
        await session.execute(delete(AccountRow))
    reader = AccountPauseReader(sessions)

    with pytest.raises(SystemPauseUnavailable):
        await reader.entry_halt()
    async with sessions.begin() as session:
        with pytest.raises(SystemPauseUnavailable):
            await reader.locked_entry_halt(session)
    with pytest.raises(EntryHaltUnavailable):
        await EntryHaltService(sessions).status()
    # A value nobody can read as "not halted" is a halt.
    assert halt_detail(None, False) == "MANUAL"
    assert halt_detail(False, None) == "AUTO"


# ---------------------------------------------------------------- PostgreSQL


@POSTGRES
async def test_a_fill_waiting_on_the_lock_sees_the_halt_committed_before_it(risk_db, now, trace):
    """The halt holds the account lock; the fill queues behind it and is refused."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    fill = build_fill_service(sessions, now, feed=feed)

    async with sessions.begin() as session:
        account = await session.scalar(
            select(AccountRow).where(AccountRow.id == 1).with_for_update()
        )
        await halt_entries_automatically(
            session,
            account,
            reason="DAILY_LOSS_LIMIT",
            at=now,
            actor_kind=ActorKind.SENTINEL,
            limits=RiskLimits(),
        )
        waiting = asyncio.create_task(fill.execute_case_fill(case.id, request_key="fill-req"))
        await asyncio.sleep(0.5)
        assert not waiting.done()

    refused = await asyncio.wait_for(waiting, timeout=30)
    assert refused.reason.value == "ENTRY_HALTED"
    assert await rows(sessions, TradeCaseExecutionRow) == 0


@POSTGRES
async def test_racing_fills_and_a_halt_never_book_after_the_halt(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    fill = build_fill_service(sessions, now, feed=feed)
    service = EntryHaltService(sessions, clock=FixedClock(now))

    results = await asyncio.gather(
        fill.execute_case_fill(case.id, request_key="fill-req"),
        service.set_manual(reason="OPERATOR_REVIEW", actor="race"),
        fill.execute_case_fill(case.id, request_key="fill-req"),
    )

    # At most one booking, whichever order the database chose; every fill
    # that came after the halt is refused by name. (The lock-order proof is
    # the test above: a fill queued behind the halt's lock sees the halt.)
    assert await rows(sessions, TradeCaseExecutionRow) <= 1
    assert results[1].changed
    for item in (results[0], results[2]):
        assert item.kind == "paper_fill_recorded" or item.reason.value == "ENTRY_HALTED", item
