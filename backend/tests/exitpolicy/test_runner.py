"""The sweep inside a full PAPER run: composed only when configured, reported always."""

from src.orchestration.exitpolicy.service import AutoExitService
from src.runner.service import BoundedPaperRun
from tests.runner.conftest import record_market, runner_settings, stack_for

FIXTURE_POLICY = {
    "paper_auto_exit_enabled": True,
    "paper_exit_stop_loss_bps": 2000,
    "paper_exit_take_profit_bps": 5000,
    "paper_exit_max_holding_minutes": 360,
}


async def test_disabled_composes_no_sweep_and_reports_none(risk_db, now):
    _, sessions = risk_db
    await record_market(sessions, now)
    stack = stack_for(sessions, runner_settings(), now)
    assert stack.exits is None
    summary = await BoundedPaperRun(stack).execute()
    assert summary.exits is None


async def test_enabled_sweeps_before_intake_and_reports_counts(risk_db, now):
    _, sessions = risk_db
    await record_market(sessions, now)
    stack = stack_for(sessions, runner_settings(**FIXTURE_POLICY), now)
    assert isinstance(stack.exits, AutoExitService)
    assert stack.exits.policy.max_holding_seconds == 360 * 60
    summary = await BoundedPaperRun(stack).execute()
    assert summary.exits is not None
    assert (summary.exits.evaluated, summary.exits.executed) == (0, 0)
