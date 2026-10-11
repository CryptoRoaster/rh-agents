"""The exit phase of a run: one budget for every exit policy, and its edges.

Each test drives the production `BoundedPaperRun` over the production stack,
with the fixture chain read wrapped so a test can make it slow, failing or
hanging, and count how often it was asked. What is proven, per defect this
phase was built against:

- a slow normal sweep can no longer keep the early policy from being asked;
- a run that runs out of time keeps every count it already made;
- no attempt starts without time for a whole one, and saying so is a deferral;
- refusals cannot turn into an unbounded number of chain reads;
- one hanging chain read costs one attempt, not the run;
- a known stop is honoured before any chain read is paid for;
- the exit job's acquisition cannot spend the time the sales need.
"""

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from src.data.tables import AccountRow, ExecutionRow, TradeCaseExitRow, TradeCaseRow
from src.orchestration.paperexit.exitread import ExitReadUnavailable
from src.runner import service as runner_service
from src.runner.models import RunMode, RunStop
from src.runner.service import BoundedPaperRun, Deadline
from tests.runner.provider import MarketProvider
from tests.runner.test_position_refresh import NORMAL, answer, portfolio
from tests.runner.test_pre_exit_refresh import refreshing_stack

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


@dataclass
class Reads:
    """The fixture chain read, wrapped: optionally slow, failing or hanging; counted."""

    inner: Any
    delay: float = 0.0
    fail: bool = False
    # How many of the first calls never answer.
    hang: int = 0
    calls: int = 0

    async def read(self, trade_case_id, market, request_key):
        self.calls += 1
        if self.calls <= self.hang:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ExitReadUnavailable("ONCHAIN_RPC_TIMEOUT")
        return await self.inner.read(trade_case_id, market, request_key)


def wrap(stage, reads: Reads):
    """The same exit policy, its one exit path reading the chain through `reads`."""
    if stage is None:
        return None
    reads.inner = stage.exits.exit_read
    return replace(stage, exits=replace(stage.exits, exit_read=reads))


def stack_with(sessions, at, provider, *, normal_reads=None, early_reads=None, **overrides):
    stack = refreshing_stack(sessions, at, provider, normal=normal_reads is not None, **overrides)
    if normal_reads is not None:
        stack = replace(stack, exits=wrap(stack.exits, normal_reads))
    if early_reads is not None:
        stack = replace(stack, early_exits=wrap(stack.early_exits, early_reads))
    return stack


def limits(monkeypatch, **values):
    """This run's exit phase bounds, small enough for a test to reach in real time."""
    from src.orchestration.exitpolicy.drive import ExitLimits

    bounds = {
        "max_attempts": 5,
        "max_exits": 5,
        "max_refreshes": 5,
        "attempt_seconds": 1.0,
        "settle_seconds": 0.2,
        **values,
    }
    monkeypatch.setattr(
        runner_service,
        "exit_limits_from_settings",
        lambda settings: ExitLimits(**bounds),
        raising=False,
    )


async def count(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def exit_rows(sessions):
    async with sessions() as session:
        return (await session.scalars(select(TradeCaseExitRow))).all()


def falling(held):
    """Every early holding at its stop; every normal one past its target.

    Normal sales are taken at a profit on purpose: a loss on each of four
    larger normal holdings would reach SENTINEL's daily loss limit, whose
    `PAUSE_SYSTEM` (unchanged here) then stops every later sale in the run.
    """
    return MarketProvider(
        targeted=[answer(item, price="2.00" if item in NORMAL else "0.40") for item in held]
    )


# --------------------------------------------- the early policy is not starved


async def test_a_slow_normal_sweep_no_longer_keeps_the_early_policy_from_being_asked(
    risk_db, now, monkeypatch
):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1, normal=4)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.5, settle_seconds=0.3)
    # Every normal sale reads the chain slowly (1.5 s); four of them outlast the
    # run, so a sweep-after-sweep order would never reach the early position.
    stack = stack_with(sessions, at, falling(held), normal_reads=Reads(None, delay=1.5))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(6.0)
    ).execute()

    assert summary.early_exits is not None, summary
    assert summary.early_exits.executed == 1, summary.early_exits
    assert summary.exits is not None and summary.exits.executed >= 1, summary.exits


async def test_a_run_that_runs_out_of_time_keeps_what_it_already_counted(risk_db, now, monkeypatch):
    _, sessions = risk_db
    held = await portfolio(sessions, now, normal=4)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.5, settle_seconds=0.3)
    stack = stack_with(sessions, at, falling(held), normal_reads=Reads(None, delay=1.0))

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(4.0)
    ).execute()

    report = summary.exits
    assert report is not None, summary
    booked = len(await exit_rows(sessions))
    assert report.executed == booked >= 1, report
    # Every triggered position is accounted for: sold, refused, unknown or deferred.
    assert report.triggered == 4
    assert report.executed + report.unknown + report.deferred + len(report.refusals) >= 4
    assert summary.stop is RunStop.TIME_BUDGET_REACHED


# ------------------------------------------------------------------ admission


async def test_no_attempt_starts_without_time_for_a_whole_one(risk_db, now, monkeypatch):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=2)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=5.0, settle_seconds=1.0)
    reads = Reads(None)
    stack = stack_with(sessions, at, falling(held), early_reads=reads)

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(3.0)
    ).execute()

    assert reads.calls == 0
    assert await count(sessions, TradeCaseExitRow) == 0
    report = summary.early_exits
    assert report.triggered == 2, report
    assert (report.attempts, report.deferred) == (0, 2), report
    assert report.deferred_reasons == ("EXIT_DEFERRED_RUN_BUDGET",)
    assert summary.exit_phase.deferred_reasons == ("EXIT_DEFERRED_RUN_BUDGET",)
    assert summary.stop is RunStop.TIME_BUDGET_REACHED


async def test_refusals_cannot_become_unbounded_chain_reads(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=5, normal=4)
    at = now + timedelta(minutes=10)
    normal, early = Reads(None, fail=True), Reads(None, fail=True)
    stack = stack_with(
        sessions,
        at,
        falling(held),
        normal_reads=normal,
        early_reads=early,
        paper_exit_max_per_run=1,
    )

    summary = await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute()

    # Nine triggered positions, every read refused, one sale allowed: the
    # attempts are bounded by their own budget (five), shared by both policies.
    assert normal.calls + early.calls == 5, (normal.calls, early.calls)
    assert summary.exits.triggered + summary.early_exits.triggered == 9
    assert summary.exit_phase.attempts == 5
    assert summary.exit_phase.deferred_reasons == ("EXIT_ATTEMPT_LIMIT_REACHED",)
    assert await count(sessions, TradeCaseExitRow) == 0


async def test_both_policies_share_one_budget_and_each_is_asked(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=5, normal=4)
    at = now + timedelta(minutes=10)
    stack = stack_with(
        sessions, at, falling(held), normal_reads=Reads(None), early_reads=Reads(None)
    )
    fills_before = await count(sessions, ExecutionRow)
    cases_before = await count(sessions, TradeCaseRow)

    summary = await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute()

    phase = summary.exit_phase
    assert (phase.max_exits, phase.exits, phase.attempts) == (5, 5, 5), phase
    assert summary.exits.executed + summary.early_exits.executed == 5
    # Interleaved: neither policy can have taken the whole budget.
    assert summary.exits.attempts >= 2 and summary.early_exits.attempts >= 2
    assert phase.deferred == 4
    # The exit job sold and bought nothing.
    assert await count(sessions, TradeCaseExitRow) == 5
    assert await count(sessions, ExecutionRow) == fills_before + 5
    assert await count(sessions, TradeCaseRow) == cases_before
    assert (summary.risk_requests, summary.fills, summary.cases_opened) == (0, 0, 0)


# ------------------------------------------------- one attempt, cut off alone


async def test_a_hanging_chain_read_costs_one_attempt_and_the_next_position_is_asked(
    risk_db, now, monkeypatch
):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=2)
    at = now + timedelta(minutes=10)
    limits(monkeypatch, attempt_seconds=2.0, settle_seconds=0.3)
    reads = Reads(None, hang=1)
    stack = stack_with(sessions, at, falling(held), early_reads=reads)

    summary = await BoundedPaperRun(
        stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(8.0)
    ).execute()

    report = summary.early_exits
    assert reads.calls == 2
    assert (report.attempts, report.timed_out, report.unknown, report.executed) == (2, 1, 1, 1)
    # Not "failed": the attempt's booking is unknown, and said so by name.
    assert "EXIT_OUTCOME_UNKNOWN" in report.refusals
    assert len(await exit_rows(sessions)) == 1

    # The next run asks again and sells the other one, once.
    later = at + timedelta(minutes=1)
    again = await BoundedPaperRun(
        stack_with(sessions, later, falling(held), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
    ).execute()
    assert again.early_exits.executed == 1
    assert len(await exit_rows(sessions)) == 2


# ----------------------------------------------------- stops before the chain


async def test_a_paused_account_stops_the_exit_job_before_any_chain_read(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    async with sessions.begin() as session:
        await session.execute(update(AccountRow).values(paused=True))
    reads = Reads(None)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=reads), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert reads.calls == 0
    assert await count(sessions, TradeCaseExitRow) == 0


# ------------------------------------------------------------- acquisition


class Clocked:
    """An acquisition stage that only records how long it was given."""

    def __init__(self) -> None:
        self.given: float | None = None

    async def execute(self, deadline, **_: object):
        from src.runner.acquisition import disabled

        self.given = deadline.remaining
        return disabled()


async def test_the_exit_jobs_acquisition_cannot_spend_the_sales_time(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    stage = Clocked()
    stack = replace(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)), acquisition=stage
    )

    await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY, deadline=Deadline(60)).execute()

    assert stage.given is not None
    assert stage.given <= stack.settings.paper_exit_acquisition_max_seconds


# ---------------------------------------------------------------- PostgreSQL


@POSTGRES
async def test_racing_exit_runs_book_one_sale_and_count_it_once(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, normal=1)
    at = now + timedelta(minutes=10)

    def run(mode):
        stack = stack_with(sessions, at, falling(held), normal_reads=Reads(None))
        return BoundedPaperRun(stack, mode=mode).execute()

    # The entry job's and the exit job's exit phases at once, same minute, same key.
    results = await asyncio.gather(run(RunMode.FULL), run(RunMode.EXITS_ONLY))

    assert all(item.stop is not RunStop.ALREADY_RUNNING for item in results)
    assert await count(sessions, TradeCaseExitRow) == 1
    reports = [item.exits for item in results if item.exits is not None]
    assert sum(item.executed for item in reports) == 1, reports
