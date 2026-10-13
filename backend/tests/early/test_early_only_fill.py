"""PAPER_EARLY_ONLY at the last two BUY lines: the case fill and the paper executor.

The runner already leaves normal cases alone and the risk request refuses them,
but neither is the place a BUY happens. An approval stored before the switch
was turned on, or a direct call to either service, must still buy nothing that
is not early. Sales of every kind, replays of what was already booked and every
existing stop are exactly what they were. No provider is called here.
"""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest

from src.core.clock import FixedClock
from src.core.models import RiskLimits, TradingMode
from src.data.tables import (
    ExecutionRow,
    IntentRow,
    PositionRow,
    RiskRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
    TradeCaseRiskRequestRow,
)
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.paper import NormalEntryDisabledError, PaperTradingService
from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1
from tests.casefill.conftest import approved_case, build_fill_service
from tests.early.test_early_only import early_only
from tests.early.test_exit import early_entry, exit_rows, observe, swept
from tests.early.test_fill_caps import Book
from tests.entryhalt.conftest import account_of, halt, rows
from tests.jobruns.conftest import GATE, exit_run
from tests.paperexit.conftest import build_exit_service, entered, market_feed
from tests.runner.conftest import stack_for
from tests.scout.test_promotion import NOW

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)
RECORDS = (ExecutionRow, IntentRow, RiskRow, TradeCaseExecutionRow, TradeCaseRiskRequestRow)


def early_paper(sessions, now, **kw):
    return PaperTradingService(
        sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now), early_only=True, **kw
    )


async def counts(sessions):
    return {table.__name__: await rows(sessions, table) for table in (*RECORDS, PositionRow)}


# ------------------------------------------------------------- the case fill


async def test_an_old_normal_approval_buys_nothing_in_early_only(risk_db, now, trace):
    """Approved before the switch; filled after it. Refused, and nothing written."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace)
    assert approval.kind == "risk_request_evaluated"
    assert case.strategy_policy_id is None
    before = await counts(sessions)
    status = await build_fill_service(sessions, now, feed=feed).cases.get_trade_case(case.id)

    refused = await build_fill_service(sessions, now, feed=feed, early_only=True).execute_case_fill(
        case.id, request_key="fill-req"
    )

    assert refused.kind == "execution_refused"
    assert refused.reason is ExecutionRefusal.NORMAL_ENTRY_DISABLED
    # No order, no re-check decision, no fill, no position, no cash moved.
    assert await counts(sessions) == before
    assert (await account_of(sessions)).cash_usd == 10000
    after = await build_fill_service(sessions, now, feed=feed).cases.get_trade_case(case.id)
    assert (after.status, after.revision) == (status.status, status.revision)


async def test_the_same_approval_still_fills_with_the_switch_off(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)

    filled = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="fill-req"
    )

    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)


async def test_the_refused_approval_fills_once_the_switch_is_off_again(risk_db, now, trace):
    """Refused, not spent: the case keeps its one request."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    refused = await build_fill_service(sessions, now, feed=feed, early_only=True).execute_case_fill(
        case.id, request_key="fill-req"
    )
    assert refused.reason is ExecutionRefusal.NORMAL_ENTRY_DISABLED

    filled = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="fill-req"
    )

    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)


async def test_a_valid_early_case_still_fills_in_early_only(risk_db, now):
    _, sessions = risk_db
    book = Book(sessions, now)
    trade_case = await book.approved(0, uuid4())
    assert trade_case.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1

    filled = await build_fill_service(
        sessions, now, feed=book.feed, early_only=True, paper=early_paper(sessions, now)
    ).execute_case_fill(trade_case.id, request_key="early-0")

    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)
    assert await rows(sessions, TradeCaseExecutionRow) == 1


async def test_a_booked_normal_fill_replays_in_early_only_and_books_nothing(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    first = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="fill-req"
    )
    assert first.kind == "paper_fill_recorded"
    before = await counts(sessions)
    cash = (await account_of(sessions)).cash_usd

    again = await build_fill_service(
        sessions, now, feed=feed, early_only=True, paper=early_paper(sessions, now)
    ).execute_case_fill(case.id, request_key="fill-req")

    assert again.kind == "paper_fill_recorded" and again.replayed
    assert again.execution_id == first.execution_id
    assert await counts(sessions) == before
    assert (await account_of(sessions)).cash_usd == cash


async def test_an_entry_halt_is_still_named_first(risk_db, now, trace):
    """Early-only adds a stop; it does not replace one."""
    _, sessions = risk_db
    book = Book(sessions, now)
    trade_case = await book.approved(0, uuid4())
    await halt(sessions, "manual", now)

    refused = await build_fill_service(
        sessions, now, feed=book.feed, early_only=True
    ).execute_case_fill(trade_case.id, request_key="early-0")

    assert refused.reason is ExecutionRefusal.ENTRY_HALTED
    assert await rows(sessions, TradeCaseExecutionRow) == 0


async def test_exit_job_stale_still_stops_an_early_fill(risk_db, now):
    _, sessions = risk_db
    book = Book(sessions, now)
    trade_case = await book.approved(0, uuid4())
    await exit_run(sessions, now - timedelta(seconds=60), ready=False)

    refused = await build_fill_service(
        sessions, now, feed=book.feed, early_only=True, exit_liveness=GATE
    ).execute_case_fill(trade_case.id, request_key="early-0")

    assert (refused.reason, refused.detail) == (
        ExecutionRefusal.EXIT_JOB_STALE,
        "EXIT_JOB_UNHEALTHY",
    )
    assert await rows(sessions, TradeCaseExecutionRow) == 0


async def test_the_composed_fill_and_executor_carry_the_switch(risk_db):
    _, sessions = risk_db
    stack = stack_for(sessions, early_only(), NOW)
    assert stack.fills.early_only is True
    assert stack.fills.paper.early_only is True
    default = stack_for(sessions, early_only(paper_early_only=False), NOW)
    assert default.fills.early_only is False
    assert default.fills.paper.early_only is False


# --------------------------------------------------------- the paper executor


async def test_the_executor_refuses_a_direct_buy_in_early_only(risk_db, now, intent, market):
    """The standalone path has no case and so no strategy: never early."""
    _, sessions = risk_db
    before = await counts(sessions)

    with pytest.raises(NormalEntryDisabledError):
        await early_paper(sessions, now).process(intent, market)

    assert await counts(sessions) == before
    assert (await account_of(sessions)).cash_usd == 10000


async def test_the_executor_refuses_a_normal_case_buy_that_reaches_it(risk_db, now, trace):
    """A fill service built without the switch, over an executor with it."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    before = await counts(sessions)

    with pytest.raises(NormalEntryDisabledError):
        await build_fill_service(
            sessions, now, feed=feed, paper=early_paper(sessions, now)
        ).execute_case_fill(case.id, request_key="fill-req")

    assert await counts(sessions) == before


async def test_the_executor_still_buys_directly_with_the_switch_off(risk_db, now, intent, market):
    _, sessions = risk_db
    service = PaperTradingService(sessions, RiskLimits(), TradingMode.PAPER, clock=FixedClock(now))

    await service.process(intent, market)

    assert await rows(sessions, ExecutionRow) == 1


# ---------------------------------------------------------------------- SELL


async def test_a_normal_sell_is_not_stopped_by_early_only(risk_db, now, trace):
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)

    sale = await build_exit_service(
        sessions, now, feed=feed, paper=early_paper(sessions, now)
    ).execute_position_exit(position.id, request_key="early-only-sale")

    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    assert await rows(sessions, TradeCaseExitRow) == 1


async def test_an_early_sell_is_not_stopped_by_early_only(risk_db, now, monkeypatch):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.50")
    # Every executor the sweep builds runs with the switch on.
    original = PaperTradingService.__init__

    def switched(self, *args, **kwargs):
        original(self, *args, **{**kwargs, "early_only": True})

    monkeypatch.setattr(PaperTradingService, "__init__", switched)

    result = await swept(sessions, at)

    assert (result.evaluated, result.triggered, result.executed) == (1, 1, 1), result
    assert len(await exit_rows(sessions)) == 1


# ------------------------------------------------------------- concurrency


@POSTGRES
async def test_racing_old_approvals_never_book_a_normal_buy(risk_db, now, trace):
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace)
    fill = build_fill_service(
        sessions, now, feed=feed, early_only=True, paper=early_paper(sessions, now)
    )

    results = await asyncio.gather(
        *(fill.execute_case_fill(case.id, request_key="fill-req") for _ in range(4))
    )

    assert {item.reason for item in results} == {ExecutionRefusal.NORMAL_ENTRY_DISABLED}
    assert await rows(sessions, TradeCaseExecutionRow) == 0
    assert await rows(sessions, ExecutionRow) == 0
