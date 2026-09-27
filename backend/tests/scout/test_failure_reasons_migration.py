"""Migration 0014: failure reason codes beside their categories, structure only.

Applied to a schema at `0013` in its own PostgreSQL schema. Earlier rows keep
NULL and an empty list; nothing is reconstructed.
"""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_runs_migration import ROW, THROUGH_0012

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0013 = (*THROUGH_0012, "0013_scout_runs")
MODULE = "0014_scout_failure_reasons"


@pytest.fixture
async def at_0013():
    url = os.environ["TEST_DATABASE_URL"]
    name = "failure_reasons_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0013]

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


async def columns(engine, table):
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: {item["name"]: item for item in inspect(sync).get_columns(table)}
        )


def test_the_chain_ends_at_0014():
    assert expected_revision() == "0014"
    assert load(MODULE).down_revision == "0013"


async def test_upgrade_adds_the_codes_and_keeps_earlier_runs(at_0013):
    run = uuid4()
    async with at_0013.begin() as connection:
        await connection.execute(text(ROW), {"id": run, "status": "COMPLETED"})

    await apply(at_0013)

    assessment = await columns(at_0013, "discovery_watch_assessments")
    assert assessment["failure_reason_code"]["nullable"] is True
    async with at_0013.connect() as connection:
        reasons = await connection.scalar(
            text("SELECT model_failure_reasons FROM scout_runs WHERE id = :id"), {"id": run}
        )
        checks = await connection.run_sync(
            lambda sync: {
                item["name"]
                for item in inspect(sync).get_check_constraints("discovery_watch_assessments")
            }
        )
    assert reasons == []
    assert "discovery_watch_assessment_failure_code" in checks


async def test_downgrade_removes_only_what_0014_added(at_0013):
    await apply(at_0013)
    await apply(at_0013, "downgrade")
    assert "failure_reason_code" not in await columns(at_0013, "discovery_watch_assessments")
    runs = await columns(at_0013, "scout_runs")
    assert "model_failure_reasons" not in runs
    assert "model_failures" in runs
