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
from src.data.tables import Base
from src.orchestration.workflow.service import TradeCaseService


@pytest.fixture
async def workflow_db(now):
    url = os.environ.get("TEST_DATABASE_URL")
    schema = "workflow_test_" + uuid4().hex
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
                versions = Path(__file__).parents[2] / "migrations/versions"
                modules = []
                for name in ("0005_trade_case_workflow", "0006_worker_runtime"):
                    spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    modules.append(module)

                def migrate(sync_connection):
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        for item in modules:
                            item.upgrade()

                await connection.run_sync(migrate)
            else:
                await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield engine, sessions
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.fixture
def workflow_service(workflow_db, now):
    _, sessions = workflow_db
    return TradeCaseService(sessions, clock=FixedClock(now))
