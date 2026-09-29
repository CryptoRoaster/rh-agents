"""Migration 0019: the ORBIT review-debt state and the V2 run counters, on PostgreSQL."""

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

THROUGH_0018 = (*THROUGH_0017, "0018_paper_exit_triggers")
MODULE = "0019_scout_fresh_first_orbit"
COUNTERS = {
    "orbit_fresh_first_reviews_due",
    "orbit_first_reviews_skipped_stale",
    "orbit_follow_ups_deferred",
    "orbit_slots_released",
}


@pytest.fixture
async def at_0018():
    url = os.environ["TEST_DATABASE_URL"]
    name = "fresh_first_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0018]

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


def test_the_chain_ends_at_0019():
    assert expected_revision() == "0019"
    assert load(MODULE).down_revision == "0018"


async def test_upgrade_adds_the_state_and_counters_and_downgrade_removes_them(at_0018):
    watches, runs, checks = await shape(at_0018)
    assert "orbit_state" not in watches and not COUNTERS & runs

    await apply(at_0018)
    watches, runs, checks = await shape(at_0018)
    assert {"orbit_state", "orbit_state_at"} <= watches
    assert COUNTERS <= runs
    assert {"discovery_watch_orbit_state", "discovery_watch_orbit_state_dated"} <= checks

    await apply(at_0018, "downgrade")
    after, runs_after, checks_after = await shape(at_0018)
    assert "orbit_state" not in after and not COUNTERS & runs_after
    assert "discovery_watch_orbit_state" not in checks_after
