"""Migration 0013: the scout run history table, structure only.

Applied to a schema at `0012` in its own PostgreSQL schema. It creates the table
and reconstructs nothing: runs from before it simply have no history.
"""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_migration import THROUGH_0011

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0012 = (*THROUGH_0011, "0012_discovery_watches")
MODULE = "0013_scout_runs"
COUNTS = (
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
    f" policy_version, {COUNTS}, oldest_orbit_due_age_seconds, created_at)"
    " VALUES (:id, now(), now(), :status, 'COMPLETED', '[]'::jsonb, 'early-scout-v1',"
    + ", ".join(["0"] * 31)
    + ", NULL, now())"
)


@pytest.fixture
async def at_0012():
    url = os.environ["TEST_DATABASE_URL"]
    name = "scout_runs_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0012]

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


def test_the_chain_ends_at_0013():
    assert expected_revision() == "0013"
    assert load(MODULE).down_revision == "0012"


async def test_upgrade_creates_an_empty_run_table(at_0012):
    await apply(at_0012)
    assert {"scout_runs", "scout_orbit_reservations"} <= await tables(at_0012)
    async with at_0012.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM scout_runs")) == 0
        indexes = await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_indexes("scout_runs")}
        )
    assert "ix_scout_runs_started" in indexes


async def test_only_terminal_statuses_are_stored(at_0012):
    await apply(at_0012)
    async with at_0012.begin() as connection:
        await connection.execute(text(ROW), {"id": uuid4(), "status": "COMPLETED"})
    with pytest.raises(IntegrityError):
        async with at_0012.begin() as connection:
            await connection.execute(text(ROW), {"id": uuid4(), "status": "RUNNING"})


async def test_downgrade_removes_only_the_run_table(at_0012):
    await apply(at_0012)
    await apply(at_0012, "downgrade")
    remaining = await tables(at_0012)
    assert "scout_runs" not in remaining
    assert "scout_orbit_reservations" not in remaining
    assert {"discovery_watches", "discovery_watch_assessments"} <= remaining


async def test_a_reservation_status_is_one_of_three(at_0012):
    await apply(at_0012)
    watch = uuid4()
    async with at_0012.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO discovery_watches (id, schema_version, policy_version, provider,"
                " chain, network, pair_id, is_fixture, market_payload, first_seen_at,"
                " last_seen_at, latest_snapshot_id, created_at, updated_at, status,"
                " next_orbit_review_at, orbit_checkpoint_index, next_history_review_at,"
                " latest_vector_sufficiency, vector_checked_at, reason_code,"
                " last_promoted_trade_case_id) VALUES (:id, 1, 'early-scout-v1', 'geckoterminal',"
                " 'bsc', 'mainnet', 'bsc:mainnet:contract_address:0xc5', false, '{}'::jsonb,"
                " now(), now(), gen_random_uuid(), now(), now(), 'WATCHING', now(), NULL, now(),"
                " NULL, NULL, 'WATCH_OPENED', NULL)"
            ),
            {"id": watch},
        )
    insert = text(
        "INSERT INTO scout_orbit_reservations (id, watch_id, checkpoint_index, utc_day,"
        " reserved_at, status) VALUES (:id, :watch, :index, current_date, now(), :status)"
    )
    async with at_0012.begin() as connection:
        await connection.execute(
            insert, {"id": uuid4(), "watch": watch, "index": 0, "status": "RESERVED"}
        )
    with pytest.raises(IntegrityError):
        async with at_0012.begin() as connection:
            await connection.execute(
                insert, {"id": uuid4(), "watch": watch, "index": 1, "status": "CANCELLED"}
            )
    with pytest.raises(IntegrityError):
        async with at_0012.begin() as connection:
            await connection.execute(
                insert, {"id": uuid4(), "watch": watch, "index": 0, "status": "RESERVED"}
            )
