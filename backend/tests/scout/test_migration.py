"""Migration 0012: discovery watches and their assessment history.

Applied to a schema at `0011` in its own PostgreSQL schema, the way a deployment
already has it. The migration creates structure only: it materialises no watch
from existing market observations — that is the bounded application bootstrap's
job, so the migration's own cost does not grow with history.

PostgreSQL only. The light suite builds its schema from the ORM metadata.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0011 = (
    "0001_foundation",
    "0002_market_observations",
    "0003_pool_locator",
    "0005_trade_case_workflow",
    "0006_worker_runtime",
    "0007_trade_case_risk_requests",
    "0008_trade_case_executions",
    "0009_position_market_identity",
    "0010_trade_case_exits",
    "0011_trade_cycles",
)
MODULE = "0012_discovery_watches"
NOW = datetime(2026, 9, 26, 6, tzinfo=UTC)


@pytest.fixture
async def at_0011():
    url = os.environ["TEST_DATABASE_URL"]
    name = "scout_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0011]

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


async def indexes(engine, table):
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_indexes(table)}
        )


WATCH = """
INSERT INTO discovery_watches (id, schema_version, policy_version, provider, chain, network,
  pair_id, is_fixture, market_payload, first_seen_at, last_seen_at, latest_snapshot_id,
  created_at, updated_at, status, next_orbit_review_at, orbit_checkpoint_index,
  next_history_review_at, latest_vector_sufficiency, vector_checked_at, reason_code,
  last_promoted_trade_case_id)
VALUES (:id, 1, 'early-scout-v1', 'geckoterminal', 'bsc', 'mainnet', :pair, false,
  '{}'::jsonb, :now, :now, gen_random_uuid(), :now, :now, 'WATCHING', :now, NULL, :now, NULL, NULL,
  'WATCH_OPENED', NULL)
"""

ASSESSMENT = """
INSERT INTO discovery_watch_assessments (id, watch_id, snapshot_id, assessed_at,
  checkpoint_index, checkpoint_seconds, status, failure_reason, classification, strength,
  reason_codes, data_gaps, cited_observation_ids, summary, input_digest, policy_version,
  prompt_version, prompt_hash, output_schema_version, reasoning_provider, reasoning_model,
  input_tokens, output_tokens, latency_ms)
VALUES (:id, :watch, :snapshot, :now, :index, 0, 'COMPLETED', NULL, 'NOT_INTERESTING', 'WEAK',
  '["PRICE_AVAILABLE"]'::jsonb, '[]'::jsonb, '[]'::jsonb, 'Observed.', :digest,
  'early-scout-v1', 'orbit-v1', :digest, 1, 'fake', 'echo', 1, 1, 1)
"""


async def test_0012_sits_on_0011_in_a_single_headed_chain():
    assert load(MODULE).down_revision == "0011"
    assert expected_revision() >= "0012"


async def test_upgrade_from_0011_creates_both_tables_and_their_indexes(at_0011):
    assert "discovery_watches" not in await tables(at_0011)
    await apply(at_0011)
    assert {"discovery_watches", "discovery_watch_assessments"} <= await tables(at_0011)
    assert {
        "ix_discovery_watches_orbit_due",
        "ix_discovery_watches_history_due",
        "ix_discovery_watches_first_seen",
    } <= await indexes(at_0011, "discovery_watches")
    assert "ix_discovery_watch_assessments_watch_time" in await indexes(
        at_0011, "discovery_watch_assessments"
    )


async def test_the_migration_materialises_no_watch_from_history(at_0011):
    async with at_0011.begin() as connection:
        before = await connection.scalar(text("SELECT count(*) FROM market_observations"))
    await apply(at_0011)
    async with at_0011.connect() as connection:
        watches = await connection.scalar(text("SELECT count(*) FROM discovery_watches"))
    assert before == 0 and watches == 0


async def test_one_watch_per_market_stream(at_0011):
    await apply(at_0011)
    pair = "bsc:mainnet:contract_address:0x" + "c1" * 20
    async with at_0011.begin() as connection:
        await connection.execute(text(WATCH), {"id": uuid4(), "pair": pair, "now": NOW})
    with pytest.raises(IntegrityError):
        async with at_0011.begin() as connection:
            await connection.execute(text(WATCH), {"id": uuid4(), "pair": pair, "now": NOW})


async def test_an_assessment_must_belong_to_a_watch_and_a_checkpoint_is_assessed_once(at_0011):
    await apply(at_0011)
    watch = uuid4()
    pair = "bsc:mainnet:contract_address:0x" + "c2" * 20
    values = {"snapshot": uuid4(), "now": NOW, "digest": "a" * 64}
    async with at_0011.begin() as connection:
        await connection.execute(text(WATCH), {"id": watch, "pair": pair, "now": NOW})
    with pytest.raises(IntegrityError):
        async with at_0011.begin() as connection:
            await connection.execute(
                text(ASSESSMENT), {**values, "id": uuid4(), "watch": uuid4(), "index": 0}
            )
    async with at_0011.begin() as connection:
        await connection.execute(
            text(ASSESSMENT), {**values, "id": uuid4(), "watch": watch, "index": 0}
        )
    with pytest.raises(IntegrityError):
        async with at_0011.begin() as connection:
            await connection.execute(
                text(ASSESSMENT), {**values, "id": uuid4(), "watch": watch, "index": 0}
            )


async def test_assessment_history_is_append_only(at_0011):
    await apply(at_0011)
    watch = uuid4()
    pair = "bsc:mainnet:contract_address:0x" + "c3" * 20
    async with at_0011.begin() as connection:
        await connection.execute(text(WATCH), {"id": watch, "pair": pair, "now": NOW})
        await connection.execute(
            text(ASSESSMENT),
            {
                "id": uuid4(),
                "watch": watch,
                "snapshot": uuid4(),
                "now": NOW,
                "digest": "b" * 64,
                "index": 0,
            },
        )
    for statement in (
        "UPDATE discovery_watch_assessments SET summary = 'rewritten'",
        "DELETE FROM discovery_watch_assessments",
    ):
        with pytest.raises(DBAPIError):
            async with at_0011.begin() as connection:
                await connection.execute(text(statement))


async def test_an_unknown_status_is_refused(at_0011):
    await apply(at_0011)
    pair = "bsc:mainnet:contract_address:0x" + "c4" * 20
    with pytest.raises(IntegrityError):
        async with at_0011.begin() as connection:
            await connection.execute(
                text(WATCH.replace("'WATCHING'", "'INTERESTING'")),
                {"id": uuid4(), "pair": pair, "now": NOW},
            )


async def test_downgrade_removes_exactly_what_it_added(at_0011):
    await apply(at_0011)
    await apply(at_0011, "downgrade")
    remaining = await tables(at_0011)
    assert "discovery_watches" not in remaining
    assert "discovery_watch_assessments" not in remaining
    assert "trade_cases" in remaining and "market_observations" in remaining
