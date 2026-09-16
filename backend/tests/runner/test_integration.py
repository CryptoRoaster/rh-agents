"""One explicit pass, and everything it is allowed and not allowed to do.

The production path runs throughout: the real intake, the real workflow, the
real worker runtime, the real risk request, `src.risk.engine.evaluate`, the real
`PaperExecutor` and the real ledger, composed by the real `build_stack`.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import TradingMode
from src.data.tables import TradeCaseExitRow, TradeCycleRow
from src.orchestration.workflow.models import TradeCaseStatus
from src.runner.models import ExitCode, RunStop
from src.runner.service import order_key
from tests.runner.conftest import (
    cases_in,
    executions,
    read_account,
    ready_case,
    record_market,
    run,
    runner_settings,
    set_account,
    stack_for,
)

# ------------------------------------------------------- the pass itself


async def test_a_run_opens_a_case_from_a_recorded_candidate_and_stops(risk_db, now, trace):
    """Intake runs, nothing else can, and the pass ends instead of waiting."""
    _, sessions = risk_db
    await record_market(sessions, now)

    summary = await run(sessions, runner_settings(), now)

    assert summary.kind == "paper_run_summary"
    assert summary.exit_code is ExitCode.COMPLETED
    assert summary.cases_opened == 1
    # One step: the intake cycle. No role could claim anything, and an empty
    # claim is not work.
    assert summary.steps_taken == 1
    assert summary.steps_timed_out == 0
    assert summary.stop is RunStop.NOTHING_LEFT_TO_DO
    # The case is waiting on evidence no configured role can produce, and says so
    # rather than being hurried along.
    assert len(summary.cases) == 1
    waiting = summary.cases[0]
    assert waiting.execution_id is None
    assert waiting.risk_outcome is None
    assert summary.waiting == (waiting,)
    assert TradeCaseStatus(waiting.status) not in (TradeCaseStatus.RISK_APPROVED,)
    # Every role is accounted for, none of them silently skipped.
    assert {item.role for item in summary.roles} == {
        "ORBIT",
        "ATLAS",
        "SIGNAL",
        "VECTOR",
        "PULSE",
        "ANCHOR",
        "FUSE",
    }
    assert all(item.reason == "ROLE_NOT_ENABLED" for item in summary.roles)


async def test_a_ready_case_goes_all_the_way_to_a_booked_fill(risk_db, now, trace):
    """The controlled full entry path, through the real services.

    The evidence is fixture evidence submitted through the real workflow — no
    specialist could produce it here, because none of them is configured and
    none of their ports exists in a test. Everything after that is production
    code: the canonical risk input, SENTINEL, the executor and the ledger.
    """
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-ready")
    before = await read_account(sessions)

    summary = await run(sessions, settings, now)

    assert summary.exit_code is ExitCode.COMPLETED
    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.risk_outcome == "APPROVE", progress
    assert progress.execution_id is not None
    assert progress.status == TradeCaseStatus.EXECUTED.value
    assert summary.risk_requests == 1
    assert summary.fills == 1

    # Real money moved, in the ledger, exactly once.
    after = await read_account(sessions)
    assert after.cash_usd < before.cash_usd
    assert after.fees_paid_usd > before.fees_paid_usd
    assert len(await executions(sessions)) == 1


async def test_the_order_key_comes_from_the_case_and_nothing_else(risk_db, now, trace):
    """A restart must address the same order, so the run id cannot be in it."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-key")

    first = await run(sessions, settings, now, run_id=uuid4())
    assert first.fills == 1

    from src.data.tables import TradeCaseRiskRequestRow

    async with sessions() as session:
        stored = await session.scalar(
            select(TradeCaseRiskRequestRow).where(TradeCaseRiskRequestRow.trade_case_id == case.id)
        )
    assert stored.request_key == order_key(case.id)
    assert str(first.run_id) not in stored.request_key


# ------------------------------------------------------- restart and replay


async def test_a_second_run_replays_rather_than_ordering_again(risk_db, now, trace):
    """Interruption and restart address one order, not two."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-restart")
    first = await run(sessions, settings, now, run_id=uuid4())
    assert first.fills == 1
    after_first = await read_account(sessions)

    second = await run(sessions, settings, now, run_id=uuid4())

    assert second.exit_code is ExitCode.COMPLETED
    # The case is terminal now, so it is not even a candidate for a second order.
    assert second.fills == 0
    assert second.risk_requests == 0
    assert len(await executions(sessions)) == 1
    again = await read_account(sessions)
    assert (again.cash_usd, again.fees_paid_usd) == (
        after_first.cash_usd,
        after_first.fees_paid_usd,
    )


# ------------------------------------------------------- stops


async def test_a_paused_account_stops_the_run_before_it_opens_anything(risk_db, now, trace):
    """The durable stop is honoured by the same services the run coordinates."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-paused")
    await set_account(sessions, paused=True)

    summary = await run(sessions, settings, now)

    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert summary.cases_opened == 0
    assert summary.fills == 0
    assert await executions(sessions) == []


async def test_the_kill_switch_refuses_the_run_before_any_mutation(risk_db, now, trace):
    """Configuration-level, so nothing is attempted and nothing is written."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(commander_kill_switch=True)

    refused = await run(sessions, settings, now)

    assert refused.kind == "run_configuration_refused"
    assert refused.reason == "KILL_SWITCH_ENGAGED"
    assert refused.exit_code is ExitCode.CONFIGURATION_REFUSED
    assert await cases_in(sessions) == []
    assert await executions(sessions) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"paper_runner_enabled": False}, "PAPER_RUNNER_NOT_ENABLED"),
        ({"commander_kill_switch": True}, "KILL_SWITCH_ENGAGED"),
    ],
)
async def test_a_configuration_that_forbids_a_run_writes_nothing(
    risk_db, now, trace, overrides, expected
):
    _, sessions = risk_db
    await record_market(sessions, now)

    refused = await run(sessions, runner_settings(**overrides), now)

    assert refused.kind == "run_configuration_refused"
    assert refused.reason == expected
    assert await cases_in(sessions) == []


def test_observe_mode_cannot_configure_a_run():
    """Caught in the settings, so it cannot reach a process at all."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="requires TRADING_MODE=PAPER"):
        runner_settings(trading_mode=TradingMode.OBSERVE.value)


def test_a_step_may_not_be_allowed_to_outlast_the_run():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="outlast the run"):
        runner_settings(paper_runner_max_seconds=10, paper_runner_step_timeout_seconds=30)


# ------------------------------------------------------- data and evidence


async def test_without_recorded_market_data_nothing_is_opened(risk_db, now, trace):
    """No candidate, no case. Not an error — an honest empty pass."""
    _, sessions = risk_db

    summary = await run(sessions, runner_settings(), now)

    assert summary.exit_code is ExitCode.COMPLETED
    assert summary.candidates_seen == 0
    assert summary.cases_opened == 0
    assert summary.stop is RunStop.NOTHING_LEFT_TO_DO


async def test_stale_market_data_prevents_the_fill(risk_db, now, trace):
    """The case is ready, the sources are not, and the run says which."""
    _, sessions = risk_db
    await record_market(sessions, now, age=timedelta(seconds=45))
    settings = runner_settings(market_max_age_seconds=3600)
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-stale")

    summary = await run(sessions, settings, now)

    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.execution_id is None
    assert progress.risk_refusal in (
        "SOURCE_OLDER_THAN_RISK_LIMIT",
        "RISK_DATA_INCOMPLETE",
    )
    assert summary.fills == 0
    assert await executions(sessions) == []


async def test_a_missing_entry_size_refuses_before_sentinel(risk_db, now, trace):
    """An unset amount is nobody's decision to make, least of all a runner's."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings(paper_requested_notional_usd=None)
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="runner-unsized")

    summary = await run(sessions, settings, now)

    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.risk_refusal is not None
    assert progress.execution_id is None
    assert await executions(sessions) == []


# ------------------------------------------------------- scope


async def test_a_run_never_exits_or_re_enters_a_position(risk_db, now, trace):
    """It opens and fills. Closing and reopening are other contracts entirely."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-scope")
    assert (await run(sessions, settings, now)).fills == 1

    again = await run(sessions, settings, now)

    assert again.fills == 0
    async with sessions() as session:
        sold = await session.scalar(select(func.count()).select_from(TradeCaseExitRow))
        cycles = (await session.scalars(select(TradeCycleRow))).all()
    assert sold == 0
    assert [item.sequence for item in cycles] == [1]
    assert all(item.predecessor_exit_id is None for item in cycles)


async def test_an_executed_market_is_not_opened_again_by_the_next_run(risk_db, now, trace):
    """Intake's own bar, honoured because the run uses intake rather than bypassing it."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    await ready_case(stack.cases, now, trace, key="runner-bar")
    assert (await run(sessions, settings, now)).fills == 1

    later = await run(sessions, settings, now)

    assert later.cases_opened == 0
    assert "POSITION_OPENED_FOR_MARKET" in later.intake_refusals
    assert len(await cases_in(sessions)) == 1
