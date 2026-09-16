"""Where migration 0011 can be undone, and where it refuses.

A lossless downgrade after re-entry is not offered. Two cycles share one reused
position row, so the uniqueness the older schema asserts is no longer true of
the data; and the predecessor and idempotency links live only in `trade_cycles`,
so dropping it would lose which cycle followed which exit. Deleting, merging or
renumbering exits to make the old shape fit would destroy booked history.

So the boundary is drawn explicitly: with no successor cycles the downgrade is
supported and works on real first-cycle data; with one — opened or completed —
it refuses before touching anything.

PostgreSQL only. There is no "before" schema in the light suite to migrate.
"""

import io
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from src.data.tables import PositionRow, TradeCaseExitRow, TradeCycleRow
from tests.reentry.conftest import (
    build_exit_service,
    build_reentry_service,
    closed_cycle,
    market_feed,
    position_of,
)
from tests.reentry.test_integration import second_cycle
from tests.reentry.test_migration import load

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

MODULE = "0011_trade_cycles"


async def stamp(engine, revision="0011"):
    """The alembic version a real deployment carries, so its survival is checkable."""
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
        )
        await connection.execute(text("DELETE FROM alembic_version"))
        await connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})


async def version(engine):
    async with engine.connect() as connection:
        return (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()


async def run(engine, direction):
    """Apply 0011's upgrade or downgrade against this schema."""
    module = load(MODULE)
    step = getattr(module, direction)

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            step()

    async with engine.begin() as connection:
        await connection.run_sync(migrate)


async def has_table(engine, name):
    async with engine.connect() as connection:
        return bool(
            (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables"
                        " WHERE table_name = :name AND table_schema = current_schema()"
                    ),
                    {"name": name},
                )
            ).scalar_one()
        )


async def has_column(engine, table, column):
    async with engine.connect() as connection:
        return bool(
            (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns"
                        " WHERE table_name = :table AND column_name = :column"
                        " AND table_schema = current_schema()"
                    ),
                    {"table": table, "column": column},
                )
            ).scalar_one()
        )


# ------------------------------------------------------- the supported path


async def test_first_cycle_data_downgrades_and_upgrades_again(risk_db, now, trace):
    """No successors, so the older shape is still true of the data."""
    engine, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    await stamp(engine)

    await run(engine, "downgrade")

    assert not await has_table(engine, "trade_cycles")
    assert not await has_column(engine, "positions", "cycle_id")
    assert not await has_column(engine, "trade_case_exits", "cycle_id")
    # The booked history is untouched by the shape change. Read as rows rather
    # than through the ORM: the mapped class describes the schema at head, and
    # the point of this window is that the schema is not at head.
    async with engine.connect() as connection:
        rows = (
            await connection.execute(text("SELECT exit_id, position_id FROM trade_case_exits"))
        ).all()
    assert [item[0] for item in rows] == [sale.exit_id]
    assert rows[0][1] == position.id

    await run(engine, "upgrade")

    async with sessions() as session:
        cycle = await session.scalar(select(TradeCycleRow))
        again = await session.scalar(select(TradeCaseExitRow))
        holding = await session.scalar(select(PositionRow))
    assert cycle.sequence == 1
    assert cycle.predecessor_exit_id is None
    assert again.cycle_id == cycle.cycle_id
    assert holding.cycle_id == cycle.cycle_id


# ------------------------------------------------------- the refusals


async def test_an_opened_successor_refuses_the_downgrade(risk_db, now, trace):
    """Opened is enough: the link it records exists nowhere else."""
    engine, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="unfilled"
    )
    assert opened.kind == "reentry_opened"
    await stamp(engine)

    with pytest.raises(RuntimeError, match="not supported once an explicit re-entry"):
        await run(engine, "downgrade")

    await assert_intact(engine, sessions, successors=1, exits=1)
    async with sessions() as session:
        successor = await session.scalar(select(TradeCycleRow).where(TradeCycleRow.sequence == 2))
    assert successor.predecessor_exit_id == sale.exit_id
    assert successor.request_key == "unfilled"


async def test_two_completed_cycles_refuse_the_downgrade(risk_db, now, trace):
    """The reused position row makes one-exit-per-position untrue of the data."""
    engine, sessions = risk_db
    feed = market_feed(now)
    _, _, position, first = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        first.exit_id, request_key="both"
    )
    _, _, again = await second_cycle(sessions, now, uuid4(), opened.trade_case_id, feed)
    second = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        again.id, request_key="both-exit"
    )
    assert second.kind == "paper_exit_recorded", getattr(second, "detail", None)
    await stamp(engine)

    with pytest.raises(RuntimeError, match="not supported once an explicit re-entry"):
        await run(engine, "downgrade")

    await assert_intact(engine, sessions, successors=1, exits=2)
    async with sessions() as session:
        rows = (await session.scalars(select(TradeCaseExitRow))).all()
    # Nothing deleted, merged or renumbered to make the old shape fit.
    assert {item.position_id for item in rows} == {position.id}
    assert len({item.cycle_id for item in rows}) == 2
    assert {item.exit_id for item in rows} == {first.exit_id, second.exit_id}
    assert (await position_of(sessions)).id == position.id


async def assert_intact(engine, sessions, *, successors, exits):
    """Schema, version, cycle links and booked results all survive a refusal."""
    assert await has_table(engine, "trade_cycles")
    assert await has_column(engine, "positions", "cycle_id")
    assert await has_column(engine, "trade_case_exits", "cycle_id")
    assert await version(engine) == "0011"
    async with sessions() as session:
        made = await session.scalar(
            select(func.count())
            .select_from(TradeCycleRow)
            .where(TradeCycleRow.predecessor_exit_id.is_not(None))
        )
        sold = await session.scalar(select(func.count()).select_from(TradeCaseExitRow))
        realised = (await session.scalars(select(TradeCaseExitRow.realized_pnl_usd))).all()
    assert made == successors
    assert sold == exits
    assert all(isinstance(item, Decimal) for item in realised)


# ------------------------------------------------------- generated SQL


def test_the_offline_script_carries_the_same_guard():
    """A generated downgrade must not be a way around the check.

    The script cannot query anything while it is being written, so it carries
    the condition itself and fails in the database that runs it — before the
    first DROP in the file.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        load(MODULE).downgrade()
    rendered = buffer.getvalue()

    assert "RAISE EXCEPTION" in rendered
    assert "not supported once an explicit re-entry" in rendered
    assert rendered.index("RAISE EXCEPTION") < rendered.index("DROP")


def test_an_offline_script_is_refused_for_a_dialect_that_cannot_guard():
    """Rather than emit one that silently drops the table."""
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="sqlite", opts={"as_sql": True, "output_buffer": buffer}
    )
    with pytest.raises(RuntimeError, match="only be generated for PostgreSQL"):
        with Operations.context(context):
            load(MODULE).downgrade()
    assert "DROP" not in buffer.getvalue()
