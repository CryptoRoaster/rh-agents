"""Migration 0018: the exit trigger columns on `trade_case_exits`, on PostgreSQL."""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_outcome_migration import THROUGH_0016

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0017 = (*THROUGH_0016, "0017_discovery_outcomes")
MODULE = "0018_paper_exit_triggers"
COLUMNS = {"exit_trigger", "exit_policy_version", "exit_trigger_basis"}


@pytest.fixture
async def at_0017():
    url = os.environ["TEST_DATABASE_URL"]
    name = "exit_trigger_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0017]

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
        columns = {item["name"]: item for item in inspector.get_columns("trade_case_exits")}
        checks = {item["name"] for item in inspector.get_check_constraints("trade_case_exits")}
        return columns, checks

    async with engine.connect() as connection:
        return await connection.run_sync(read)


def test_the_chain_ends_at_0018():
    assert expected_revision() == "0018"
    assert load(MODULE).down_revision == "0017"


async def test_the_columns_arrive_nullable_with_their_pairing_check(at_0017):
    before, _ = await shape(at_0017)
    assert not COLUMNS & set(before)

    await apply(at_0017)
    columns, checks = await shape(at_0017)
    assert COLUMNS <= set(columns)
    assert all(columns[name]["nullable"] for name in COLUMNS)
    assert "JSONB" in str(columns["exit_trigger_basis"]["type"]).upper()
    assert "trade_case_exit_trigger_versioned" in checks


async def test_the_downgrade_removes_exactly_what_it_added(at_0017):
    before, checks_before = await shape(at_0017)
    await apply(at_0017)
    await apply(at_0017, "downgrade")
    after, checks_after = await shape(at_0017)
    assert set(after) == set(before)
    assert checks_after == checks_before
