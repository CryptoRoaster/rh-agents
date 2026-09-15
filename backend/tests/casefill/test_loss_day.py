"""The stored valuation basis across a UTC day boundary.

The day's realised loss is reset when the UTC day turns, inside the evaluation.
A basis assembled from account values read *before* that reset records a day that
had already ended by the time SENTINEL was asked anything.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update

from src.core.models import RiskContext
from src.data.tables import AccountRow, PositionRow, TradeCaseExecutionRow
from src.ledger.portfolio import replay_portfolio_basis
from tests.casefill.conftest import approved_case, build_fill_service
from tests.casefill.test_marks_hardening import ELSEWHERE, hold, two_pools
from tests.riskrequest.conftest import read_account, set_account

LOSS = Decimal("50")


def midnight(now):
    """Two seconds before the UTC day turns, and four seconds later.

    Inside the five-second approval window, so the fill is authorised by the
    approval the request produced — the day boundary is the only thing that
    moved.
    """
    before = now.replace(hour=23, minute=59, second=58, microsecond=0)
    return before, before + timedelta(seconds=4)


async def stored_basis(sessions, case_id):
    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == case_id)
        )
    return row.basis["portfolio"], RiskContext.model_validate(row.basis["risk_context"])


async def move_everything(sessions):
    """Change the account and the positions the recomputation must not read."""
    async with sessions.begin() as session:
        await session.execute(
            update(AccountRow)
            .where(AccountRow.id == 1)
            .values(cash_usd=Decimal("1"), realized_loss_today_usd=Decimal("777"))
        )
        await session.execute(update(PositionRow).values(quantity=Decimal("99")))


async def fill_across(sessions, now, trace, *, turn_of_day: bool):
    """One approved case, filled either side of the UTC midnight it spans."""
    before, after = midnight(now)
    feed, _, _ = two_pools(before)
    await hold(
        sessions, before, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id, quantity="4"
    )
    # A real loss already recorded against the day the request is made on.
    await set_account(sessions, realized_loss_today_usd=LOSS, loss_day=before.date())
    case, approval, _ = await approved_case(sessions, before, trace, key="day", feed=feed)
    assert approval.kind == "risk_request_evaluated", getattr(approval, "reason", None)

    at = after if turn_of_day else before + timedelta(seconds=1)
    service = build_fill_service(sessions, at, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="day-req")
    assert result.kind == "paper_fill_recorded", getattr(result, "detail", None)
    return case


async def test_the_new_day_s_loss_is_what_sentinel_judged_and_what_is_stored(risk_db, now, trace):
    """The reset happens inside the evaluation, so the basis must follow it.

    Reproduction: the service read the account's realised loss before
    `execute_in_session`, where `roll_loss_day` then zeroed it for the very
    evaluation the basis claims to record. The stored figures were the new day's
    and the stored inputs the old day's, so the two disagreed by exactly the
    loss that had been rolled off.
    """
    _, sessions = risk_db
    case = await fill_across(sessions, now, trace, turn_of_day=True)

    basis, judged = await stored_basis(sessions, case.id)

    # The holding is 64 under water (4 x 9 against a 100 cost basis) and the day
    # turned, so yesterday's 50 is not part of today's loss.
    assert judged.daily_loss_usd == Decimal("64.000000000000000000")
    assert Decimal(basis["realized_loss_today_usd"]) == Decimal("0")
    assert (await read_account(sessions)).loss_day == midnight(now)[1].date()

    assert replay_portfolio_basis(basis).context == judged


async def test_that_recomputation_survives_the_account_and_positions_moving(risk_db, now, trace):
    """History is recomputed from the record, never from what is current now."""
    _, sessions = risk_db
    case = await fill_across(sessions, now, trace, turn_of_day=True)
    basis, judged = await stored_basis(sessions, case.id)
    first = replay_portfolio_basis(basis).context

    await move_everything(sessions)

    assert replay_portfolio_basis(basis).context == first == judged


async def test_without_a_day_change_the_day_s_loss_is_kept(risk_db, now, trace):
    """The control: nothing turned, so nothing is rolled off."""
    _, sessions = risk_db
    case = await fill_across(sessions, now, trace, turn_of_day=False)

    basis, judged = await stored_basis(sessions, case.id)

    assert judged.daily_loss_usd == Decimal("114.000000000000000000")  # 50 + 64
    assert Decimal(basis["realized_loss_today_usd"]) == LOSS
    assert replay_portfolio_basis(basis).context == judged


async def test_the_request_s_own_basis_agrees_with_what_it_judged(risk_db, now, trace):
    """The approval records the day it was made on, unrolled and unchanged."""
    from src.data.tables import TradeCaseRiskRequestRow

    _, sessions = risk_db
    before, _ = midnight(now)
    feed, _, _ = two_pools(before)
    await hold(
        sessions, before, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id, quantity="4"
    )
    await set_account(sessions, realized_loss_today_usd=LOSS, loss_day=before.date())
    case, _, _ = await approved_case(sessions, before, uuid4(), key="day-req-only", feed=feed)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseRiskRequestRow).where(TradeCaseRiskRequestRow.trade_case_id == case.id)
        )
    judged = RiskContext.model_validate(row.basis["risk_context"])
    assert Decimal(row.basis["portfolio"]["realized_loss_today_usd"]) == LOSS
    assert replay_portfolio_basis(row.basis["portfolio"]).context == judged
