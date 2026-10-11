"""The daily loss limit: a halted entry, never a halted exit.

SENTINEL refuses a BUY at the limit and the refusal sets the automatic entry
halt beside it; a SELL is never refused for the limit, and a booked SELL that
takes the day's realised loss to the limit sets the halt in its own
transaction. The kill switch and the durable pause are unchanged full stops.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.core.models import RiskLimits
from src.data.tables import AccountRow, PaperControlEventRow, TradeCaseExecutionRow
from src.orchestration.paperexit.service import PaperExitService
from tests.casefill.conftest import approved_case, build_fill_service
from tests.entryhalt.conftest import account_of, event_count, halt, rows
from tests.paperexit.conftest import build_exit_service, entered, exits, market_feed
from tests.riskrequest.conftest import build_service, ready_case, set_account

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)
LIMIT = RiskLimits().daily_loss_limit_usd


async def events(sessions):
    async with sessions() as session:
        return (
            await session.scalars(
                select(PaperControlEventRow).order_by(PaperControlEventRow.sequence)
            )
        ).all()


async def held(sessions, now, trace):
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    return feed, position


async def held_elsewhere(sessions, now, trace):
    """A booked position on a second market, so a case on the first stays open to buy."""
    from tests.riskrequest.conftest import fresh_snapshot
    from tests.runner.test_position_refresh import NORMAL

    identity = NORMAL[0]
    feed = market_feed(now)
    feed.replace(
        fresh_snapshot(now, pair_id=identity.pair_id, base_asset_id=identity.base_asset_id)
    )
    _, _, position = await entered(
        sessions, now, trace, key="elsewhere", feed=feed, identity=identity
    )
    return feed, position


async def sell(sessions, now, feed, position, key="sale"):
    return await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key=key
    )


# ----------------------------------------------------------------------- BUY


async def test_a_buy_at_the_limit_is_refused_and_halts_entries_with_its_event(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())

    verdict = await service.request_risk_evaluation(case.id, request_key="at-limit")

    assert verdict.outcome.value == "REJECT"
    [event] = await events(sessions)
    account = await account_of(sessions)
    assert (event.control, event.action, event.actor_kind) == (
        "ENTRY_HALT_AUTO",
        "SET",
        "SENTINEL",
    )
    assert event.reason_code == "DAILY_LOSS_LIMIT"
    assert event.risk_decision_id == account.entry_halt_auto_decision_id is not None
    assert account.entry_halt_auto_loss_day == now.date()
    assert account.paused is False


async def test_a_halt_already_in_force_is_not_set_again(risk_db, now, trace):
    _, sessions = risk_db
    await halt(sessions, "auto", now)
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())

    refused = await service.request_risk_evaluation(case.id, request_key="again")

    # Refused by the halt before SENTINEL is asked: no second verdict, no second event.
    assert refused.reason.value == "ENTRY_HALTED"
    assert await event_count(sessions) == 1


# ---------------------------------------------------------------------- SELL


async def test_a_sale_below_the_limit_halts_nothing(risk_db, now, trace):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)

    sale = await sell(sessions, now, feed, position)

    assert sale.kind == "paper_exit_recorded"
    assert (await account_of(sessions)).entry_halt_auto is False
    assert await event_count(sessions) == 0


async def test_a_sale_that_reaches_the_limit_halts_entries_in_its_own_booking(risk_db, now, trace):
    """Fees alone make this sale a loss; it takes the day from just under to the limit."""
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await set_account(
        sessions, realized_loss_today_usd=LIMIT - Decimal("0.000001"), loss_day=now.date()
    )

    sale = await sell(sessions, now, feed, position)

    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    account = await account_of(sessions)
    assert account.realized_loss_today_usd >= LIMIT
    assert (account.entry_halt_auto, account.paused) == (True, False)
    [event] = await events(sessions)
    assert (event.actor_kind, event.reason_code, event.action) == (
        "LEDGER",
        "DAILY_LOSS_LIMIT",
        "SET",
    )
    assert event.realized_loss_today_usd == account.realized_loss_today_usd


async def test_a_sale_past_the_limit_with_a_manual_halt_adds_the_automatic_one(risk_db, now, trace):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await halt(sessions, "manual", now)
    await set_account(sessions, realized_loss_today_usd=LIMIT * 2, loss_day=now.date())

    assert (await sell(sessions, now, feed, position)).kind == "paper_exit_recorded"

    account = await account_of(sessions)
    assert (account.entry_halt_manual, account.entry_halt_auto) == (True, True)


async def test_a_sale_whose_booking_fails_leaves_no_halt_and_no_event(
    risk_db, now, trace, monkeypatch
):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())

    def broken(self, *args, **kwargs):
        raise RuntimeError("the exit record could not be written")

    monkeypatch.setattr(PaperExitService, "_record", broken)
    with pytest.raises(RuntimeError):
        await sell(sessions, now, feed, position)

    account = await account_of(sessions)
    assert (account.entry_halt_auto, account.realized_loss_today_usd) == (False, LIMIT)
    assert await event_count(sessions) == 0
    assert await exits(sessions) == []


async def test_a_replayed_sale_writes_no_second_halt_event(risk_db, now, trace):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())

    first = await sell(sessions, now, feed, position, key="once")
    again = await sell(sessions, now, feed, position, key="once")

    assert first.kind == again.kind == "paper_exit_recorded" and again.replayed
    assert await event_count(sessions) == 1
    assert len(await exits(sessions)) == 1


async def test_a_kill_switch_still_refuses_a_sale_at_the_limit(risk_db, now, trace):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())

    refused = await build_exit_service(
        sessions, now, feed=feed, kill_switch=True
    ).execute_position_exit(position.id, request_key="killed")

    assert refused.reason.value == "KILL_SWITCH_ENGAGED"
    assert await event_count(sessions) == 0


async def test_a_paused_account_still_refuses_a_sale_and_is_not_unpaused(risk_db, now, trace):
    _, sessions = risk_db
    feed, position = await held(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date(), paused=True)

    refused = await sell(sessions, now, feed, position)

    assert refused.reason.value == "SYSTEM_PAUSED"
    assert (await account_of(sessions)).paused is True


# ------------------------------------------------------------- the boundary


@pytest.mark.parametrize(
    ("loss", "halted"),
    [("499.999999999999999999", False), ("500", True), ("500.000000000000000001", True)],
)
async def test_the_ledger_halts_exactly_at_the_limit(risk_db, now, loss, halted):
    from src.orchestration.entryhalt.service import halt_on_realized_loss

    _, sessions = risk_db
    async with sessions.begin() as session:
        account = await session.scalar(
            select(AccountRow).where(AccountRow.id == 1).with_for_update()
        )
        account.realized_loss_today_usd = Decimal(loss)
        account.loss_day = now.date()
        assert await halt_on_realized_loss(session, account, limits=RiskLimits(), at=now) is halted


# ------------------------------------------------- an approval met by a halt


async def test_an_approval_granted_before_a_sale_reached_the_limit_buys_nothing(
    risk_db, now, trace
):
    """Approved under the limit; a sale then halts entries; the fill is refused by the halt."""
    _, sessions = risk_db
    case, approval, feed = await approved_case(sessions, now, trace, key="approved")
    assert approval.kind == "risk_request_evaluated"
    other, position = await held_elsewhere(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())
    assert (await sell(sessions, now, other, position)).kind == "paper_exit_recorded"

    refused = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="approved-req"
    )

    assert refused.reason.value == "ENTRY_HALTED"
    assert await rows(sessions, TradeCaseExecutionRow) == 1  # the sold entry only


# ---------------------------------------------------------------- PostgreSQL


@POSTGRES
async def test_a_fill_queued_behind_a_limit_reaching_sale_meets_the_halt(risk_db, now, trace):
    """Same account lock, no new lock order: the fill waits for the sale, then sees the halt."""
    _, sessions = risk_db
    case, _, feed = await approved_case(sessions, now, trace, key="queued")
    other, position = await held_elsewhere(sessions, now, trace)
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())
    fill = build_fill_service(sessions, now, feed=feed)
    exit_service = build_exit_service(sessions, now, feed=other)

    async with sessions.begin() as session:
        # Hold the lock the sale and the fill both take, start both, release.
        await session.scalar(select(AccountRow).where(AccountRow.id == 1).with_for_update())
        sale = asyncio.create_task(exit_service.execute_position_exit(position.id, request_key="q"))
        await asyncio.sleep(0.3)
        bought = asyncio.create_task(fill.execute_case_fill(case.id, request_key="queued-req"))
        await asyncio.sleep(0.3)

    sold, refused = await asyncio.wait_for(asyncio.gather(sale, bought), timeout=60)
    assert sold.kind == "paper_exit_recorded", getattr(sold, "reason", None)
    assert refused.kind == "execution_refused"
    assert refused.reason.value in ("ENTRY_HALTED", "RISK_RECHECK_REFUSED")
    assert await rows(sessions, TradeCaseExecutionRow) == 1
    assert (await account_of(sessions)).entry_halt_auto is True
