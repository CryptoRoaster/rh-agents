"""Issue #94: the PostgreSQL pool index store keeps the indexer's contract.

The same indexer runs against the SQL store as against the in-memory one and
must end in the same state; coverage moves only from where it was, a pool is
recorded once, and a rewind drops exactly what lies beyond it. On PostgreSQL
the schema is created by migration 0026 itself.
"""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agents.atlas.v4.pool_index import IndexConflict, IndexCoverage, V4PoolIndexer
from src.agents.atlas.v4.pool_index_sql import SqlPoolIndexStore
from src.data.tables import Base, V4PoolIndexCoverageRow, V4PoolIndexPoolRow
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import deployment
from tests.atlas.v4.test_pool_index import CONFIRMATIONS, indexed_chain


@pytest.fixture
async def store():
    url = os.environ.get("TEST_DATABASE_URL")
    schema = "pool_index_test_" + uuid4().hex
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            if url:
                path = Path(__file__).parents[3] / "migrations/versions/0026_v4_pool_index.py"
                spec = importlib.util.spec_from_file_location("m0026", path)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                def migrate(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        module.upgrade()

                await connection.run_sync(migrate)
            else:
                tables = [V4PoolIndexCoverageRow.__table__, V4PoolIndexPoolRow.__table__]
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(sync, tables=tables)
                )
        yield SqlPoolIndexStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


def indexer(chain, target, max_requests=200):
    return V4PoolIndexer(
        reads=chain,
        store=target,
        deployment=deployment(*chain.position_managers),
        confirmations=CONFIRMATIONS,
        max_requests=max_requests,
    )


async def test_the_sql_store_ends_where_the_memory_store_does(store) -> None:
    chain, _, early = indexed_chain()
    memory = MemoryPoolIndexStore()
    assert (await indexer(chain, memory).advance()).status == "COMPLETE"
    sql_chain, _, _ = indexed_chain()
    assert (await indexer(sql_chain, store, max_requests=34).advance()).status == "PARTIAL"
    assert (await indexer(sql_chain, store).advance()).status == "COMPLETE"
    key = (chain.network_id, chain.pool_manager)
    assert await store.coverage(*key) == memory.coverages[key]
    for before in (early.created_block, early.created_block + 1, chain.head):
        assert await store.pools_with(*key, TOKEN, before) == await memory.pools_with(
            *key, TOKEN, before
        )


async def test_coverage_moves_only_from_where_it_was(store) -> None:
    chain, _, _ = indexed_chain()
    await indexer(chain, store).advance()
    key = (chain.network_id, chain.pool_manager)
    coverage = await store.coverage(*key)
    assert coverage is not None
    moved = IndexCoverage(
        coverage.chain_id,
        coverage.pool_manager,
        coverage.deployment_block,
        coverage.through_block + 5,
        "0x" + "ab" * 32,
    )
    with pytest.raises(IndexConflict):
        await store.extend(moved, (), expected_through=coverage.through_block - 1)
    with pytest.raises(IndexConflict):
        await store.begin(coverage)
    # A pool recorded twice is a conflict, not a duplicate row.
    existing = (await store.pools_with(*key, TOKEN, chain.head))[0]
    with pytest.raises(IndexConflict):
        await store.extend(moved, (existing,), expected_through=coverage.through_block)
    assert await store.coverage(*key) == coverage


async def test_a_rewind_drops_exactly_what_lies_beyond_it(store) -> None:
    chain, traded, early = indexed_chain()
    await indexer(chain, store).advance()
    key = (chain.network_id, chain.pool_manager)
    coverage = await store.coverage(*key)
    assert coverage is not None
    back = IndexCoverage(
        coverage.chain_id,
        coverage.pool_manager,
        coverage.deployment_block,
        early.created_block,
        "0x" + "cd" * 32,
    )
    await store.rewind(back, expected_through=coverage.through_block)
    kept = await store.pools_with(*key, TOKEN, chain.head)
    assert [pool.pool_id for pool in kept] == [early.id]
    assert await store.coverage(*key) == back
