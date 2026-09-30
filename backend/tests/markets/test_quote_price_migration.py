"""Migration 0021: version-3 market observations, on PostgreSQL."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_history_queue_migration import THROUGH_0019

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0020 = (*THROUGH_0019, "0020_scout_history_fair_queue")
MODULE = "0021_market_snapshot_quote_price"


def test_the_chain_ends_at_0021():
    assert expected_revision() == "0021"
    assert load(MODULE).down_revision == "0020"


@pytest.fixture
async def at_0020():
    url = os.environ["TEST_DATABASE_URL"]
    name = "quote_price_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0020]

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


async def insert(engine, version):
    at = datetime(2026, 9, 30, tzinfo=UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO market_observations (id, schema_version, provider, chain, network,"
                " asset_id, pair_id, correlation_id, observed_at, recorded_at, freshness_at,"
                " available, is_fixture, payload) VALUES (:id, :v, 'geckoterminal', 'robinhood',"
                " 'mainnet', 'robinhood:mainnet:0xa', 'robinhood:mainnet:p', :c, :t, :t, :t,"
                " true, false, '{}')"
            ),
            {"id": uuid4(), "v": version, "c": uuid4(), "t": at},
        )


async def test_version_3_is_refused_before_and_accepted_after(at_0020):
    with pytest.raises(IntegrityError):
        await insert(at_0020, 3)
    await apply(at_0020)
    await insert(at_0020, 3)
    for legacy in (1, 2):
        await insert(at_0020, legacy)
    with pytest.raises(IntegrityError):
        await insert(at_0020, 4)


async def test_downgrade_never_erases_version_3_observations(at_0020):
    await apply(at_0020)
    await insert(at_0020, 3)
    with pytest.raises(DBAPIError):
        await apply(at_0020, "downgrade")
    async with at_0020.connect() as connection:
        kept = await connection.scalar(
            text("SELECT count(*) FROM market_observations WHERE schema_version = 3")
        )
    assert kept == 1


async def test_downgrade_without_version_3_restores_the_old_check(at_0020):
    await apply(at_0020)
    await apply(at_0020, "downgrade")
    with pytest.raises(IntegrityError):
        await insert(at_0020, 3)
    await insert(at_0020, 2)
