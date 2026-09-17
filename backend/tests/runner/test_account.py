"""What the summary keeps when something goes wrong afterwards.

A run reports what it observed, not what it hoped. Work that committed stays in
the account; work whose outcome nobody knows is named as unknown; and nothing
another run committed is ever counted as this one's.
"""

from uuid import uuid4

from sqlalchemy import func, select

from src.data.tables import TradeCaseRiskRequestRow, TradeCaseRow
from src.runner.models import ExitCode
from src.runner.service import BoundedPaperRun
from tests.runner.conftest import (
    executions,
    ready_case,
    record_market,
    run,
    runner_settings,
    stack_for,
)
from tests.runner.test_budgets import record_markets


async def test_a_committed_approve_survives_a_failing_fill(risk_db, now, trace):
    """The verdict is its own transaction, and it committed. It is reported.

    Reproduction: the approval was only written into the account once the fill
    had answered, so a fill that raised took a committed SENTINEL verdict down
    with it and the run reported a case it had never asked about.
    """
    from sqlalchemy.exc import OperationalError

    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="account-approve")

    async def breaks(*arguments, **keywords):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    object.__setattr__(stack.fills, "execute_case_fill", breaks)

    summary = await BoundedPaperRun(stack).execute()

    # The fault is visible, and no fill is claimed.
    assert summary.errors == ("DATABASE_UNAVAILABLE",)
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE
    assert summary.fills == 0
    assert await executions(sessions) == []

    # And the verdict that really did commit is in the account.
    progress = next(item for item in summary.cases if item.trade_case_id == case.id)
    assert progress.risk_outcome == "APPROVE"
    assert summary.risk_requests == 1
    async with sessions() as session:
        stored = await session.scalar(
            select(TradeCaseRiskRequestRow).where(TradeCaseRiskRequestRow.trade_case_id == case.id)
        )
    assert stored is not None and stored.outcome == "APPROVE"


async def test_an_interrupted_intake_does_not_claim_its_cases_never_happened(risk_db, now, trace):
    """Intake commits per candidate. A cycle that dies mid-way leaves cases.

    Reproduction: the whole cycle's outcome was read from its return value, so a
    cycle that raised after committing one case reported zero opened — and the
    case was there, in the database, for the next run to find.
    """
    _, sessions = risk_db
    await record_markets(sessions, now, 3)
    settings = runner_settings(paper_runner_max_candidates=3, paper_runner_max_new_cases=3)
    stack = stack_for(sessions, settings, now)
    opened: list[object] = []
    original = stack.cases.open_trade_case_in_session

    async def breaks_after_the_first(session, market, **keywords):
        if opened:
            raise RuntimeError("the cycle dies after one case is committed")
        result = await original(session, market, **keywords)
        opened.append(result)
        return result

    object.__setattr__(stack.cases, "open_trade_case_in_session", breaks_after_the_first)

    summary = await BoundedPaperRun(stack).execute()

    # One case really exists; the run says so rather than reporting none.
    assert await written(sessions) == 1
    assert summary.cases_opened >= 1 or summary.intake_outcome_unknown
    if summary.intake_outcome_unknown:
        # An unconfirmed cycle is named as one, and no count is invented.
        assert summary.cases_opened == 0
    assert summary.exit_code in (ExitCode.COMPLETED, ExitCode.TECHNICAL_FAILURE)


async def written(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TradeCaseRow))


async def test_a_run_counts_no_case_another_run_opened(risk_db, now, trace):
    """Cases that were already there are not this pass's openings."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    first = await run(sessions, settings, now)
    assert first.cases_opened == 1

    second = await run(sessions, settings, now)

    assert second.cases_opened == 0
    assert await written(sessions) == 1


async def test_replay_is_still_told_apart_from_a_fresh_execution(risk_db, now, trace):
    """A stored answer returned again is marked, not recounted as new work."""
    _, sessions = risk_db
    await record_market(sessions, now)
    settings = runner_settings()
    stack = stack_for(sessions, settings, now)
    case = await ready_case(stack.cases, now, trace, key="account-replay")

    first = await BoundedPaperRun(stack).execute()
    assert first.fills == 1
    fresh = next(item for item in first.cases if item.trade_case_id == case.id)
    assert fresh.replayed is False

    # The same case again: the workflow has ended it, so there is nothing to
    # replay through the decision path and nothing new to execute either.
    again = await run(sessions, settings, now, run_id=uuid4())
    assert again.fills == 0
    assert len(await executions(sessions)) == 1


async def two_claimable_cases(sessions, now, trace):
    """Two cases that were already there, each with claimable work."""
    from tests.runner.test_budgets import claimable_cases

    return await claimable_cases(sessions, now, trace, 2)


async def attempted_cases(sessions) -> set:
    from src.data.tables import WorkerTaskAttemptRow

    async with sessions() as session:
        return set((await session.scalars(select(WorkerTaskAttemptRow.trade_case_id))).all())


async def test_an_unconfirmed_intake_does_not_hand_the_budget_out_again(risk_db, now, trace):
    """A cycle that committed something and then broke has spent the allowance.

    Reproduction: the unknown outcome was reported honestly and the pass carried
    on regardless. The case intake had already committed was not in the run's
    working set, so the worker stage read a fresh set from the database and
    handed out the whole case budget a second time — two older cases worked on
    top of one just opened, against an allowance of two.
    """
    from src.runner.composition import RunnerPorts
    from tests.runner.specialists import ScriptedSpecialists
    from tests.runner.test_budgets import record_markets

    _, sessions = risk_db
    await two_claimable_cases(sessions, now, trace)
    await record_markets(sessions, now, 3)
    settings = runner_settings(
        paper_runner_max_cases=2,
        paper_runner_max_candidates=3,
        paper_runner_max_new_cases=3,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )
    stack = stack_for(sessions, settings, now, ports=RunnerPorts(reasoning=ScriptedSpecialists()))
    before = await written(sessions)
    opened: list[object] = []
    original = stack.cases.open_trade_case_in_session

    async def breaks_on_the_second(session, market, **keywords):
        if opened:
            raise RuntimeError("the cycle dies on the next candidate")
        result = await original(session, market, **keywords)
        opened.append(result)
        return result

    object.__setattr__(stack.cases, "open_trade_case_in_session", breaks_on_the_second)

    summary = await BoundedPaperRun(stack).execute()

    # One case really was committed and stays committed.
    assert await written(sessions) == before + 1

    # And that is the end of this pass: no claim, no verdict, no fill.
    assert await attempted_cases(sessions) == set()
    assert summary.risk_requests == 0
    assert summary.fills == 0
    assert await executions(sessions) == []

    # The outcome is named, not counted, and the run reports a fault.
    assert summary.intake_outcome_unknown is True
    assert "INTAKE_OUTCOME_UNKNOWN" in summary.errors
    assert summary.cases_opened == 0
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE


async def test_an_intake_that_times_out_mid_cycle_also_stops_the_pass(risk_db, now, trace):
    """Unknown *and* out of time, with nothing mutated afterwards."""
    import asyncio

    from src.runner.composition import RunnerPorts
    from src.runner.models import RunStop
    from src.runner.service import Deadline
    from tests.runner.specialists import ScriptedSpecialists
    from tests.runner.test_budgets import record_markets

    _, sessions = risk_db
    await two_claimable_cases(sessions, now, trace)
    await record_markets(sessions, now, 3)
    settings = runner_settings(
        paper_runner_max_cases=2,
        paper_runner_max_candidates=3,
        paper_runner_max_new_cases=3,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )
    stack = stack_for(sessions, settings, now, ports=RunnerPorts(reasoning=ScriptedSpecialists()))
    before = await written(sessions)
    opened: list[object] = []
    original = stack.cases.open_trade_case_in_session

    async def hangs_on_the_second(session, market, **keywords):
        if opened:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        result = await original(session, market, **keywords)
        opened.append(result)
        return result

    object.__setattr__(stack.cases, "open_trade_case_in_session", hangs_on_the_second)

    summary = await asyncio.wait_for(
        BoundedPaperRun(stack, deadline=Deadline(2)).execute(), timeout=25
    )

    assert await written(sessions) == before + 1
    assert await attempted_cases(sessions) == set()
    assert summary.fills == 0
    assert summary.intake_outcome_unknown is True
    assert summary.stop is RunStop.TIME_BUDGET_REACHED
    assert summary.cases_opened == 0


async def test_the_next_explicit_run_picks_the_work_up(risk_db, now, trace):
    """Nothing is lost: what committed is ordinary work for the next pass."""
    from src.runner.composition import RunnerPorts
    from tests.runner.specialists import ScriptedSpecialists
    from tests.runner.test_budgets import record_markets

    _, sessions = risk_db
    await two_claimable_cases(sessions, now, trace)
    await record_markets(sessions, now, 3)
    settings = runner_settings(
        paper_runner_max_cases=2,
        paper_runner_max_candidates=3,
        paper_runner_max_new_cases=3,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )

    def ports():
        return RunnerPorts(reasoning=ScriptedSpecialists())

    stack = stack_for(sessions, settings, now, ports=ports())
    opened: list[object] = []
    original = stack.cases.open_trade_case_in_session

    async def breaks_on_the_second(session, market, **keywords):
        if opened:
            raise RuntimeError("the cycle dies on the next candidate")
        result = await original(session, market, **keywords)
        opened.append(result)
        return result

    object.__setattr__(stack.cases, "open_trade_case_in_session", breaks_on_the_second)
    await BoundedPaperRun(stack).execute()
    assert await attempted_cases(sessions) == set()

    # A second, ordinary run. Nothing special is needed for it to continue.
    second = await run(sessions, settings, now, ports=ports())

    assert second.exit_code is ExitCode.COMPLETED, second
    assert second.intake_outcome_unknown is False
    worked = await attempted_cases(sessions)
    assert worked, "the committed case and its neighbours are ordinary work now"
    assert len(worked) <= 2
