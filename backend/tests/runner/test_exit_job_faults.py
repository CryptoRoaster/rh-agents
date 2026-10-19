"""The exit job (`--exits-once`) end to end under a database fault and an unreadable stop.

Issue #111 isolates a failure nobody named to its own position. These tests pin
what that isolation must leave as it was, at the level of the whole exit run:
an unreadable stop still ends the run before any further sale, a database that
cannot be read is a technical failure, and a database error inside one
position's sale costs that position only. Every case leaves the run not ready
-- the evidence that alarms and holds every new entry (`EXIT_JOB_STALE`).
"""

from datetime import timedelta

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.commander.context import SystemPauseUnavailable
from src.runner.models import RunMode, RunStop
from src.runner.service import BoundedPaperRun
from tests.runner.test_exit_phase import Reads, exit_rows, falling, stack_with
from tests.runner.test_position_refresh import portfolio


class Raising(Reads):
    """The fixture chain read; the first call raises `error` instead of answering."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(None)
        self.error = error

    async def read(self, trade_case_id, market, request_key):
        self.calls += 1
        if self.calls == 1:
            raise self.error
        return await self.inner.read(trade_case_id, market, request_key)


def unreachable():
    """Sessions bound to a database that cannot be opened: every read raises."""
    engine = create_async_engine("sqlite+aiosqlite:////nonexistent-rh-agents-test/db.sqlite")
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_an_unreadable_stop_mid_sweep_ends_the_exit_job(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=2)
    at = now + timedelta(minutes=10)
    reads = Raising(SystemPauseUnavailable("PAUSE_STATE_UNAVAILABLE"))

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=reads), mode=RunMode.EXITS_ONLY
    ).execute()

    # Fail closed: the run stops at once, sells nothing more, and is not evidence.
    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert "SYSTEM_STOP_UNREADABLE" in summary.errors
    assert reads.calls == 1
    assert len(await exit_rows(sessions)) == 0
    assert summary.exit_ready is False


async def test_an_unreadable_database_is_a_technical_failure_of_the_exit_job(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=2)
    at = now + timedelta(minutes=10)
    reads = Reads(None)
    # The whole exit job -- stack, sweeps and job record -- on a database it cannot open.
    stack = stack_with(unreachable(), at, falling(held), early_reads=reads)

    summary = await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute()

    # A technical failure, not evidence that exits work -- and no job record
    # could be written either, so the entry gate sees no fresh ready run.
    assert summary.errors == ("DATABASE_UNAVAILABLE", "JOB_RUN_UNRECORDED")
    assert summary.exit_ready is False
    assert summary.exit_ready_reason == "EXIT_JOB_TECHNICAL_FAILURE"
    # The chain was never read, so nothing was sold: a sale needs that read.
    assert reads.calls == 0


async def test_a_database_error_in_one_sale_costs_that_position_only(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=2)
    at = now + timedelta(minutes=10)
    reads = Raising(OperationalError("SELECT 1", {}, Exception("connection reset")))

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=reads), mode=RunMode.EXITS_ONLY
    ).execute()

    report = summary.early_exits
    assert report is not None
    assert (report.attempts, report.executed, report.failed) == (2, 1, 1)
    assert report.refusals == ("EXIT_ATTEMPT_FAILED",)
    assert len(await exit_rows(sessions)) == 1
    # Not ready, by name: that alarms and holds entries until a clean run.
    assert summary.exit_ready is False
    assert summary.exit_ready_reason == "EXIT_ATTEMPT_FAILED"

    later = at + timedelta(minutes=1)
    again = await BoundedPaperRun(
        stack_with(sessions, later, falling(held), early_reads=Reads(None)),
        mode=RunMode.EXITS_ONLY,
    ).execute()
    assert again.early_exits is not None and again.early_exits.executed == 1
    assert len(await exit_rows(sessions)) == 2
