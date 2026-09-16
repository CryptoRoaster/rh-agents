"""What a second cycle leaves behind, and what it must not disturb.

The guarantees Phase 2M-E and 2M-F established are re-proved inside cycle two,
because a reused position row is exactly where they would quietly stop holding.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update

from src.core.models import RiskContext
from src.data.tables import AccountRow, PositionRow, TradeCaseExitRow
from src.ledger.portfolio import replay_portfolio_basis
from src.orchestration.paperexit.models import ExitRefusal
from tests.reentry.conftest import (
    build_exit_service,
    build_reentry_service,
    closed_cycle,
    market_feed,
    money,
    position_of,
    read_account,
)
from tests.reentry.test_integration import second_cycle


async def stored_exit(sessions, cycle_id):
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseExitRow).where(TradeCaseExitRow.cycle_id == cycle_id)
        )


async def test_the_second_cycle_s_audit_basis_still_recomputes(risk_db, now, trace):
    """The valuation record of cycle two reconstructs what SENTINEL judged."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, first_exit = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="audit"
    )
    _, _, position = await second_cycle(sessions, now, uuid4(), opened.trade_case_id, feed)
    sale = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="audit-exit"
    )
    assert sale.kind == "paper_exit_recorded", getattr(sale, "detail", None)

    row = await stored_exit(sessions, opened.cycle_id)
    basis = row.basis["portfolio"]
    judged = RiskContext.model_validate(row.basis["risk_context"])
    assert basis["cycle_id"] == str(opened.cycle_id)
    assert replay_portfolio_basis(basis).context == judged

    # And it stays exact once the account and the holding have moved on.
    async with sessions.begin() as session:
        await session.execute(
            update(AccountRow).where(AccountRow.id == 1).values(cash_usd=Decimal("4"))
        )
        await session.execute(update(PositionRow).values(quantity=Decimal("13")))
    assert replay_portfolio_basis(basis).context == judged


async def test_the_second_cycle_books_its_own_result(risk_db, now, trace):
    """Cash, fees and the day's loss follow cycle two, not cycle one."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, first_exit = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="books"
    )
    before = await read_account(sessions)
    _, _, position = await second_cycle(sessions, now, uuid4(), opened.trade_case_id, feed)
    after_entry = await read_account(sessions)
    assert after_entry.cash_usd < before.cash_usd

    sale = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="books-exit"
    )

    assert sale.kind == "paper_exit_recorded"
    row = await stored_exit(sessions, opened.cycle_id)
    assert money(row.realized_pnl_usd) == money(sale.realized_pnl_usd)
    after = await read_account(sessions)
    assert money(after.realized_loss_today_usd) == money(
        max(Decimal("0"), -first_exit.realized_pnl_usd) + max(Decimal("0"), -sale.realized_pnl_usd)
    )
    closed = await position_of(sessions)
    assert closed.quantity == Decimal("0")
    assert closed.cost_basis_usd == Decimal("0")


async def test_a_holding_whose_cycle_names_another_market_is_not_sold(risk_db, now, trace):
    """The holding and the cycle it names disagree; a sale is not the arbiter."""
    from tests.paperexit.conftest import market_for
    from tests.riskrequest.conftest import ready_case

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, first_exit = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="astray"
    )
    _, _, position = await second_cycle(sessions, now, uuid4(), opened.trade_case_id, feed)

    service = build_exit_service(sessions, now, feed=feed)
    elsewhere = market_for(token="c7" * 20, pool="d8" * 20)
    stranger = await ready_case(service.cases, now, uuid4(), key="astray", identity=elsewhere)
    from src.data.tables import TradeCycleRow
    from src.orchestration.cycles import cycle_of

    async with sessions.begin() as session:
        session.add(
            TradeCycleRow(
                cycle_id=cycle_of(stranger.id),
                trade_case_id=stranger.id,
                asset_id=elsewhere.base_asset_id,
                market_pair_id=elsewhere.pair_id,
                sequence=1,
                predecessor_exit_id=None,
                request_key=None,
                opened_at=now,
                correlation_id=trace,
            )
        )
        await session.execute(
            update(PositionRow)
            .where(PositionRow.id == position.id)
            .values(cycle_id=cycle_of(stranger.id))
        )

    result = await service.execute_position_exit(position.id, request_key="astray-exit")

    assert result.reason is ExitRefusal.POSITION_MARKET_MISMATCH
    assert await stored_exit(sessions, opened.cycle_id) is None


async def test_a_re_entry_moves_no_money(risk_db, now, trace):
    """It opens a case. Nothing is bought, sold, reserved or booked."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, first_exit = await closed_cycle(sessions, now, trace, feed=feed)
    before = await read_account(sessions)
    held = await position_of(sessions)

    opened = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="quiet"
    )

    assert opened.kind == "reentry_opened"
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd, after.realized_loss_today_usd) == (
        before.cash_usd,
        before.fees_paid_usd,
        before.realized_loss_today_usd,
    )
    still = await position_of(sessions)
    assert (still.quantity, still.cost_basis_usd) == (held.quantity, held.cost_basis_usd)
