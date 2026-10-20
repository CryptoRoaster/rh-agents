"""Migration 0027: network binding cache, pool creation time, request accounting.

Applied to a schema at `0026` in its own PostgreSQL schema. Existing rows read
zero, an empty list or NULL; nothing is reconstructed, estimated or backfilled.
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
from tests.scout.test_discovery_gaps_migration import ROW, THROUGH_0024

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0026 = (*THROUGH_0024, "0025_scout_discovery_gaps", "0026_v4_pool_index")
MODULE = "0027_discovery_optimization"
COUNTERS = (
    "network_requests",
    "network_cache_hits",
    "network_cache_rejected",
    "provider_http_attempts",
)


@pytest.fixture
async def at_0026():
    url = os.environ["TEST_DATABASE_URL"]
    name = "discovery_optimization_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0026]

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


async def shape(engine) -> dict[str, set[str]]:
    def read(sync):
        found = inspect(sync)
        return {
            table: {item["name"] for item in found.get_columns(table)}
            for table in found.get_table_names()
        }

    async with engine.connect() as connection:
        return await connection.run_sync(read)


def test_0027_follows_0026_and_is_the_head():
    assert load(MODULE).down_revision == "0026"
    assert expected_revision() == "0027"


async def test_existing_rows_read_zero_and_the_downgrade_restores_the_shape(at_0026):
    run = uuid4()
    async with at_0026.begin() as connection:
        await connection.execute(text(ROW), {"id": run})
    before = await shape(at_0026)
    await apply(at_0026)
    after = await shape(at_0026)
    assert after["provider_network_bindings"] == {
        "provider",
        "chain",
        "network_id",
        "platform",
        "verified_at",
    }
    assert "pool_created_at" in after["discovery_watches"]
    async with at_0026.connect() as connection:
        row = (
            await connection.execute(
                text(
                    f"SELECT {', '.join(COUNTERS)}, discovery_gap_reasons"
                    " FROM scout_runs WHERE id = :id"
                ),
                {"id": run},
            )
        ).one()
        bindings = await connection.scalar(text("SELECT count(*) FROM provider_network_bindings"))
    assert tuple(row) == (0, 0, 0, 0, [])
    assert bindings == 0
    await apply(at_0026, "downgrade")
    assert await shape(at_0026) == before
