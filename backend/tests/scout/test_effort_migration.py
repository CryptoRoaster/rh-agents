"""Migration 0015: requested and reported effort on scout assessments, structure only.

Applied to a schema at `0014` in its own PostgreSQL schema. Earlier rows keep
NULL in both columns; nothing is reconstructed.
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
from tests.scout.test_failure_reasons_migration import THROUGH_0013

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0014 = (*THROUGH_0013, "0014_scout_failure_reasons")
MODULE = "0015_scout_assessment_effort"


@pytest.fixture
async def at_0014():
    url = os.environ["TEST_DATABASE_URL"]
    name = "effort_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0014]

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


async def columns(engine):
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: {
                item["name"]: item
                for item in inspect(sync).get_columns("discovery_watch_assessments")
            }
        )


def test_the_chain_ends_at_0015():
    assert expected_revision() == "0015"
    assert load(MODULE).down_revision == "0014"


async def test_upgrade_adds_two_nullable_effort_columns(at_0014):
    await apply(at_0014)
    found = await columns(at_0014)
    assert found["reasoning_effort"]["nullable"] is True
    assert found["reported_effort"]["nullable"] is True


async def test_downgrade_removes_only_the_effort_columns(at_0014):
    await apply(at_0014)
    await apply(at_0014, "downgrade")
    found = await columns(at_0014)
    assert "reasoning_effort" not in found and "reported_effort" not in found
    assert "failure_reason_code" in found
