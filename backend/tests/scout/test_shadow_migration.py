"""Migration 0016: stream declines and JEV shadow assessments, on PostgreSQL.

Applied to a schema at `0015` in its own PostgreSQL schema: integrity is held
by the database (foreign keys, uniqueness, checks), not by application code.
"""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.data.schema import expected_revision
from tests.reentry.test_migration import load
from tests.scout.test_effort_migration import THROUGH_0014

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating a real schema needs one"
)

THROUGH_0015 = (*THROUGH_0014, "0015_scout_assessment_effort")
MODULE = "0016_scout_shadow_triage"


@pytest.fixture
async def at_0015():
    url = os.environ["TEST_DATABASE_URL"]
    name = "shadow_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0015]

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


async def a_watch_and_observation(engine):
    """One observation and one watch on it, the rows an assessment points at."""
    watch, observation = uuid4(), uuid4()
    pair = "robinhood:mainnet:contract_address:0x" + "0f" * 20
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO market_observations (id, schema_version, provider, chain, network,"
                " pair_id, asset_id, is_fixture, correlation_id, observed_at, recorded_at,"
                " freshness_at, available, payload) VALUES (:id, 2, 'geckoterminal',"
                " 'robinhood', 'mainnet', :pair, :asset, false, :trace, now(), now(), now(),"
                " true, '{}'::jsonb)"
            ),
            {
                "id": observation,
                "pair": pair,
                "asset": "robinhood:mainnet:0x" + "9f" * 20,
                "trace": uuid4(),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO discovery_watches (id, schema_version, policy_version, provider,"
                " chain, network, pair_id, is_fixture, market_payload, first_seen_at,"
                " last_seen_at, latest_snapshot_id, created_at, updated_at, status,"
                " next_orbit_review_at, next_history_review_at, reason_code)"
                " VALUES (:id, 1, 'early-scout-v1', 'geckoterminal', 'robinhood', 'mainnet',"
                " :pair, false, '{}'::jsonb, now(), now(), :obs, now(), now(), 'WATCHING',"
                " now(), now(), 'WATCH_OPENED')"
            ),
            {"id": watch, "pair": pair, "obs": observation},
        )
    return watch, observation


SETTLED = datetime(2026, 9, 28, 6, tzinfo=UTC)

ROW = (
    "INSERT INTO discovery_watch_fast_assessments (id, watch_id, snapshot_id, utc_day,"
    " reserved_at, assessed_at, status, provider, model, question_version,"
    " input_schema_version, input_digest, input_payload, answers, failure_category)"
    " VALUES (:id, :watch, :obs, current_date, now(), :assessed, :status, 'jev', 'jev-1.13.0',"
    " :version, 1, 'd', '{\"schema_version\": 1}'::jsonb, CAST(:answers AS jsonb), :failure)"
)


def row(watch, obs, **overrides):
    values = {
        "id": uuid4(),
        "watch": watch,
        "obs": obs,
        "assessed": None,
        "status": "PENDING",
        "version": "jev-scout-v1",
        "answers": None,
        "failure": None,
    }
    values.update(overrides)
    return values


def test_the_chain_ends_at_0016():
    assert expected_revision() == "0016"
    assert load(MODULE).down_revision == "0015"


async def test_upgrade_creates_both_tables_empty(at_0015):
    await apply(at_0015)
    assert {"discovery_stream_declines", "discovery_watch_fast_assessments"} <= await tables(
        at_0015
    )
    async with at_0015.connect() as connection:
        for table in ("discovery_stream_declines", "discovery_watch_fast_assessments"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_an_assessment_must_point_at_a_real_watch(at_0015):
    await apply(at_0015)
    _, obs = await a_watch_and_observation(at_0015)
    with pytest.raises(IntegrityError):
        async with at_0015.begin() as connection:
            await connection.execute(text(ROW), row(uuid4(), obs))


async def test_one_assessment_per_watch_and_question_set(at_0015):
    await apply(at_0015)
    watch, obs = await a_watch_and_observation(at_0015)
    async with at_0015.begin() as connection:
        await connection.execute(text(ROW), row(watch, obs))
    with pytest.raises(IntegrityError):
        async with at_0015.begin() as connection:
            await connection.execute(text(ROW), row(watch, obs))
    # A later question set is a different assessment.
    async with at_0015.begin() as connection:
        await connection.execute(text(ROW), row(watch, obs, version="jev-scout-v2"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "COMPLETED", "assessed": SETTLED, "answers": None},
        {"status": "FAILED", "assessed": SETTLED, "failure": None},
        {"status": "PENDING", "assessed": SETTLED},
        {"status": "RUNNING"},
    ],
    ids=["completed_without_answers", "failed_without_reason", "pending_settled", "unknown"],
)
async def test_the_database_refuses_an_inconsistent_assessment(at_0015, overrides):
    await apply(at_0015)
    watch, obs = await a_watch_and_observation(at_0015)
    with pytest.raises(IntegrityError):
        async with at_0015.begin() as connection:
            await connection.execute(text(ROW), row(watch, obs, **overrides))


async def test_answers_round_trip_as_jsonb(at_0015):
    await apply(at_0015)
    watch, obs = await a_watch_and_observation(at_0015)
    answers = {"anomaly_signal": {"type": "noul", "noul": 0.2}}
    async with at_0015.begin() as connection:
        await connection.execute(
            text(ROW),
            row(
                watch,
                obs,
                status="COMPLETED",
                assessed=SETTLED,
                answers=json.dumps(answers),
            ),
        )
        stored = await connection.scalar(
            text(
                "SELECT answers -> 'anomaly_signal' ->> 'noul'"
                " FROM discovery_watch_fast_assessments"
            )
        )
    assert stored == "0.2"


async def test_a_stream_is_declined_once(at_0015):
    await apply(at_0015)
    insert = text(
        "INSERT INTO discovery_stream_declines (id, provider, chain, network, pair_id,"
        " is_fixture, reason, declined_at) VALUES (:id, 'geckoterminal', 'bsc', 'mainnet',"
        " 'bsc:mainnet:contract_address:0x01', false, 'WATCH_LIMIT_REACHED', now())"
    )
    async with at_0015.begin() as connection:
        await connection.execute(insert, {"id": uuid4()})
    with pytest.raises(IntegrityError):
        async with at_0015.begin() as connection:
            await connection.execute(insert, {"id": uuid4()})


async def test_downgrade_removes_only_what_0016_added(at_0015):
    await apply(at_0015)
    await apply(at_0015, "downgrade")
    remaining = await tables(at_0015)
    assert "discovery_stream_declines" not in remaining
    assert "discovery_watch_fast_assessments" not in remaining
    assert {"discovery_watches", "discovery_watch_assessments", "scout_runs"} <= remaining
