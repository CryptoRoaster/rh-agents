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
from src.core.config import Settings
from src.data.tables import EvmCursorRow, EvmLogRow, RuntimeAuditRow
from src.runtime.models import ChainConfig
from src.runtime.store import RuntimeStore


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        evm_retry_delay_seconds=0,
    )


@pytest.fixture(params=["robinhood", "bsc"])
def chain_config(request):
    chain = request.param
    return ChainConfig(
        chain=chain,
        chain_id=4663 if chain == "robinhood" else 56,
        http_url="https://rpc.invalid/secret-token?key=secret-token",
        ws_url="wss://rpc.invalid/secret-token",
        confirmations=2,
    )


@pytest.fixture
async def runtime_db(now):
    url = os.environ.get("TEST_DATABASE_URL")
    schema = "runtime_test_" + uuid4().hex
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as c:
            if url:
                path = Path(__file__).parents[2] / "migrations/versions/0004_data_runtime.py"
                spec = importlib.util.spec_from_file_location("runtime_migration", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                def migrate(connection):
                    with Operations.context(MigrationContext.configure(connection)):
                        module.upgrade()

                await c.run_sync(migrate)
            else:
                for table in (
                    RuntimeAuditRow.__table__,
                    EvmCursorRow.__table__,
                    EvmLogRow.__table__,
                ):
                    await c.run_sync(table.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield engine, RuntimeStore(sessions, FixedClock(now))
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as c:
                await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
