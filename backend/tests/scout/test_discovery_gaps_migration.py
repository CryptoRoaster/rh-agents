"""Migration 0025: three discovery counters on the scout run history, structure only.

Applied to a schema at `0024` in its own PostgreSQL schema. Existing run rows
read zero for every new counter; nothing is reconstructed or backfilled.
"""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.jobruns.test_migration import THROUGH_0023
from tests.reentry.test_migration import load

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0024 = (*THROUGH_0023, "0024_paper_job_runs")
MODULE = "0025_scout_discovery_gaps"
COLUMNS = ("discovery_no_overlap_polls", "discovery_page2_requests", "discovery_possible_gaps")
OLD_COUNTS = (
    "discovered, valid_markets, provider_identity_rejects, other_provider_rejects,"
    " watches_created, watches_updated, bootstrapped, refreshed, watches_due_orbit,"
    " orbit_reviews_started, orbit_reviews_completed, interesting, not_interesting,"
    " insufficient_data, watches_due_history, history_checks, vector_sufficient,"
    " promotable_new, dormant_new, retired_new, provider_failures, model_failures,"
    " provider_requests, orbit_backlog_before, orbit_backlog_after,"
    " new_watches_without_orbit_assessment, orbit_daily_budget, orbit_daily_used_before,"
    " orbit_daily_remaining_before, orbit_daily_used_after, orbit_daily_remaining_after"
)
ROW = (
    "INSERT INTO scout_runs (id, started_at, completed_at, status, stop, errors,"
    f" policy_version, {OLD_COUNTS}, oldest_orbit_due_age_seconds, created_at)"
    " VALUES (:id, now(), now(), 'COMPLETED', 'COMPLETED', '[]'::jsonb, 'early-scout-v2',"
    + ", ".join(["0"] * 31)
    + ", NULL, now())"
)


@pytest.fixture
async def at_0024():
    url = os.environ["TEST_DATABASE_URL"]
    name = "discovery_gaps_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0024]

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


async def columns(engine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_columns("scout_runs")}
        )


def test_0025_follows_0024():
    assert load(MODULE).down_revision == "0024"
    assert expected_revision() >= "0025"


async def test_existing_runs_read_zero_and_the_downgrade_drops_only_the_counters(at_0024):
    run = uuid4()
    async with at_0024.begin() as connection:
        await connection.execute(text(ROW), {"id": run})
    before = await columns(at_0024)
    await apply(at_0024)
    assert set(COLUMNS) <= await columns(at_0024)
    async with at_0024.connect() as connection:
        row = (
            await connection.execute(
                text(f"SELECT {', '.join(COLUMNS)} FROM scout_runs WHERE id = :id"), {"id": run}
            )
        ).one()
    assert tuple(row) == (0, 0, 0)
    await apply(at_0024, "downgrade")
    assert await columns(at_0024) == before
