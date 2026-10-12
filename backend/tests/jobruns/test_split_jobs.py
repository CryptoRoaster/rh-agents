"""Split operation: the entry job buys and never sells, the exit job sells and never buys.

Each run is the production `BoundedPaperRun` over the production stack, and
every run of every mode leaves its `paper_job_runs` row. What is proven:

- `--entries-once` runs no exit sweep, and starts nothing while the exit job's
  evidence is missing, stale, failed or unreadable;
- `--exits-once` records whether it demonstrably worked — and a run that sold
  with a whole policy class unwatched, without its markets, out of time or
  cut off in the middle does not count as working;
- `--once` keeps doing what it did, and is recorded too;
- a run that never finishes reads as one, never as evidence.
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from src.data.tables import ExecutionRow, TradeCaseExitRow, TradeCaseRow
from src.orchestration.jobruns import service as jobruns_service
from src.orchestration.jobruns.models import PolicyClass
from src.orchestration.jobruns.service import JobRunRecorder, exit_liveness
from src.runner import service as runner_service
from src.runner.locks import RunJob, job_lock
from src.runner.models import ConfigurationRefused, RunMode, RunStop
from src.runner.service import BoundedPaperRun, Deadline
from tests.entryhalt.conftest import halt
from tests.jobruns.conftest import GATE, exit_run, job_runs
from tests.runner.provider import MarketProvider
from tests.runner.test_exit_phase import Reads, count, falling, stack_with
from tests.runner.test_position_refresh import portfolio

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL advisory locks required"
)
SPLIT = {"paper_entry_requires_exit_job": True}


async def evidence(sessions, at, policy=None):
    async with sessions() as session:
        return await exit_liveness(session, GATE, at=at, policy=policy)


# ------------------------------------------------------------ the entry job


async def test_the_entry_job_refuses_to_start_without_the_gate(risk_db, now):
    _, sessions = risk_db
    stack = stack_with(sessions, now, MarketProvider(), early_reads=Reads(None))

    refused = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert isinstance(refused, ConfigurationRefused)
    assert refused.reason == "EXIT_JOB_GATE_NOT_ENABLED"
    assert await job_runs(sessions) == []


async def test_the_entry_job_without_exit_evidence_asks_nobody_and_says_why(risk_db, now):
    _, sessions = risk_db
    provider = MarketProvider()
    stack = stack_with(sessions, now, provider, early_reads=Reads(None), **SPLIT)

    summary = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert summary.stop is RunStop.EXIT_JOB_STALE
    assert summary.exit_liveness == "EXIT_JOB_MISSING"
    assert summary.exits_delegated is True
    assert summary.acquisition is None
    assert provider.requests == []
    assert (summary.candidates_seen, summary.cases_opened, summary.steps_taken) == (0, 0, 0)
    assert summary.exit_code == 0
    [row] = await job_runs(sessions)
    assert (row.job, row.mode, row.outcome, row.stop_code, row.exit_ready) == (
        "PAPER_ENTRY_JOB",
        "ENTRIES_ONLY",
        "REFUSED",
        "EXIT_JOB_STALE",
        False,
    )
    assert row.exit_phase_status == "DELEGATED_TO_EXIT_JOB"


@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        ({"ago": 600}, "EXIT_JOB_TOO_OLD"),
        ({"ago": 10, "ready": False}, "EXIT_JOB_UNHEALTHY"),
    ],
)
async def test_the_entry_job_on_stale_or_failed_evidence_starts_nothing(
    risk_db, now, setup, detail
):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=setup["ago"]), ready=setup.get("ready", True))
    stack = stack_with(sessions, now, MarketProvider(), early_reads=Reads(None), **SPLIT)

    summary = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert (summary.stop, summary.exit_liveness) == (RunStop.EXIT_JOB_STALE, detail)


async def test_an_unreadable_job_record_fails_closed(risk_db, now, monkeypatch):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=10))

    async def broken(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("unreadable"))

    monkeypatch.setattr(runner_service, "exit_liveness", broken)
    stack = stack_with(sessions, now, MarketProvider(), early_reads=Reads(None), **SPLIT)

    summary = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert (summary.stop, summary.exit_liveness) == (RunStop.EXIT_JOB_STALE, "EXIT_JOB_UNREADABLE")
    assert summary.steps_taken == 0


async def test_the_entry_job_never_sells_even_a_triggered_position(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    await exit_run(sessions, at - timedelta(seconds=30))
    reads = Reads(None)
    stack = stack_with(sessions, at, falling(held), early_reads=reads, **SPLIT)

    summary = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert summary.mode is RunMode.ENTRIES_ONLY
    assert summary.exit_liveness == "READY"
    assert summary.exits_delegated is True
    assert (summary.exits, summary.early_exits, summary.exit_phase) == (None, None, None)
    # No exit sweep: no chain read was asked for and nothing was sold.
    assert reads.calls == 0
    assert await count(sessions, TradeCaseExitRow) == 0
    assert summary.stop is not RunStop.EXIT_JOB_STALE


async def test_an_entry_halt_still_stops_the_entry_job(risk_db, now):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    await halt(sessions, "manual", now)
    stack = stack_with(sessions, now, MarketProvider(), early_reads=Reads(None), **SPLIT)

    summary = await BoundedPaperRun(stack, mode=RunMode.ENTRIES_ONLY).execute()

    assert summary.stop is RunStop.ENTRY_HALTED
    assert summary.steps_taken == 0


# ------------------------------------------------------------- the exit job


async def test_a_working_exit_run_is_recorded_as_evidence_for_its_own_classes(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    fills_before = await count(sessions, ExecutionRow)
    cases_before = await count(sessions, TradeCaseRow)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.early_exits.executed == 1, summary.early_exits
    assert (summary.exit_ready, summary.exit_ready_reason) == (True, None)
    # The sale only: no BUY and no case.
    assert await count(sessions, ExecutionRow) == fills_before + 1
    assert await count(sessions, TradeCaseRow) == cases_before
    [row] = await job_runs(sessions)
    assert (row.job, row.mode, row.outcome, row.exit_ready, row.exit_ready_reason) == (
        "PAPER_EXIT_JOB",
        "EXITS_ONLY",
        "SUCCEEDED",
        True,
        None,
    )
    assert row.policy_classes == ["EARLY"]
    assert row.metrics["exit_executed"] == 1
    assert row.metrics["uncovered_positions"] == 0
    # The early class is covered; the normal one is not configured here.
    assert await evidence(sessions, at + timedelta(seconds=5), PolicyClass.EARLY) is None
    assert await evidence(sessions, at + timedelta(seconds=5), PolicyClass.NORMAL) == (
        "EXIT_POLICY_NOT_COVERED"
    )


async def test_an_exit_run_with_nothing_open_is_evidence(risk_db, now):
    _, sessions = risk_db

    summary = await BoundedPaperRun(
        stack_with(sessions, now, MarketProvider(), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
    ).execute()

    assert summary.exit_ready is True, (summary.exit_ready_reason, summary.acquisition)


async def test_an_open_position_no_configured_policy_closes_is_not_evidence(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1, normal=1)
    at = now + timedelta(minutes=10)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)), mode=RunMode.EXITS_ONLY
    ).execute()

    assert (summary.exit_ready, summary.exit_ready_reason) == (
        False,
        "EXIT_POLICY_COVERAGE_MISSING",
    )
    [row] = await job_runs(sessions)
    assert (row.outcome, row.metrics["uncovered_positions"]) == ("EXIT_COVERAGE_MISSING", 1)
    assert await evidence(sessions, at, PolicyClass.EARLY) == "EXIT_JOB_UNHEALTHY"


async def test_an_exit_run_that_could_not_observe_its_markets_is_not_evidence(risk_db, now):
    _, sessions = risk_db
    await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    # The provider answers nothing for the held market.
    summary = await BoundedPaperRun(
        stack_with(sessions, at, MarketProvider(targeted=[]), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
    ).execute()

    assert summary.exit_ready is False
    assert summary.exit_ready_reason == "EXIT_ACQUISITION_INCOMPLETE", summary.acquisition


async def test_an_exit_run_out_of_time_is_not_evidence(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
        deadline=Deadline(0),
    ).execute()

    assert summary.stop is RunStop.TIME_BUDGET_REACHED
    assert (summary.exit_ready, summary.exit_ready_reason) == (False, "EXIT_JOB_TIMED_OUT")
    [row] = await job_runs(sessions)
    assert row.outcome == "TIMED_OUT"


async def test_an_exit_attempt_that_could_not_be_made_is_not_evidence(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None, fail=True)),
        mode=RunMode.EXITS_ONLY,
    ).execute()

    assert summary.early_exits.executed == 0
    assert summary.early_exits.refusals == ("EXIT_READ_UNAVAILABLE",)
    assert (summary.exit_ready, summary.exit_ready_reason) == (False, "EXIT_BLOCKED")


async def test_a_run_that_never_finished_reads_as_one(risk_db, now, monkeypatch):
    """The finish could not be written: the row stays RUNNING, and later reads as a crash."""
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=40))

    async def lost(self, run_id, finish):
        raise OperationalError("UPDATE", {}, Exception("connection lost"))

    monkeypatch.setattr(JobRunRecorder, "finish", lost)

    summary = await BoundedPaperRun(
        stack_with(sessions, now, MarketProvider(), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
    ).execute()

    assert "JOB_RUN_UNRECORDED" in summary.errors
    assert summary.exit_code == 1
    rows = await job_runs(sessions)
    assert rows[-1].outcome == "RUNNING"
    # While it could still be running, the run before it stands …
    assert await evidence(sessions, now + timedelta(seconds=10)) is None
    # … and once it is past its bound, nothing does.
    assert await evidence(sessions, now + timedelta(seconds=120)) == "EXIT_JOB_INCOMPLETE"


async def test_a_run_whose_start_could_not_be_recorded_still_sells(risk_db, now, monkeypatch):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)

    async def lost(self, *args):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(JobRunRecorder, "start", lost)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.early_exits.executed == 1
    assert "JOB_RUN_UNRECORDED" in summary.errors
    assert await job_runs(sessions) == []


# ---------------------------------------------------------------- --once


async def test_the_combined_run_still_sells_and_is_recorded(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None))
    ).execute()

    assert summary.mode is RunMode.FULL
    assert summary.early_exits.executed == 1
    assert summary.exits_delegated is False
    assert summary.exit_liveness is None
    [row] = await job_runs(sessions)
    assert (row.job, row.mode, row.exit_ready, row.exit_phase_status) == (
        "PAPER_ENTRY_JOB",
        "FULL",
        False,
        "COMPLETED",
    )
    # A combined run is never the exit job's evidence.
    assert await evidence(sessions, at) == "EXIT_JOB_MISSING"


# ------------------------------------------------------------- PostgreSQL


@POSTGRES
async def test_a_start_that_found_the_lock_held_is_recorded_and_proves_nothing(risk_db, now):
    _, sessions = risk_db
    stack = stack_with(sessions, now, MarketProvider(), early_reads=Reads(None))

    async with job_lock(sessions, RunJob.PAPER_EXIT_JOB):
        summary = await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute()

    assert summary.stop is RunStop.ALREADY_RUNNING
    assert summary.exit_ready is None
    [row] = await job_runs(sessions)
    assert (row.outcome, row.exit_ready, row.finished_at is not None) == (
        "ALREADY_RUNNING",
        False,
        True,
    )
    assert await evidence(sessions, now) == "EXIT_JOB_MISSING"


def test_the_recorder_is_the_one_the_service_uses():
    assert jobruns_service.JobRunRecorder is JobRunRecorder
