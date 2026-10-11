"""The bounded run under an entry-only halt: the exits run, no entry stage does."""

from datetime import timedelta

from src.runner.models import RunMode, RunStop
from src.runner.service import BoundedPaperRun
from tests.entryhalt.conftest import halt
from tests.runner.conftest import runner_settings, stack_for
from tests.runner.test_exit_phase import Reads, falling, stack_with
from tests.runner.test_position_refresh import portfolio


async def test_a_halted_entry_run_stops_after_its_exits_and_says_why(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "manual", now)

    summary = await BoundedPaperRun(stack_for(sessions, runner_settings(), now)).execute()

    assert summary.stop is RunStop.ENTRY_HALTED
    assert summary.entry_halt == "MANUAL"
    assert (summary.candidates_seen, summary.cases_opened, summary.steps_taken) == (0, 0, 0)
    assert summary.exit_code == 0


async def test_an_unhalted_run_reports_no_halt(risk_db, now):
    _, sessions = risk_db

    summary = await BoundedPaperRun(stack_for(sessions, runner_settings(), now)).execute()

    assert summary.entry_halt is None
    assert summary.stop is not RunStop.ENTRY_HALTED


async def test_the_exit_job_sells_under_a_halt_and_reports_it(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    await halt(sessions, "auto", at)

    summary = await BoundedPaperRun(
        stack_with(sessions, at, falling(held), early_reads=Reads(None)), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.early_exits.executed == 1, summary.early_exits
    assert summary.entry_halt == "AUTO"
    assert summary.stop is not RunStop.ENTRY_HALTED
