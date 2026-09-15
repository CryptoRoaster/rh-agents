"""What an exit leaves behind: the time boundary, the record and the bar.

Every time case runs on a controlled clock that advances when work happens
rather than when it is read. No sleep is used anywhere.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from src.core.models import RiskContext, RiskLimits
from src.data.tables import AccountRow, ExecutionRow, PositionRow, TradeCaseExitRow
from src.ledger.portfolio import replay_portfolio_basis
from src.orchestration.paperexit.models import ExitRefusal
from src.orchestration.workflow.models import TradeCaseStatus
from tests.casefill.test_hardening import WaitingClock, slow_persistence
from tests.paperexit.conftest import (
    FRESH,
    IDENTITY,
    build_exit_service,
    entered,
    exits,
    market_feed,
    money,
    position_of,
    read_account,
    set_account,
)

LOSS = Decimal("50")


async def stored(sessions, position_id):
    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExitRow).where(TradeCaseExitRow.position_id == position_id)
        )
    return row


# ------------------------------------------------------- the time boundary


@pytest.mark.parametrize(
    ("limits", "age", "expected"),
    [
        (None, FRESH, "APPROVAL_EXPIRED"),
        # A window wide enough that only a source can run out, so the second
        # case proves the source re-check rather than the approval one. The
        # holding is priced from this same reading, so its mark is what runs
        # out first — which is the mark freshness check, at the fill boundary.
        (
            RiskLimits(approval_ttl_seconds=600),
            timedelta(seconds=25),
            "POSITION_VALUATION_STALE",
        ),
    ],
)
async def test_an_expiry_during_persistence_rolls_the_whole_sale_back(
    risk_db, now, trace, monkeypatch, limits, age, expected
):
    """Persisting the decision, the intent and the market takes real time.

    The clock is read again at the execution boundary, truthfully, and whatever
    ran out in that span stops the sale — the decision's own window, or any
    source it rested on. Nothing is backdated, no deadline is extended, and no
    risk rejection is written in its place.
    """
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace, feed=market_feed(now))
    feed = market_feed(now, age=age)
    before = await read_account(sessions)

    clock = WaitingClock(now)
    service = build_exit_service(sessions, now, feed=feed, limits=limits, clock=clock)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    slow_persistence(monkeypatch, clock, timedelta(seconds=3))

    result = await service.execute_position_exit(position.id, request_key="exit-late")

    assert result.kind == "exit_refused"
    assert result.reason is ExitRefusal.EXECUTION_WINDOW_EXPIRED
    assert result.detail == expected
    assert await exits(sessions) == []
    assert (await position_of(sessions)).quantity == position.quantity
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)
    async with sessions() as session:
        # The entry's fill, and nothing at all from this attempt.
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1


# ------------------------------------------------------- the audit record


async def test_the_stored_basis_recomputes_what_sentinel_judged(risk_db, now, trace):
    """History is recomputed from the record, never from what is current."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-audit"
    )
    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)

    row = await stored(sessions, position.id)
    basis = row.basis["portfolio"]
    judged = RiskContext.model_validate(row.basis["risk_context"])
    first = replay_portfolio_basis(basis).context
    assert first == judged
    # The holding the sale was judged on is in the record, not in the table.
    assert basis["holdings"][0]["asset_id"] == position.asset_id
    assert Decimal(basis["holdings"][0]["quantity"]) == position.quantity

    # The world moves on: the account and the positions both change.
    async with sessions.begin() as session:
        await session.execute(
            update(AccountRow).where(AccountRow.id == 1).values(cash_usd=Decimal("7"))
        )
        await session.execute(update(PositionRow).values(quantity=Decimal("99")))

    assert replay_portfolio_basis(basis).context == first == judged


async def test_the_record_names_the_entry_the_market_and_its_own_decision(risk_db, now, trace):
    """One read answers what was closed, under what, and on what basis."""
    _, sessions = risk_db
    feed = market_feed(now)
    case, entry, position = await entered(sessions, now, trace, feed=feed)

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-chain"
    )

    row = await stored(sessions, position.id)
    assert row.trade_case_id == case.id
    assert row.case_execution_id == entry.case_execution_id
    assert row.market_pair_id == IDENTITY.pair_id
    assert row.basis["entry_execution_id"] == str(entry.execution_id)
    # The exit's own decision, never the entry's approval.
    assert row.risk_decision_id != entry.recheck_decision_id
    assert row.basis["decision"]["id"] == str(result.risk_decision_id)
    assert row.basis["intent"]["side"] == "SELL"
    assert row.basis["trade"]["realized_pnl_usd"] == str(result.realized_pnl_usd)


# ------------------------------------------------------- the UTC day


def midnight(now):
    """Two seconds before the UTC day turns, and four seconds after."""
    before = now.replace(hour=23, minute=59, second=58, microsecond=0)
    return before, before + timedelta(seconds=4)


async def test_a_loss_realised_after_midnight_lands_on_the_new_day(risk_db, now, trace):
    """The day is rolled inside the evaluation, and the loss follows it."""
    _, sessions = risk_db
    before, after = midnight(now)
    entry_feed = market_feed(before)
    _, _, position = await entered(sessions, before, trace, feed=entry_feed)
    await set_account(sessions, realized_loss_today_usd=LOSS, loss_day=before.date())
    # A sale at a quarter of the entry price, so the exit realises a real loss.
    feed = market_feed(after, price=Decimal("0.3"))

    result = await build_exit_service(sessions, after, feed=feed).execute_position_exit(
        position.id, request_key="exit-midnight"
    )

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    assert result.realized_pnl_usd < 0
    account = await read_account(sessions)
    # Yesterday's 50 is gone; today carries exactly what today realised.
    assert account.loss_day == after.date()
    assert money(account.realized_loss_today_usd) == money(-result.realized_pnl_usd)

    row = await stored(sessions, position.id)
    basis = row.basis["portfolio"]
    assert Decimal(basis["realized_loss_today_usd"]) == Decimal("0")
    assert replay_portfolio_basis(basis).context == RiskContext.model_validate(
        row.basis["risk_context"]
    )


async def test_without_a_day_change_the_day_s_loss_accumulates(risk_db, now, trace):
    """The control: nothing turned, so the new loss adds to the old."""
    _, sessions = risk_db
    before, _ = midnight(now)
    _, _, position = await entered(sessions, before, trace, feed=market_feed(before))
    await set_account(sessions, realized_loss_today_usd=LOSS, loss_day=before.date())
    feed = market_feed(before, price=Decimal("0.3"))

    result = await build_exit_service(sessions, before, feed=feed).execute_position_exit(
        position.id, request_key="exit-sameday"
    )

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    account = await read_account(sessions)
    assert account.loss_day == before.date()
    assert money(account.realized_loss_today_usd) == money(LOSS - result.realized_pnl_usd)
    row = await stored(sessions, position.id)
    assert Decimal(row.basis["portfolio"]["realized_loss_today_usd"]) == LOSS


# ------------------------------------------------------- what an exit is not


async def test_a_closed_position_is_not_a_re_entry_permit(risk_db, now, trace):
    """The case stays EXECUTED and its market stays barred. Deliberately.

    A sold position is not a licence to buy again: whether a later entry is a
    new trade or the same one repeated is a contract that does not exist, and an
    exit is not the place to invent one.
    """
    from tests.casefill.conftest import candidate_for
    from tests.commander.conftest import intake_service
    from tests.paperexit.conftest import recorded_snapshot

    _, sessions = risk_db
    feed = market_feed(now)
    case, _, position = await entered(sessions, now, trace, feed=feed)
    service = build_exit_service(sessions, now, feed=feed)
    assert (
        await service.execute_position_exit(position.id, request_key="exit-bar")
    ).kind == "paper_exit_recorded"

    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.EXECUTED
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

    # And the closed holding cannot be sold again under a fresh key.
    again = await service.execute_position_exit(position.id, request_key="exit-bar-2")
    assert again.reason is ExitRefusal.POSITION_ALREADY_CLOSED


async def test_an_unvaluable_other_holding_stops_the_exit(risk_db, now, trace):
    """A portfolio this system cannot value is not one SENTINEL may judge."""
    from src.core.models import Position
    from src.data.repository import save_position
    from tests.paperexit.conftest import market_for

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    elsewhere = market_for(token="c7" * 20, pool="d8" * 20)
    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id=elsewhere.base_asset_id,
                market_pair_id=elsewhere.pair_id,
                market_chain=elsewhere.chain,
                market_network=elsewhere.network,
                market_provider=elsewhere.provider,
                quantity=Decimal("3"),
                cost_basis_usd=Decimal("30"),
                created_at=now,
                updated_at=now,
            ),
        )

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-unvaluable"
    )

    assert result.reason is ExitRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert result.detail == "MARKET_NOT_RECORDED"
    assert await exits(sessions) == []


async def test_a_strict_limit_refuses_the_sale_without_special_rights(risk_db, now, trace):
    """No emergency exit: what SENTINEL refuses stays refused."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    strict = RiskLimits(max_slippage_bps=Decimal("0"))

    result = await build_exit_service(
        sessions, now, feed=feed, limits=strict
    ).execute_position_exit(position.id, request_key="exit-strict")

    assert result.reason is ExitRefusal.EXIT_RISK_REFUSED
    assert "SLIPPAGE_LIMIT" in result.reason_codes
    assert (await position_of(sessions)).quantity == position.quantity
    assert await exits(sessions) == []
