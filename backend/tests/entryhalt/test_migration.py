"""Migration 0023 on PostgreSQL: the halt columns, the audit, and its guards.

What is proven here is what the database itself enforces, whoever writes:

- an existing pause survives the upgrade and the downgrade untouched;
- a halt field cannot change in a transaction that does not also write the
  event describing that change — not in an earlier one, not without a reason;
- the audit cannot be updated, deleted or truncated;
- the downgrade refuses while a halt is in force or any event exists.
"""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.reentry.test_migration import load
from tests.scout.test_outcome_percent_migration import THROUGH_0021

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0022 = (*THROUGH_0021, "0022_stream_outcome_percent_type")
MODULE = "0023_persistent_entry_halt"
EVENT = (
    "INSERT INTO paper_control_events (event_id, occurred_at, control, action, reason_code,"
    " actor_kind, halted_before, halted_after)"
    " VALUES (:id, now(), :control, :action, :reason, 'OPERATOR', :before, :after)"
)
SET_MANUAL = (
    "UPDATE paper_accounts SET entry_halt_manual = true,"
    " entry_halt_manual_reason = :reason, entry_halt_manual_at = now() WHERE id = 1"
)


@pytest.fixture
async def at_0022():
    url = os.environ["TEST_DATABASE_URL"]
    name = "entry_halt_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0022]

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            for item in modules:
                item.upgrade()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(migrate)
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        await admin.dispose()


async def apply(engine, direction="upgrade"):
    module = load(MODULE)

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            getattr(module, direction)()

    async with engine.begin() as connection:
        await connection.run_sync(migrate)


async def scalar(engine, sql, **values):
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), values)).scalar_one()


async def event(connection, control, action, reason, before, after):
    await connection.execute(
        text(EVENT),
        {
            "id": uuid4(),
            "control": control,
            "action": action,
            "reason": reason,
            "before": before,
            "after": after,
        },
    )


def test_0023_follows_0022_and_is_followed_by_0024():
    # The chain's head is asserted by the newest migration's own test.
    assert load(MODULE).down_revision == "0022"
    assert load("0024_paper_job_runs").down_revision == "0023"


async def test_an_existing_pause_survives_the_upgrade_and_both_halts_arrive_cleared(at_0022):
    async with at_0022.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))

    await apply(at_0022)

    assert await scalar(at_0022, "SELECT paused FROM paper_accounts") is True
    assert (
        await scalar(at_0022, "SELECT entry_halt_auto OR entry_halt_manual FROM paper_accounts")
        is False
    )
    assert await scalar(at_0022, "SELECT COUNT(*) FROM paper_control_events") == 0


async def test_a_halt_change_without_its_event_is_refused_at_commit(at_0022):
    await apply(at_0022)

    with pytest.raises(DBAPIError, match="without an audit event"):
        async with at_0022.begin() as connection:
            await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})

    assert await scalar(at_0022, "SELECT entry_halt_manual FROM paper_accounts") is False


async def test_a_halt_change_with_its_event_commits_in_either_order(at_0022):
    await apply(at_0022)

    async with at_0022.begin() as connection:
        await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})
        await event(connection, "ENTRY_HALT_MANUAL", "SET", "OPERATOR_REVIEW", False, True)

    assert await scalar(at_0022, "SELECT entry_halt_manual FROM paper_accounts") is True


async def test_an_event_from_another_transaction_or_reason_accounts_for_nothing(at_0022):
    await apply(at_0022)
    async with at_0022.begin() as connection:
        await event(connection, "ENTRY_HALT_MANUAL", "SET", "OPERATOR_REVIEW", False, True)

    with pytest.raises(DBAPIError, match="without an audit event"):
        async with at_0022.begin() as connection:
            await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})
    with pytest.raises(DBAPIError, match="without an audit event"):
        async with at_0022.begin() as connection:
            await event(connection, "ENTRY_HALT_MANUAL", "SET", "SOMETHING_ELSE", False, True)
            await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})
    with pytest.raises(DBAPIError, match="without an audit event"):
        async with at_0022.begin() as connection:
            # An event for the other halt does not account for this one.
            await event(connection, "ENTRY_HALT_AUTO", "SET", "OPERATOR_REVIEW", False, True)
            await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})


async def test_a_change_of_anything_but_a_halt_needs_no_event(at_0022):
    await apply(at_0022)

    async with at_0022.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET cash_usd = cash_usd - 1"))

    assert await scalar(at_0022, "SELECT cash_usd FROM paper_accounts") == 9999


async def test_a_halt_without_its_reason_is_refused_by_its_constraint(at_0022):
    await apply(at_0022)

    with pytest.raises(DBAPIError, match="entry_halt_manual_consistent"):
        async with at_0022.begin() as connection:
            await connection.execute(text("UPDATE paper_accounts SET entry_halt_manual = true"))
    with pytest.raises(DBAPIError, match="entry_halt_reason_format"):
        async with at_0022.begin() as connection:
            await connection.execute(text(SET_MANUAL), {"reason": "free text"})


async def test_the_audit_is_append_only(at_0022):
    await apply(at_0022)
    async with at_0022.begin() as connection:
        await event(connection, "ENTRY_HALT_MANUAL", "SET", "OPERATOR_REVIEW", False, True)
        await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})

    for statement in (
        "UPDATE paper_control_events SET reason_code = 'REWRITTEN'",
        "DELETE FROM paper_control_events",
        "TRUNCATE paper_control_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with at_0022.begin() as connection:
                await connection.execute(text(statement))
    assert await scalar(at_0022, "SELECT COUNT(*) FROM paper_control_events") == 1


async def test_the_downgrade_refuses_while_a_halt_is_in_force(at_0022):
    await apply(at_0022)
    async with at_0022.begin() as connection:
        await event(connection, "ENTRY_HALT_MANUAL", "SET", "OPERATOR_REVIEW", False, True)
        await connection.execute(text(SET_MANUAL), {"reason": "OPERATOR_REVIEW"})

    with pytest.raises(RuntimeError, match="Downgrade of 0023 refused"):
        await apply(at_0022, "downgrade")
    assert await scalar(at_0022, "SELECT entry_halt_manual FROM paper_accounts") is True


async def test_the_downgrade_refuses_while_any_audit_history_exists(at_0022):
    await apply(at_0022)
    async with at_0022.begin() as connection:
        # A refused request: it changed no halt, and it is still history.
        await connection.execute(
            text(
                "INSERT INTO paper_control_events (event_id, occurred_at, control, action,"
                " reason_code, refusal_code, actor_kind, halted_before, halted_after)"
                " VALUES (:id, now(), 'ENTRY_HALT_MANUAL', 'REFUSED', 'REVIEW_DONE',"
                " 'NOT_HALTED', 'OPERATOR', false, false)"
            ),
            {"id": uuid4()},
        )

    with pytest.raises(RuntimeError, match="Downgrade of 0023 refused"):
        await apply(at_0022, "downgrade")
    assert await scalar(at_0022, "SELECT COUNT(*) FROM paper_control_events") == 1


async def test_a_clean_downgrade_keeps_the_pause_and_upgrades_again(at_0022):
    async with at_0022.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))
    await apply(at_0022)

    await apply(at_0022, "downgrade")

    assert await scalar(at_0022, "SELECT paused FROM paper_accounts") is True
    assert (
        await scalar(
            at_0022,
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'paper_accounts'"
            " AND column_name LIKE 'entry_halt%' AND table_schema = current_schema()",
        )
        == 0
    )
    await apply(at_0022)
    assert await scalar(at_0022, "SELECT paused FROM paper_accounts") is True
