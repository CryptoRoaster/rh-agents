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
