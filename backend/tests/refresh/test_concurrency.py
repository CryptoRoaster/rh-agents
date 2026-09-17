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
    traded_case,
)
from tests.refresh.test_refresh import waited
from tests.runner.conftest import executions, run

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def test_two_runs_at_once_produce_one_order_and_one_fill(risk_db, now, trace):
    """Both passes reach the same case needing the same new observation.

    Which of them gets there first is a genuine race and not this test's
    business. What must hold either way is that only one order is placed, only
    one canonical request exists, and at most one fill happens — and that the
    work the race left unfinished is still there for the next explicit pass.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    # Stopped one step before the order, so both runs below start from a case
    # that is ready, refused, and has nothing outstanding.
    staged = type(settings).model_validate({**settings.model_dump(), "paper_runner_max_steps": 4})
    await run(sessions, staged, later, ports=ports_at(later, model))
    case = await traded_case(sessions)
    assert case.status == "READY_FOR_RISK"
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (observer.status, observer.attempt) == ("SUCCEEDED", 1)

    both = await asyncio.gather(
        run(sessions, settings, later, ports=ports_at(later, model)),
        run(sessions, settings, later, ports=ports_at(later, model)),
        return_exceptions=True,
    )

    for item in both:
        assert not isinstance(item, BaseException), item
    outcomes = [
        entry.outcome for item in both for progress in item.cases for entry in progress.refreshes
    ]
    assert outcomes.count("ORDERED") <= 1, outcomes
    # One order, whoever placed it: the slot moved by exactly one attempt.
    armed = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert armed.attempt == 2, outcomes
    assert len(await executions(sessions)) <= 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) <= 1

    # And nothing was lost: one more explicit pass completes whatever the race
    # left unfinished, and the totals are still one of each.
    await run(sessions, settings, later, ports=ports_at(later, model))
    assert len(await executions(sessions)) == 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 1
    assert (await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")).attempt == 2


async def test_two_runs_at_once_order_one_execution_reassessment(risk_db, now, trace):
    """The same guarantee for the second observer, on its own gap.

    Both passes find a case whose execution assessment has expired. The case row
    serializes the order, so one of them places it and the other is told it
    already exists — and the slot moves by exactly one attempt.
    """
    from tests.refresh.conftest import ANCHOR_SOURCE
    from tests.refresh.test_execution import carried_to_an_expired_assessment, observer_attempts

    _, sessions = risk_db
    model = scripted()
    settings, third_at, _, before = await carried_to_an_expired_assessment(sessions, now, model)

    both = await asyncio.gather(
        run(sessions, settings, third_at, ports=ports_at(third_at, model)),
        run(sessions, settings, third_at, ports=ports_at(third_at, model)),
        return_exceptions=True,
    )

    for item in both:
        assert not isinstance(item, BaseException), item
    outcomes = [
        (entry.origin, entry.outcome)
        for item in both
        for progress in item.cases
        for entry in progress.refreshes
    ]
    anchor = [outcome for origin, outcome in outcomes if origin == ANCHOR_SOURCE]
    assert anchor.count("ORDERED") <= 1, outcomes
    after = await observer_attempts(sessions)
    assert after[AgentRole.ANCHOR] - before[AgentRole.ANCHOR] == 1, (before, after)
    assert len(await executions(sessions)) <= 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) <= 1
