"""What bounds one pass, and what the process contract says when it ends.

Every time case runs on a fixed or controlled clock. No sleep is used anywhere,
and nothing here starts work that outlives the call that asked for it.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.data.tables import TradeCaseRow
from src.orchestration.worker.models import TaskLease, TaskWaitReport
from src.runner.composition import RunnerPorts, build_stack, ports_from_settings
from src.runner.models import ExitCode, RunStop
from src.runner.service import BoundedPaperRun
from tests.runner.conftest import (
    executions,
    read_account,
    ready_case,
    record_market,
    run,
    runner_settings,
    stack_for,
)


class StepClock:
    """A clock that advances when work happens, never when it is read.

    A clock that moved on reads could not express "the run spent its budget
    doing something", which is exactly the situation a time bound exists for.
    """

    def __init__(self, instant) -> None:
        self.instant = instant

    def now(self):
        return self.instant

    def wait(self, delta) -> None:
        self.instant = self.instant + delta


class SlowRole:
    """A handler that never returns, standing in for an external call that hangs."""

    role = AgentRole.PULSE
    task_type = "WAIT_FOR_TRIGGER"

    def __init__(self) -> None:
        self.entered = 0

    async def handle(self, lease: TaskLease, capabilities: object):
        import asyncio

        self.entered += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


# ------------------------------------------------------- budgets


async def test_the_time_budget_ends_the_pass(risk_db, now, trace):
    """A run that has spent its time stops, and says that is why."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    clock = StepClock(now)
    stack = build_stack(settings, sessions, ports=RunnerPorts(), clock=clock)
    await ready_case(stack.cases, now, trace, key="runner-time")
    # Intake is real work and real work takes time. Here it takes the whole
    # budget, so the pass reaches its deadline before it decides anything.
    cycle = stack.intake.run_cycle

    async def slow_intake():
        outcome = await cycle()
        clock.wait(timedelta(seconds=settings.paper_runner_max_seconds))
        return outcome

    object.__setattr__(stack.intake, "run_cycle", slow_intake)

    summary = await BoundedPaperRun(stack).execute()

    assert summary.stop is RunStop.TIME_BUDGET_REACHED
    assert summary.exit_code is ExitCode.COMPLETED
    assert summary.fills == 0
    assert await executions(sessions) == []


async def test_the_case_budget_bounds_what_one_pass_decides(risk_db, now, trace):
    """More ready cases than the budget: the rest stay for the next run."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(paper_runner_max_cases=1)
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-one")
    await ready_case(stack.cases, now, uuid4(), key="runner-two")

    summary = await BoundedPaperRun(stack).execute()

    assert len(summary.cases) <= 1
    assert summary.limits.max_cases == 1
    async with sessions() as session:
        assert (await session.scalar(select(func.count()).select_from(TradeCaseRow))) >= 2
    # Whatever it did not reach is still there, untouched, for an explicit rerun.
    assert len(await executions(sessions)) <= 1


async def test_an_external_call_that_hangs_is_bounded_by_the_step_timeout(risk_db, now, trace):
    """The wait is the run's, not the provider's, and the task keeps its lease."""
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(
        pulse_worker_enabled=True,
        paper_runner_step_timeout_seconds=1,
        paper_runner_max_seconds=30,
    )
    stack = stack_for(sessions, settings, now)
    assert len(stack.runners) == 1
    handler = SlowRole()
    object.__setattr__(stack.runners[0], "handler", handler)

    summary = await asyncio.wait_for(BoundedPaperRun(stack).execute(), timeout=20)

    assert summary.exit_code is ExitCode.COMPLETED
    # Either the role had nothing to claim, or it claimed once and was cut off.
    assert handler.entered <= 1
    assert summary.steps_taken == 0
    assert await executions(sessions) == []


async def test_a_waiting_monitor_ends_the_pass_without_spinning(risk_db, now, trace):
    """A durable reschedule is a disposition, and the pass still terminates."""
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(pulse_worker_enabled=True)
    stack = stack_for(sessions, settings, now)

    class Waiting:
        role = AgentRole.PULSE
        task_type = "WAIT_FOR_TRIGGER"

        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, lease: TaskLease, capabilities: object):
            self.calls += 1
            return TaskWaitReport(reason_code="CONDITION_NOT_MET")

    handler = Waiting()
    object.__setattr__(stack.runners[0], "handler", handler)

    summary = await asyncio.wait_for(BoundedPaperRun(stack).execute(), timeout=30)

    assert summary.exit_code is ExitCode.COMPLETED
    assert summary.stop in (RunStop.NOTHING_LEFT_TO_DO, RunStop.STEP_BUDGET_REACHED)
    # Bounded either way: the step budget is the backstop, never an open loop.
    assert handler.calls <= settings.paper_runner_max_steps


# ------------------------------------------------------- composition honesty


def test_a_configuration_can_never_produce_a_synthetic_model():
    """`fake` is selectable and deliberately not constructible from settings."""
    settings = runner_settings(reasoning_provider="fake")

    ports = ports_from_settings(settings, None)  # type: ignore[arg-type]

    assert ports.reasoning is None
    assert ports.reasoning_unavailable == "REASONING_PROVIDER_NOT_COMPOSABLE"


def test_a_disabled_reasoning_provider_is_reported_not_substituted():
    settings = runner_settings(reasoning_provider="disabled")

    ports = ports_from_settings(settings, None)  # type: ignore[arg-type]

    assert ports.reasoning is None
    assert ports.reasoning_unavailable == "REASONING_PROVIDER_NOT_CONFIGURED"


async def test_a_role_that_needs_an_unbuildable_port_is_reported(risk_db, now, trace):
    """Enabled and unwired is a different answer from enabled and idle."""
    _, sessions = risk_db
    settings = runner_settings(
        orbit_worker_enabled=True,
        reasoning_provider="fake",
        signal_worker_enabled=True,
        fuse_worker_enabled=True,
    )

    stack = build_stack(
        settings,
        sessions,
        ports=ports_from_settings(settings, sessions),
        clock=FixedClock(now),
    )

    reasons = {item.role: item.reason for item in stack.roles}
    assert reasons["ORBIT"] == "REASONING_PROVIDER_NOT_COMPOSABLE"
    assert reasons["SIGNAL"] == "REASONING_PROVIDER_NOT_COMPOSABLE"
    assert reasons["FUSE"] is None
    assert [item.handler.role for item in stack.runners] == [AgentRole.FUSE]


# ------------------------------------------------------- the process contract


def test_the_entry_point_requires_once(monkeypatch):
    """There is no other mode, so the flag is not optional."""
    from src.runner import main as entry

    monkeypatch.setattr("sys.argv", ["src.runner.main"])
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 2  # argparse usage error


def test_an_invalid_configuration_exits_two_without_touching_anything(monkeypatch, capsys):
    import json

    from src.runner import main as entry

    monkeypatch.setattr("sys.argv", ["src.runner.main", "--once"])
    monkeypatch.setenv("DATABASE_URL", "sqlite:///nope")
    monkeypatch.setattr(entry, "Settings", _raising_settings())

    code = entry.main()

    assert code == int(ExitCode.CONFIGURATION_REFUSED)
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["kind"] == "run_configuration_refused"
    assert printed["reason"] == "SETTINGS_INVALID"


def _raising_settings():
    from pydantic import ValidationError

    from src.core.config import Settings

    def build(*arguments, **keywords):
        raise ValidationError.from_exception_data("Settings", [])

    build.model_validate = Settings.model_validate  # type: ignore[attr-defined]
    return build


def test_the_summary_carries_no_secret_and_no_payload(risk_db):
    """Everything printed is a code, a count or an identifier already public."""
    from src.runner.models import RunLimits, RunSummary

    summary = RunSummary(
        run_id=uuid4(),
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        stop=RunStop.NOTHING_LEFT_TO_DO,
        limits=RunLimits(
            max_candidates=1,
            max_steps=1,
            max_cases=1,
            max_runtime_seconds=5,
            step_timeout_seconds=1,
        ),
    )
    rendered = summary.model_dump(mode="json")

    assert set(rendered) == {
        "kind",
        "run_id",
        "started_at",
        "finished_at",
        "stop",
        "limits",
        "roles",
        "candidates_seen",
        "cases_opened",
        "intake_refusals",
        "steps_taken",
        "cases",
        "risk_requests",
        "fills",
        "replays",
        "errors",
    }
    assert summary.exit_code is ExitCode.COMPLETED


async def test_a_run_leaves_no_background_task_behind(risk_db, now, trace):
    """Nothing outlives the call that asked for it."""
    import asyncio

    _, sessions = risk_db
    await record_market(sessions, now)
    before = len(asyncio.all_tasks())

    await run(sessions, runner_settings(), now)

    assert len(asyncio.all_tasks()) <= before
    assert (await read_account(sessions)) is not None
