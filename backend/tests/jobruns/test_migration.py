"""Migration 0024 on PostgreSQL: the job-run record, and the audited pause lift.

What the database itself enforces, whoever writes:

- an existing pause survives the upgrade and the downgrade untouched;
- a run is `RUNNING` exactly while unfinished, and once finished it is final;
- only a successful exit-job run can be evidence that exits work;
- `paused` cannot go from true to false without a `SYSTEM_PAUSE` CLEAR event of
  the same transaction, and only an operator can write one;
- the downgrade refuses while a `SYSTEM_PAUSE` event exists.
"""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.entryhalt.test_migration import THROUGH_0022
from tests.reentry.test_migration import load

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0023 = (*THROUGH_0022, "0023_persistent_entry_halt")
MODULE = "0024_paper_job_runs"
RUN = (
    "INSERT INTO paper_job_runs (run_id, job, mode, started_at, finished_at, outcome,"
    " exit_ready, exit_ready_reason)"
    " VALUES (:id, :job, :mode, now() - interval '1 minute', :finished, :outcome,"
    " :ready, :reason)"
)
PAUSE_EVENT = (
    "INSERT INTO paper_control_events (event_id, occurred_at, control, action, reason_code,"
    " actor_kind, halted_before, halted_after)"
    " VALUES (:id, now(), 'SYSTEM_PAUSE', 'CLEAR', 'RECOVERY', :actor, true, false)"
)


@pytest.fixture
async def at_0023():
    url = os.environ["TEST_DATABASE_URL"]
    name = "job_runs_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0023]

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


async def run(
    connection,
    *,
    finished=True,
    outcome="SUCCEEDED",
    ready=False,
    reason=None,
    job="PAPER_EXIT_JOB",
    mode="EXITS_ONLY",
):
    run_id = uuid4()
    await connection.execute(
        text(RUN.replace(":finished", "now()" if finished else "NULL")),
        {
            "id": run_id,
            "job": job,
            "mode": mode,
            "outcome": outcome,
            "ready": ready,
            "reason": reason,
        },
    )
    return run_id


def test_0024_follows_0023_and_is_the_head():
    assert load(MODULE).down_revision == "0023"
    assert expected_revision() == "0024"


async def test_an_existing_pause_survives_the_upgrade_untouched(at_0023):
    async with at_0023.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))

    await apply(at_0023)

    assert await scalar(at_0023, "SELECT paused FROM paper_accounts") is True
    assert await scalar(at_0023, "SELECT COUNT(*) FROM paper_job_runs") == 0


async def test_a_running_row_is_finished_once_and_then_final(at_0023):
    await apply(at_0023)
    async with at_0023.begin() as connection:
        run_id = await run(connection, finished=False, outcome="RUNNING")
    async with at_0023.begin() as connection:
        await connection.execute(
            text(
                "UPDATE paper_job_runs SET outcome = 'SUCCEEDED', finished_at = now(),"
                " exit_ready = true WHERE run_id = :id"
            ),
            {"id": run_id},
        )

    for statement in (
        "UPDATE paper_job_runs SET exit_ready = false WHERE run_id = :id",
        "UPDATE paper_job_runs SET outcome = 'TIMED_OUT' WHERE run_id = :id",
    ):
        with pytest.raises(DBAPIError, match="a finished paper job run is final"):
            async with at_0023.begin() as connection:
                await connection.execute(text(statement), {"id": run_id})
    assert await scalar(at_0023, "SELECT exit_ready FROM paper_job_runs") is True


async def test_a_running_row_keeps_its_identity(at_0023):
    await apply(at_0023)
    async with at_0023.begin() as connection:
        run_id = await run(connection, finished=False, outcome="RUNNING")

    with pytest.raises(DBAPIError, match="keeps its identity"):
        async with at_0023.begin() as connection:
            await connection.execute(
                text("UPDATE paper_job_runs SET started_at = now() WHERE run_id = :id"),
                {"id": run_id},
            )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"finished": False, "outcome": "SUCCEEDED"}, "paper_job_run_finished"),
        ({"finished": True, "outcome": "RUNNING"}, "paper_job_run_finished"),
        ({"outcome": "TECHNICAL_FAILURE", "ready": True}, "paper_job_run_exit_ready"),
        ({"ready": True, "job": "PAPER_ENTRY_JOB", "mode": "FULL"}, "paper_job_run_exit_ready"),
        ({"ready": True, "reason": "EXIT_BACKLOG"}, "paper_job_run_exit_ready"),
        ({"outcome": "CRASHED"}, "paper_job_run_outcome"),
        ({"reason": "free text"}, "paper_job_run_codes"),
        ({"mode": "SOMETHING"}, "paper_job_run_mode"),
    ],
)
async def test_the_record_cannot_claim_what_did_not_happen(at_0023, values, constraint):
    await apply(at_0023)

    with pytest.raises(DBAPIError, match=constraint):
        async with at_0023.begin() as connection:
            await run(connection, **values)


async def test_a_pause_lifted_without_its_event_is_refused_at_commit(at_0023):
    async with at_0023.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))
    await apply(at_0023)

    with pytest.raises(DBAPIError, match="system pause cleared without an audit event"):
        async with at_0023.begin() as connection:
            await connection.execute(text("UPDATE paper_accounts SET paused = false"))
    assert await scalar(at_0023, "SELECT paused FROM paper_accounts") is True

    async with at_0023.begin() as connection:
        await connection.execute(text(PAUSE_EVENT), {"id": uuid4(), "actor": "OPERATOR"})
        await connection.execute(text("UPDATE paper_accounts SET paused = false"))
    assert await scalar(at_0023, "SELECT paused FROM paper_accounts") is False


async def test_only_an_operator_records_a_pause_lift(at_0023):
    await apply(at_0023)

    with pytest.raises(DBAPIError, match="paper_control_event_pause_actor"):
        async with at_0023.begin() as connection:
            await connection.execute(text(PAUSE_EVENT), {"id": uuid4(), "actor": "SENTINEL"})


async def test_the_downgrade_refuses_while_a_pause_lift_is_recorded(at_0023):
    async with at_0023.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))
    await apply(at_0023)
    async with at_0023.begin() as connection:
        await connection.execute(text(PAUSE_EVENT), {"id": uuid4(), "actor": "OPERATOR"})
        await connection.execute(text("UPDATE paper_accounts SET paused = false"))

    with pytest.raises(RuntimeError, match="Downgrade of 0024 refused"):
        await apply(at_0023, "downgrade")
    assert await scalar(at_0023, "SELECT COUNT(*) FROM paper_job_runs") == 0


async def test_a_clean_downgrade_keeps_the_pause_and_upgrades_again(at_0023):
    async with at_0023.begin() as connection:
        await connection.execute(text("UPDATE paper_accounts SET paused = true"))
    await apply(at_0023)
    async with at_0023.begin() as connection:
        await run(connection)

    await apply(at_0023, "downgrade")

    assert await scalar(at_0023, "SELECT paused FROM paper_accounts") is True
    assert (
        await scalar(
            at_0023,
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'paper_job_runs'"
            " AND table_schema = current_schema()",
        )
        == 0
    )
    await apply(at_0023)
    assert await scalar(at_0023, "SELECT paused FROM paper_accounts") is True
