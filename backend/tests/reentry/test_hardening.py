"""Reproductions for the 2M-G review findings, then the proofs they are closed.

Two defects, each written here before it was fixed:

1. The holding checks were all inside `if holding is not None`, so a missing
   position row skipped every one of them and a successor cycle was opened for a
   predecessor whose holding could not be shown to be closed at all.
2. `0011` could be downgraded once re-entry had been used, and the schema change
   would then fail part-way through instead of refusing up front.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.data.tables import (
    PositionRow,
    TradeCaseEventRow,
    TradeCaseExitRow,
    TradeCaseRow,
    TradeCycleRow,
)
from src.orchestration.reentry.models import ReentryRefusal
from tests.reentry.conftest import (
    build_reentry_service,
    closed_cycle,
    cycles,
    market_feed,
    position_of,
    read_account,
)


async def counts(sessions):
    """Everything a refused re-entry must leave exactly as it found it."""
    async with sessions() as session:
        return (
            await session.scalar(select(func.count()).select_from(TradeCaseRow)),
            await session.scalar(select(func.count()).select_from(TradeCycleRow)),
            await session.scalar(select(func.count()).select_from(TradeCaseEventRow)),
        )


# ============================================================ finding 1


async def test_a_missing_position_row_is_not_an_absent_objection(risk_db, now, trace):
    """Reproduction: the holding checks were skipped when the row was gone.

    A row this system cannot read is not a holding this system has shown to be
    closed. Nothing about the previous cycle can be established from its
    absence, so a successor is refused rather than opened on it.
    """
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    # The exit names a holding nothing can resolve. `trade_case_exits` carries
    # the reference without a foreign key, because the position row outlives the
    # cycle and is reused — so a reference that leads nowhere is reachable, and
    # its absence must be an objection rather than the absence of one.
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseExitRow)
            .where(TradeCaseExitRow.exit_id == sale.exit_id)
            .values(position_id=uuid4())
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="vanished"
    )

    assert result.kind == "reentry_refused"
    assert result.reason is ReentryRefusal.POSITION_NOT_FOUND
    assert await counts(sessions) == before
    assert len(await cycles(sessions)) == 1


async def test_a_holding_of_another_asset_is_refused(risk_db, now, trace):
    """The exit, its entry and the holding must describe one trade."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow)
            .where(PositionRow.id == position.id)
            .values(asset_id="robinhood:mainnet:0xsomethingelse")
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="other-asset"
    )

    assert result.reason is ReentryRefusal.POSITION_CYCLE_MISMATCH
    assert await counts(sessions) == before


@pytest.mark.parametrize(
    "field",
    ["market_pair_id", "market_chain", "market_network", "market_provider"],
)
async def test_a_holding_recorded_in_another_market_is_refused(risk_db, now, trace, field):
    """The whole recorded identity has to agree, not just the pair."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow)
            .where(PositionRow.id == position.id)
            .values(**{field: "somewhere-else"})
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key=f"market-{field}"
    )

    assert result.reason is ReentryRefusal.POSITION_CYCLE_MISMATCH
    assert await counts(sessions) == before


async def test_a_holding_attributed_to_another_cycle_is_refused(risk_db, now, trace):
    """The row must belong to the cycle the exit closed."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow).where(PositionRow.id == position.id).values(cycle_id=None)
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="unowned"
    )

    assert result.reason is ReentryRefusal.POSITION_CYCLE_MISMATCH
    assert await counts(sessions) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", Decimal("2")), ("cost_basis_usd", Decimal("7"))],
)
async def test_both_quantity_and_cost_basis_must_be_nil(risk_db, now, trace, field, value):
    """Either one alone means the cycle still owns something."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow).where(PositionRow.id == position.id).values(**{field: value})
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key=f"open-{field}"
    )

    assert result.reason is ReentryRefusal.POSITION_STILL_OPEN
    assert await counts(sessions) == before


async def test_an_exit_naming_another_holding_is_refused(risk_db, now, trace):
    """The exit's position reference has to be the cycle's own holding."""
    from src.core.models import Position
    from src.data.repository import save_position
    from tests.reentry.conftest import market_for

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
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
                quantity=Decimal("0"),
                cost_basis_usd=Decimal("0"),
                created_at=now,
                updated_at=now,
            ),
        )
    async with sessions() as session:
        stranger = await session.scalar(
            select(PositionRow).where(PositionRow.asset_id == elsewhere.base_asset_id)
        )
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseExitRow)
            .where(TradeCaseExitRow.exit_id == sale.exit_id)
            .values(position_id=stranger.id)
        )
    before = await counts(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="wrong-holding"
    )

    assert result.reason is ReentryRefusal.POSITION_CYCLE_MISMATCH
    assert await counts(sessions) == before


async def test_a_properly_closed_holding_still_opens_a_successor(risk_db, now, trace):
    """The control: nothing above rejects the case these checks exist for."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    before = await read_account(sessions)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="ordinary"
    )

    assert result.kind == "reentry_opened"
    assert result.sequence == 2
    assert len(await cycles(sessions)) == 2
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)


async def test_an_opened_successor_replays_before_the_new_preconditions(risk_db, now, trace):
    """History does not have to satisfy checks it was not opened under.

    The successor already exists; re-reading the holding now would answer a
    question about today rather than returning what was recorded. The replay is
    checked first, and stays first.
    """
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    opened = await service.open_reentry(sale.exit_id, request_key="already")
    assert opened.kind == "reentry_opened"

    # The holding becomes unreachable afterwards: the stored answer is unaffected.
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseExitRow)
            .where(TradeCaseExitRow.exit_id == sale.exit_id)
            .values(position_id=uuid4())
        )

    again = await service.open_reentry(sale.exit_id, request_key="already")

    assert again.kind == "reentry_opened"
    assert again.replayed is True
    assert again.trade_case_id == opened.trade_case_id
    assert again.cycle_id == opened.cycle_id
    assert len(await cycles(sessions)) == 2


async def test_a_second_key_after_a_successor_still_refuses_first(risk_db, now, trace):
    """One successor per exit is decided before any holding is read."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    assert (await service.open_reentry(sale.exit_id, request_key="one")).kind == "reentry_opened"
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseExitRow)
            .where(TradeCaseExitRow.exit_id == sale.exit_id)
            .values(position_id=uuid4())
        )

    result = await service.open_reentry(sale.exit_id, request_key="two")

    assert result.reason is ReentryRefusal.SUCCESSOR_ALREADY_EXISTS
    assert len(await cycles(sessions)) == 2


# ============================================================ finding 2


async def test_two_cycles_share_one_position_row(risk_db, now, trace):
    """Why `0011` cannot simply be undone once re-entry has been used.

    Both exits reference the same reused position row, so the uniqueness the
    downgrade would restore — one exit per position — is no longer true of the
    data. Reproduced with real cycles rather than asserted about the schema.
    """
    from uuid import uuid4 as fresh

    from src.data.tables import TradeCaseExitRow
    from tests.reentry.conftest import build_exit_service
    from tests.reentry.test_integration import second_cycle

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, first = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        first.exit_id, request_key="both"
    )
    _, _, again = await second_cycle(sessions, now, fresh(), opened.trade_case_id, feed)
    second = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        again.id, request_key="both-exit"
    )
    assert second.kind == "paper_exit_recorded", getattr(second, "detail", None)

    async with sessions() as session:
        rows = (await session.scalars(select(TradeCaseExitRow))).all()
    assert len(rows) == 2
    assert {item.position_id for item in rows} == {position.id}
    assert len({item.cycle_id for item in rows}) == 2
    assert (await position_of(sessions)).id == position.id
