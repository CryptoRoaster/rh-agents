"""Two passes, one account, and the guarantees that survive them.

PostgreSQL only. Row locks are what these prove, and SQLite has none — a test
that ran there would report a pass it had not earned.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tests.runner.conftest import (
    executions,
    ready_case,
    record_market,
    run,
    runner_settings,
    stack_for,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def test_two_concurrent_runs_produce_one_order_and_one_fill(risk_db, now, trace):
    """Two passes over one ready case converge; neither doubles anything."""

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-race")

    outcomes = await asyncio.gather(
        run(sessions, settings, now, run_id=uuid4()),
        run(sessions, settings, now, run_id=uuid4()),
        return_exceptions=True,
    )

    summaries = [item for item in outcomes if not isinstance(item, Exception)]
    assert summaries, outcomes
    assert sum(item.fills for item in summaries) <= 2
    assert len(await executions(sessions)) == 1
    async with sessions() as session:
        from src.data.tables import TradeCaseExecutionRow

        assert (await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))) == 1
