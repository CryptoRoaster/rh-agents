import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.clock import FixedClock
from src.data.tables import MarketObservationRow
from src.markets.fake import fixture_snapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder


@pytest.fixture
def observation(now, trace):
    return fixture_snapshot(now, trace)


@pytest.fixture
async def market_sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    schema = f"market_test_{uuid4().hex}"
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
                path = Path(__file__).parents[2] / "migrations/versions/0002_market_observations.py"
                spec = importlib.util.spec_from_file_location("market_migration", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                def migrate(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        module.upgrade()
                        next_path = path.with_name("0003_pool_locator.py")
                        next_spec = importlib.util.spec_from_file_location(
                            "locator_migration", next_path
                        )
                        next_module = importlib.util.module_from_spec(next_spec)
                        next_spec.loader.exec_module(next_module)
                        next_module.upgrade()

                await connection.run_sync(migrate)
            else:
                await connection.run_sync(MarketObservationRow.__table__.create)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.fixture
def recorder(market_sessions, now):
    return MarketRecorder(market_sessions, clock=FixedClock(now))


@pytest.fixture
def reader(market_sessions, now):
    return MarketReader(market_sessions, clock=FixedClock(now))
