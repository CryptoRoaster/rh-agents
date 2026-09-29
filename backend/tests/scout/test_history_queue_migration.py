"""Migration 0020: history retry state and fair-queue run counters, on PostgreSQL."""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.exitpolicy.test_migration import THROUGH_0017
from tests.reentry.test_migration import load

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0019 = (*THROUGH_0017, "0018_paper_exit_triggers", "0019_scout_fresh_first_orbit")
MODULE = "0020_scout_history_fair_queue"
COUNTERS = {
    "history_eligible_now",
    "history_current_selected",
    "history_catchup_selected",
    "history_provider_requests",
    "history_backoff_set",
    "history_rate_limited",
    "oldest_history_due_age_seconds",
}
COLUMNS = {"history_retry_not_before", "history_failure_count", "history_last_failure"}


@pytest.fixture
async def at_0019():
    url = os.environ["TEST_DATABASE_URL"]
    name = "history_queue_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0019]

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


async def shape(engine):
    def read(sync):
        inspector = inspect(sync)
        watches = {item["name"] for item in inspector.get_columns("discovery_watches")}
        runs = {item["name"] for item in inspector.get_columns("scout_runs")}
        checks = {item["name"] for item in inspector.get_check_constraints("discovery_watches")}
        return watches, runs, checks

    async with engine.connect() as connection:
        return await connection.run_sync(read)


def test_the_chain_ends_at_0020():
    assert expected_revision() == "0020"
    assert load(MODULE).down_revision == "0019"


async def test_upgrade_adds_retry_state_and_counters_and_downgrade_removes_them(at_0019):
    watches, runs, checks = await shape(at_0019)
    assert not COLUMNS & watches and not COUNTERS & runs

    await apply(at_0019)
    watches, runs, checks = await shape(at_0019)
    assert COLUMNS <= watches and COUNTERS <= runs
    assert "discovery_watch_history_failure_count" in checks

    await apply(at_0019, "downgrade")
    watches, runs, checks = await shape(at_0019)
    assert not COLUMNS & watches and not COUNTERS & runs
    assert "discovery_watch_history_failure_count" not in checks
