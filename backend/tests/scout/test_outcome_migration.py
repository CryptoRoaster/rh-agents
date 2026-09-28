"""Migration 0017: bar store and discovery outcome labels, on PostgreSQL."""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_shadow_migration import THROUGH_0015, an_observation

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0016 = (*THROUGH_0015, "0016_scout_shadow_triage")
MODULE = "0017_discovery_outcomes"
TABLES = {
    "market_ohlcv_fetches",
    "market_ohlcv_bars",
    "discovery_outcome_samples",
    "discovery_stream_outcomes",
}


@pytest.fixture
async def at_0016():
    url = os.environ["TEST_DATABASE_URL"]
    name = "outcome_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0016]

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


async def tables(engine):
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))


FETCH = (
    "INSERT INTO market_ohlcv_fetches (id, provider, chain, network, pair_id, is_fixture,"
    " timeframe, aggregate, window_start, window_end, fetched_at, source, bars_returned)"
    " VALUES (:id, 'geckoterminal', 'robinhood', 'mainnet', :pair, false, 'minute', 15,"
    " now() - interval '1 hour', now(), now(), 'OUTCOME_SAMPLER', 1)"
)
BAR = (
    "INSERT INTO market_ohlcv_bars (id, fetch_id, provider, chain, network, pair_id,"
    " is_fixture, timeframe, aggregate, opened_at, open, high, low, close, volume)"
    " VALUES (:id, :fetch, 'geckoterminal', 'robinhood', 'mainnet', :pair, false, 'minute', 15,"
    " '2026-09-28T06:00:00Z', 1, :high, 1, 1, 10)"
)
SAMPLE = (
    "INSERT INTO discovery_outcome_samples (id, provider, chain, network, pair_id, is_fixture,"
    " schema_version, reference_observation_id, reference_at, reference_price_usd, sampled_at,"
    " status, history_source, provider_requests) VALUES (:id, 'geckoterminal', 'robinhood',"
    " 'mainnet', :pair, false, 1, :obs, now(), 0.000000000001234, now(), :status, 'FETCHED', 1)"
)
LABEL = (
    "INSERT INTO discovery_stream_outcomes (id, sample_id, provider, chain, network, pair_id,"
    " is_fixture, horizon_minutes, schema_version, status, missing_reason, bars_used,"
    " computed_at) VALUES (:id, :sample, 'geckoterminal', 'robinhood', 'mainnet', :pair, false,"
    " :horizon, 1, :status, :reason, 0, now())"
)


def test_the_chain_ends_at_0017():
    assert expected_revision() == "0017"
    assert load(MODULE).down_revision == "0016"


async def test_upgrade_creates_the_tables_and_keeps_tiny_prices_exact(at_0016):
    await apply(at_0016)
    assert TABLES <= await tables(at_0016)
    pair, obs = await an_observation(at_0016)
    sample = uuid4()
    async with at_0016.begin() as connection:
        await connection.execute(
            text(SAMPLE), {"id": sample, "pair": pair, "obs": obs, "status": "COMPLETE"}
        )
        price = await connection.scalar(
            text("SELECT reference_price_usd FROM discovery_outcome_samples")
        )
    assert price == Decimal("0.000000000001234")


async def test_a_stream_is_sampled_and_labelled_once_per_horizon(at_0016):
    await apply(at_0016)
    pair, obs = await an_observation(at_0016)
    sample = uuid4()
    async with at_0016.begin() as connection:
        await connection.execute(
            text(SAMPLE), {"id": sample, "pair": pair, "obs": obs, "status": "COMPLETE"}
        )
        await connection.execute(
            text(LABEL),
            {
                "id": uuid4(),
                "sample": sample,
                "pair": pair,
                "horizon": 60,
                "status": "LABELLED",
                "reason": None,
            },
        )
    with pytest.raises(IntegrityError):
        async with at_0016.begin() as connection:
            await connection.execute(
                text(SAMPLE), {"id": uuid4(), "pair": pair, "obs": obs, "status": "COMPLETE"}
            )
    with pytest.raises(IntegrityError):
        async with at_0016.begin() as connection:
            await connection.execute(
                text(LABEL),
                {
                    "id": uuid4(),
                    "sample": sample,
                    "pair": pair,
                    "horizon": 60,
                    "status": "LABELLED",
                    "reason": None,
                },
            )


@pytest.mark.parametrize(
    "values",
    [
        {"horizon": 60, "status": "MISSING", "reason": None},
        {"horizon": 60, "status": "LABELLED", "reason": "HISTORY_NOT_COVERED"},
        {"horizon": 0, "status": "LABELLED", "reason": None},
        {"horizon": 60, "status": "GUESSED", "reason": None},
    ],
    ids=["missing_without_reason", "labelled_with_reason", "zero_horizon", "unknown_status"],
)
async def test_the_database_refuses_an_inconsistent_label(at_0016, values):
    await apply(at_0016)
    pair, obs = await an_observation(at_0016)
    sample = uuid4()
    async with at_0016.begin() as connection:
        await connection.execute(
            text(SAMPLE), {"id": sample, "pair": pair, "obs": obs, "status": "PARTIAL"}
        )
    with pytest.raises(IntegrityError):
        async with at_0016.begin() as connection:
            await connection.execute(
                text(LABEL), {"id": uuid4(), "sample": sample, "pair": pair, **values}
            )


async def test_a_bar_is_kept_once_and_must_have_a_shape(at_0016):
    await apply(at_0016)
    pair = "robinhood:mainnet:contract_address:0x" + "0f" * 20
    fetch = uuid4()
    async with at_0016.begin() as connection:
        await connection.execute(text(FETCH), {"id": fetch, "pair": pair})
        await connection.execute(
            text(BAR), {"id": uuid4(), "fetch": fetch, "pair": pair, "high": 2}
        )
    with pytest.raises(IntegrityError):
        async with at_0016.begin() as connection:
            await connection.execute(
                text(BAR), {"id": uuid4(), "fetch": fetch, "pair": pair, "high": 2}
            )
    with pytest.raises(IntegrityError):
        async with at_0016.begin() as connection:
            await connection.execute(
                text(BAR.replace("'2026-09-28T06:00:00Z'", "'2026-09-28T06:15:00Z'")),
                {"id": uuid4(), "fetch": fetch, "pair": pair, "high": 0.5},
            )


async def test_downgrade_removes_only_what_0017_added(at_0016):
    await apply(at_0016)
    await apply(at_0016, "downgrade")
    remaining = await tables(at_0016)
    assert not TABLES & remaining
    assert {"discovery_fast_assessments", "discovery_stream_declines"} <= remaining
