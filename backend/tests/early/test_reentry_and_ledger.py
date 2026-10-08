"""An early cycle is never re-entered, and its realised loss is what the cap reads."""

from decimal import Decimal

from sqlalchemy import update

from src.core.models import RiskOutcome
from src.data.tables import TradeCaseRow
from src.orchestration.reentry.models import ReentryRefusal
from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1, early_ledger
from tests.casefill.conftest import build_fill_service
from tests.early.test_sentinel import _service, early_ready
from tests.paperexit.conftest import build_exit_service, market_feed, position_of
from tests.reentry.conftest import build_reentry_service, closed_cycle


async def test_a_closed_early_cycle_is_not_re_entered(risk_db, now, trace):
    _, sessions = risk_db
    case, _, _, sale = await closed_cycle(sessions, now, trace)
    # The same closed cycle, but the case that opened it was an early one.
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseRow)
            .where(TradeCaseRow.id == case.id)
            .values(strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1)
        )

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="again"
    )

    assert result.kind == "reentry_refused"
    assert result.reason is ReentryRefusal.STRATEGY_REENTRY_NOT_PERMITTED


async def test_a_closed_normal_cycle_is_still_re_entered(risk_db, now, trace):
    _, sessions = risk_db
    _, _, _, sale = await closed_cycle(sessions, now, trace)
    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="again"
    )
    assert result.kind == "reentry_opened", getattr(result, "reason", None)


async def test_an_early_loss_is_counted_once_it_is_realised(risk_db, now, trace):
    _, sessions = risk_db
    service = _service(sessions, now, liquidity="10000")
    trade_case = await early_ready(service, sessions, now, trace)
    approval = await service.request_risk_evaluation(trade_case.id, request_key="early-loss")
    assert approval.outcome is RiskOutcome.APPROVE
    fill = await build_fill_service(sessions, now, feed=service.markets).execute_case_fill(
        trade_case.id, request_key="early-loss"
    )
    assert fill.kind == "paper_fill_recorded", getattr(fill, "detail", None)

    position = await position_of(sessions)
    lower = market_feed(now, price=Decimal("0.5"))
    sale = await build_exit_service(sessions, now, feed=lower).execute_position_exit(
        position.id, request_key="early-loss-exit"
    )
    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)

    async with sessions() as session:
        ledger = await early_ledger(session)
    book = ledger.at(now)
    assert book.open_positions == 0
    assert book.exposure_usd == 0
    # Roughly six dollars lost on a ten-dollar entry at half the price.
    assert Decimal(5) < book.realized_loss_today_usd < Decimal(7)
