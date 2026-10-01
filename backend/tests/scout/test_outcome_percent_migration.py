"""Migration 0022: stream outcome percents become NUMERIC(24, 6), on PostgreSQL."""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.markets.test_quote_price_migration import THROUGH_0020
from tests.reentry.test_migration import load
from tests.scout.test_outcome_migration import SAMPLE
from tests.scout.test_shadow_migration import an_observation

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0021 = (*THROUGH_0020, "0021_market_snapshot_quote_price")
MODULE = "0022_stream_outcome_percent_type"
COLUMNS = ("return_pct", "max_return_pct", "max_drawdown_pct")
LABEL = (
    "INSERT INTO discovery_stream_outcomes (id, sample_id, provider, chain, network, pair_id,"
    " is_fixture, horizon_minutes, schema_version, status, bars_used, computed_at,"
    " return_pct, max_return_pct, max_drawdown_pct) VALUES (:id, :sample, 'geckoterminal',"
    " 'robinhood', 'mainnet', :pair, false, :horizon, 1, 'LABELLED', 1, now(), :r, :m, :d)"
)


def test_the_chain_ends_at_0022():
    assert expected_revision() == "0022"
    assert load(MODULE).down_revision == "0021"


@pytest.fixture
async def at_0021():
    url = os.environ["TEST_DATABASE_URL"]
    name = "outcome_percent_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0021]

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


async def types(engine):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a"
                " JOIN pg_class c ON c.oid = a.attrelid"
                " WHERE c.relname = 'discovery_stream_outcomes' AND a.attnum > 0"
            )
        )
        found = dict(rows.all())
    return {name: found[name] for name in COLUMNS}


async def a_sample(engine):
    pair, obs = await an_observation(engine)
    sample = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(SAMPLE), {"id": sample, "pair": pair, "obs": obs, "status": "COMPLETE"}
        )
    return pair, sample


async def label(engine, pair, sample, horizon, r, m, d):
    async with engine.begin() as connection:
        await connection.execute(
            text(LABEL),
            {
                "id": uuid4(),
                "sample": sample,
                "pair": pair,
                "horizon": horizon,
                "r": r,
                "m": m,
                "d": d,
            },
        )


async def values(engine):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT horizon_minutes, return_pct, max_return_pct, max_drawdown_pct"
                " FROM discovery_stream_outcomes ORDER BY horizon_minutes"
            )
        )
        return rows.all()


async def test_upgrade_types_the_columns_and_keeps_existing_rows_exact(at_0021):
    assert set((await types(at_0021)).values()) == {"numeric"}
    pair, sample = await a_sample(at_0021)
    await label(
        at_0021, pair, sample, 15, Decimal("-12.345678"), Decimal("250.5"), Decimal("-99.9")
    )
    await label(at_0021, pair, sample, 60, Decimal("292134308220.720813"), Decimal("0"), None)
    before = await values(at_0021)

    await apply(at_0021)

    assert set((await types(at_0021)).values()) == {"numeric(24,6)"}
    assert [tuple(row) for row in await values(at_0021)] == [tuple(row) for row in before]


async def test_100x_and_1000x_are_storable_after_the_upgrade(at_0021):
    await apply(at_0021)
    pair, sample = await a_sample(at_0021)
    await label(at_0021, pair, sample, 15, Decimal("9900"), Decimal("9900"), Decimal("-50"))
    await label(at_0021, pair, sample, 60, Decimal("99900"), Decimal("99900"), Decimal("0"))
    stored = await values(at_0021)
    assert [row[1] for row in stored] == [Decimal("9900.000000"), Decimal("99900.000000")]


async def test_the_type_refuses_what_it_cannot_hold(at_0021):
    await apply(at_0021)
    pair, sample = await a_sample(at_0021)
    with pytest.raises(DBAPIError):
        await label(
            at_0021,
            pair,
            sample,
            15,
            Decimal("0"),
            Decimal("2099833095042667926.688429"),
            Decimal("0"),
        )


async def test_an_unconvertible_existing_value_blocks_the_upgrade(at_0021):
    """Never rounded or truncated to fit: the upgrade refuses."""
    pair, sample = await a_sample(at_0021)
    await label(at_0021, pair, sample, 15, Decimal("1.1234567"), Decimal("0"), Decimal("0"))
    with pytest.raises(DBAPIError):
        await apply(at_0021)
    assert set((await types(at_0021)).values()) == {"numeric"}


async def test_downgrade_restores_unbounded_numeric(at_0021):
    await apply(at_0021)
    pair, sample = await a_sample(at_0021)
    await label(at_0021, pair, sample, 15, Decimal("9900"), Decimal("99900"), Decimal("-1"))
    await apply(at_0021, "downgrade")
    assert set((await types(at_0021)).values()) == {"numeric"}
    assert (await values(at_0021))[0][1] == Decimal("9900.000000")
