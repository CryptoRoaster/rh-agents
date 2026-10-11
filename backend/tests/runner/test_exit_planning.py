"""Planning in the exit phase: one policy's slow judgement never costs the other's.

Each policy first judges its positions from what is recorded (`plan`). A plan
that hangs — a database read that never answers — must be cut off on its own
share of the run, keep everything it already counted and every candidate it
already named, and leave the other policy its own chance to be judged. The
hang is an event nobody sets: deterministic, and no provider is involved in it.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from src.runner.models import RunMode, RunStop
from src.runner.service import BoundedPaperRun, Deadline
from tests.runner.test_exit_phase import Reads, count, falling, limits, stack_with
from tests.runner.test_position_refresh import portfolio


@dataclass
class Hanging:
    """A policy whose judgement hangs — before it starts, or after it has finished."""

    inner: Any
    after: bool = False

    async def plan(self, result, **kwargs):
        if not self.after:
            await asyncio.Event().wait()
        planned = await self.inner.plan(result, **kwargs)
        await asyncio.Event().wait()
        return planned  # pragma: no cover - never reached


async def held_both(sessions, now):
    return await portfolio(sessions, now, early=1, normal=1)


async def test_a_hanging_normal_plan_leaves_the_early_policy_its_judgement_and_sale(
    risk_db, now, monkeypatch
):
    _, sessions = risk_db
    held = await held_both(sessions, now)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.0, settle_seconds=0.3)
    reads = Reads(None)
    stack = stack_with(sessions, at, falling(held), normal_reads=Reads(None), early_reads=reads)
    stack = replace(stack, exits=Hanging(stack.exits))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(8.0)
    ).execute()

    assert summary.early_exits.evaluated == 1, summary.early_exits
    assert summary.early_exits.executed == 1, summary.early_exits
    assert summary.exits.plan_stop == "EXIT_PLAN_TIMED_OUT", summary.exits
    assert summary.early_exits.plan_stop is None


async def test_a_plan_cut_off_after_judging_keeps_its_counts_and_its_candidates(
    risk_db, now, monkeypatch
):
    _, sessions = risk_db
    held = await held_both(sessions, now)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.0, settle_seconds=0.3)
    stack = stack_with(
        sessions, at, falling(held), normal_reads=Reads(None), early_reads=Reads(None)
    )
    stack = replace(stack, exits=Hanging(stack.exits, after=True))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(10.0)
    ).execute()

    normal = summary.exits
    assert (normal.evaluated, normal.triggered) == (1, 1), normal
    # The candidate it named before the cut is still asked for.
    assert normal.executed == 1, normal
    assert normal.plan_stop == "EXIT_PLAN_TIMED_OUT"
    assert summary.early_exits.executed == 1, summary.early_exits


async def test_a_hanging_early_plan_keeps_the_normal_results_and_sales(risk_db, now, monkeypatch):
    _, sessions = risk_db
    held = await held_both(sessions, now)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.0, settle_seconds=0.3)
    stack = stack_with(
        sessions, at, falling(held), normal_reads=Reads(None), early_reads=Reads(None)
    )
    stack = replace(stack, early_exits=Hanging(stack.early_exits))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(8.0)
    ).execute()

    assert (summary.exits.evaluated, summary.exits.executed) == (1, 1), summary.exits
    assert summary.early_exits.plan_stop == "EXIT_PLAN_TIMED_OUT"
    assert summary.stop is RunStop.TIME_BUDGET_REACHED
    assert await count_exits(sessions) == 1


async def test_a_spent_deadline_starts_no_chain_read_and_no_sale(risk_db, now, monkeypatch):
    _, sessions = risk_db
    held = await held_both(sessions, now)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.0, settle_seconds=0.3)
    normal, early = Reads(None), Reads(None)
    stack = stack_with(sessions, at, falling(held), normal_reads=normal, early_reads=early)
    # Judged, then hung until the whole run is gone: nothing may be asked after.
    stack = replace(stack, exits=Hanging(stack.exits, after=True))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(1.5)
    ).execute()

    assert normal.calls + early.calls == 0
    assert await count_exits(sessions) == 0
    assert summary.exits.triggered == 1
    assert summary.exits.plan_stop == "EXIT_PLAN_TIMED_OUT"
    assert "EXIT_DEFERRED_RUN_BUDGET" in summary.exits.deferred_reasons
    assert summary.stop is RunStop.TIME_BUDGET_REACHED


async def count_exits(sessions):
    from src.data.tables import TradeCaseExitRow

    return await count(sessions, TradeCaseExitRow)
