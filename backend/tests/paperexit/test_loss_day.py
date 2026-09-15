"""Which UTC day a realised sale result counts against.

`roll_loss_day` runs at the decision instant. Persisting the decision, the
intent and the market then takes real time, and the fill carries the instant it
was actually placed at — which can be the next day. A loss booked against the
day the decision was taken on is a loss recorded against a day that had already
ended by the time it happened.

Controlled clock throughout: it advances when work happens, not when it is read.
No sleep is used anywhere here.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.core.models import RiskContext, RiskLimits
from src.data.tables import AccountRow, ExecutionRow, PositionRow, TradeCaseExitRow
from src.ledger.portfolio import replay_portfolio_basis
from tests.casefill.test_hardening import WaitingClock, slow_persistence
from tests.paperexit.conftest import (
    build_exit_service,
    entered,
    exits,
    market_feed,
    money,
    position_of,
    read_account,
    set_account,
)

YESTERDAY = Decimal("50")
# Wide enough that the approval cannot be what runs out: the day boundary is
# the only thing this file is about.
GENEROUS = RiskLimits(approval_ttl_seconds=600)
# Three persistence writes happen between the decision and the fill, so three
# seconds each carries the clock nine seconds forward.
PER_WRITE = timedelta(seconds=3)


def clocks(now, *, crossing: bool):
    """A decision instant that either does or does not precede UTC midnight.

    At `23:59:55` the nine seconds of persistence land the fill on the next day.
    At `23:59:40` they do not, and everything else about the two runs is equal.
    """
    second = 55 if crossing else 40
    decision = now.replace(hour=23, minute=59, second=second, microsecond=0)
    return decision, decision + 3 * PER_WRITE


async def sold_across(sessions, now, trace, monkeypatch, *, crossing: bool, key="exit-day"):
    """One real entry, then one real exit whose persistence spans the boundary."""
    decision, boundary = clocks(now, crossing=crossing)
    await entered(sessions, decision, trace, feed=market_feed(decision))
    await set_account(sessions, realized_loss_today_usd=YESTERDAY, loss_day=decision.date())
    position = await position_of(sessions)

    # A quarter of the entry price, so the sale realises a real loss.
    feed = market_feed(decision, price=Decimal("0.3"))
    clock = WaitingClock(decision)
    service = build_exit_service(sessions, decision, feed=feed, limits=GENEROUS, clock=clock)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    slow_persistence(monkeypatch, clock, PER_WRITE)

    result = await service.execute_position_exit(position.id, request_key=key)
    return result, position, decision, boundary


# ------------------------------------------------------------------ the day


async def test_a_loss_realised_after_the_boundary_counts_against_the_fill_s_day(
    risk_db, now, trace, monkeypatch
):
    """Reproduction: the day was normalised at the decision, the fill was later.

    The decision is taken at `23:59:55`; three real persistence writes carry the
    clock to `00:00:04`, and the fill records that instant. Before the fix the
    loss was added to the day set at the decision, so yesterday ended holding
    both its own 50 and a loss that happened today.
    """
    result, _, decision, boundary = await sold_across(
        risk_db[1], now, trace, monkeypatch, crossing=True
    )

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    assert result.realized_pnl_usd < 0
    assert result.filled_at == boundary
    assert result.filled_at.date() != decision.date()

    account = await read_account(risk_db[1])
    assert account.loss_day == boundary.date()
    # Exactly the loss that happened today, and nothing carried over from the
    # day that ended while this sale was being written.
    assert money(account.realized_loss_today_usd) == money(-result.realized_pnl_usd)


async def test_a_later_operation_on_the_new_day_does_not_reset_that_loss(
    risk_db, now, trace, monkeypatch
):
    """The day is normalised, not re-zeroed by every booking that follows."""
    _, sessions = risk_db
    result, _, _, boundary = await sold_across(sessions, now, trace, monkeypatch, crossing=True)
    assert result.kind == "paper_exit_recorded"
    monkeypatch.undo()
    carried = (await read_account(sessions)).realized_loss_today_usd

    # A second, entirely separate entry booked on the new day.
    from tests.paperexit.conftest import market_for, recorded_snapshot

    second = market_for(token="c7" * 20, pool="d8" * 20)
    feed = market_feed(boundary)
    feed.replace(
        recorded_snapshot(
            boundary,
            age=timedelta(seconds=5),
            metadata_age=timedelta(seconds=5),
            base_asset_id=second.base_asset_id,
            pair_id=second.pair_id,
            label="second",
        )
    )
    await entered(sessions, boundary, uuid4(), key="next-day", feed=feed, identity=second)

    account = await read_account(sessions)
    assert account.loss_day == boundary.date()
    assert money(account.realized_loss_today_usd) == money(carried)
    assert money(carried) == money(-result.realized_pnl_usd)


async def test_without_a_boundary_the_day_s_loss_accumulates(risk_db, now, trace, monkeypatch):
    """The control: the same nine seconds, no midnight in them."""
    result, _, decision, boundary = await sold_across(
        risk_db[1], now, trace, monkeypatch, crossing=False
    )

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    assert result.filled_at.date() == decision.date() == boundary.date()
    account = await read_account(risk_db[1])
    assert account.loss_day == decision.date()
    assert money(account.realized_loss_today_usd) == money(YESTERDAY - result.realized_pnl_usd)


# ------------------------------------------------------------------ the basis


async def test_the_decision_basis_still_reconstructs_the_context_it_judged(
    risk_db, now, trace, monkeypatch
):
    """Booking state and decision basis are not the same record.

    The sale is booked against the day it happened on; the basis keeps the day
    that was in force when SENTINEL was asked, because that is what SENTINEL was
    shown.
    """
    _, sessions = risk_db
    result, position, decision, _ = await sold_across(
        sessions, now, trace, monkeypatch, crossing=True
    )
    assert result.kind == "paper_exit_recorded"

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExitRow).where(TradeCaseExitRow.position_id == position.id)
        )
    basis = row.basis["portfolio"]
    judged = RiskContext.model_validate(row.basis["risk_context"])

    assert basis["valued_at"] == decision.isoformat()
    assert Decimal(basis["realized_loss_today_usd"]) == YESTERDAY
    assert replay_portfolio_basis(basis).context == judged

    # And it stays exact once the account and the positions have moved on.
    async with sessions.begin() as session:
        await session.execute(
            update(AccountRow)
            .where(AccountRow.id == 1)
            .values(cash_usd=Decimal("3"), realized_loss_today_usd=Decimal("999"))
        )
        await session.execute(update(PositionRow).values(quantity=Decimal("77")))
    assert replay_portfolio_basis(basis).context == judged


# ------------------------------------------------------------------ rollback


async def test_a_failure_before_commit_normalises_no_day_and_books_no_loss(
    risk_db, now, trace, monkeypatch
):
    """All of it or none of it — the day roll included."""
    _, sessions = risk_db
    decision, _ = clocks(now, crossing=True)
    await entered(sessions, decision, trace, feed=market_feed(decision))
    await set_account(sessions, realized_loss_today_usd=YESTERDAY, loss_day=decision.date())
    position = await position_of(sessions)
    before = await read_account(sessions)

    feed = market_feed(decision, price=Decimal("0.3"))
    clock = WaitingClock(decision)
    service = build_exit_service(sessions, decision, feed=feed, limits=GENEROUS, clock=clock)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    slow_persistence(monkeypatch, clock, PER_WRITE)
    original = type(service)._record

    def explode(self, *arguments, **keywords):
        original(self, *arguments, **keywords)
        raise RuntimeError("the transaction dies after everything is staged")

    monkeypatch.setattr(type(service), "_record", explode)
    with pytest.raises(RuntimeError):
        await service.execute_position_exit(position.id, request_key="exit-boom")
    monkeypatch.undo()

    after = await read_account(sessions)
    assert after.loss_day == before.loss_day == decision.date()
    assert money(after.realized_loss_today_usd) == money(YESTERDAY)
    assert money(after.cash_usd) == money(before.cash_usd)
    assert await exits(sessions) == []
    assert (await position_of(sessions)).quantity == position.quantity
    async with sessions() as session:
        # The entry's fill, and nothing from this attempt.
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1
