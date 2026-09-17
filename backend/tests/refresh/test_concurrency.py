"""Two passes over one case whose decision basis has aged out.

PostgreSQL only. What these prove is that the case row's lock serializes the
refresh order and the request key serializes everything that follows from it,
and SQLite has neither — a test that ran there would report a pass it had not
earned.
"""

import asyncio
import os

import pytest
from sqlalchemy import func, select

from src.core.models import AgentRole
from src.data.tables import TradeCaseRiskRequestRow
from tests.refresh.conftest import (
    at_the_recheck,
    first_pass,
    ports_at,
    scripted,
    task_row,
)
from tests.refresh.test_refresh import waited
from tests.runner.conftest import executions, run

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def test_two_runs_at_once_produce_one_refresh_and_one_fill(risk_db, now, trace):
    """A restart or an overlap must not double anything.

    Both passes are real and both may claim; the case row serializes the order
    and the request key serializes the order that follows from it.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)

    both = await asyncio.gather(
        run(sessions, settings, later, ports=ports_at(later, model)),
        run(sessions, settings, later, ports=ports_at(later, model)),
        return_exceptions=True,
    )

    for item in both:
        assert not isinstance(item, BaseException), item
    # Which of the two gets there first is a genuine race and not this test's
    # business. What must hold either way is that nothing happened twice — and
    # a run that addressed the same order key and was handed the same fill back
    # reports that fill, so the count to check is the durable one, not how many
    # runs saw it.
    assert len(await executions(sessions)) <= 1
    ordered = [item for item in both for case in item.cases if case.refresh == "ORDERED"]
    assert len(ordered) == 1, "the second run found the order the first had placed"
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert observer.attempt == 2, "only one of the two runs ordered a new observation"
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) <= 1

    # And nothing was lost either: one more explicit pass completes whatever the
    # race left unfinished, and the totals are still one of each.
    await run(sessions, settings, later, ports=ports_at(later, model))
    assert len(await executions(sessions)) == 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 1
    assert (await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")).attempt == 2
