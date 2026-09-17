"""What one pass is actually allowed to touch, checked against the database.

Every assertion here reads real rows: the cases intake wrote, the attempts the
runtime recorded, the tasks that were claimed. A budget that only holds in a
summary is not a budget, and a test that rebuilt the intake policy itself would
be testing its own arithmetic rather than the production composition.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from src.core.models import AgentRole
from src.data.tables import TradeCaseRow, WorkerTaskAttemptRow
from src.orchestration.worker.models import TaskLease
from src.runner.models import ExitCode, RunStop
from src.runner.service import BoundedPaperRun
from tests.riskdata.conftest import CHAIN, NETWORK, recorded_snapshot
from tests.runner.conftest import FRESH, record_market, run, runner_settings, stack_for


async def record_markets(sessions, now, count: int):
    """Several distinct, equally valid recorded markets."""
    from src.core.clock import FixedClock
    from src.markets.recorder import MarketRecorder

    recorder = MarketRecorder(sessions, clock=FixedClock(now))
    for index in range(count):
        token = f"{index + 1:02x}" * 20
        pool = f"{index + 0x41:02x}" * 20
        await recorder.record(
            recorded_snapshot(
                now,
                age=FRESH,
                metadata_age=FRESH,
                base_asset_id=f"{CHAIN}:{NETWORK}:0x{token}",
                pair_id=f"{CHAIN}:{NETWORK}:contract_address:0x{pool}",
                label=f"market-{index}",
            )
        )


async def cases_written(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TradeCaseRow))


async def worked_cases(sessions) -> set:
    """The distinct cases any worker actually attempted anything on."""
    async with sessions() as session:
        rows = (await session.scalars(select(WorkerTaskAttemptRow.trade_case_id))).all()
    return set(rows)


# ------------------------------------------------------- candidates


async def test_the_candidate_budget_bounds_what_intake_writes(risk_db, now, trace):
    """One candidate means one case in the database, not five trimmed to one.

    The control policy's own per-cycle ceiling is five. A budget applied to the
    result would leave four cases behind that nobody asked for and nobody will
    work; this asserts the rows, not the report.
    """
    _, sessions = risk_db
    await record_markets(sessions, now, 4)

    summary = await run(sessions, runner_settings(paper_runner_max_candidates=1), now)

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert summary.cases_opened == 1
    assert await cases_written(sessions) == 1


async def test_candidates_and_opened_cases_are_different_quantities(risk_db, now, trace):
    """Four candidates processed; one of them taken on.

    Two budgets because they are two questions. Reading a market costs a read;
    opening a case creates work somebody has to finish or expire, and the second
    number is the one that bounds it.
    """
    _, sessions = risk_db
    await record_markets(sessions, now, 4)

    summary = await run(
        sessions,
        runner_settings(paper_runner_max_candidates=4, paper_runner_max_new_cases=1),
        now,
    )

    assert summary.candidates_seen == 4
    assert summary.cases_opened == 1
    assert "CYCLE_LIMIT_REACHED" in summary.intake_refusals
    assert await cases_written(sessions) == 1


async def test_the_processing_budget_stops_intake_reading_further(risk_db, now, trace):
    """One candidate processed means three never judged at all."""
    _, sessions = risk_db
    await record_markets(sessions, now, 4)

    summary = await run(sessions, runner_settings(paper_runner_max_candidates=1), now)

    assert summary.candidates_seen == 1
    assert summary.cases_opened == 1
    assert await cases_written(sessions) == 1


# ------------------------------------------------------- cases


async def claimable_cases(sessions, now, trace, count: int):
    """Several existing cases, each with a claimable ORBIT task."""
    from src.core.clock import FixedClock
    from src.orchestration.workflow.service import TradeCaseService
    from tests.riskdata.conftest import market_for

    cases = TradeCaseService(sessions, clock=FixedClock(now))
    opened = []
    for index in range(count):
        market = market_for(token=f"{index + 0x71:02x}" * 20, pool=f"{index + 0x81:02x}" * 20)
        opened.append(
            await cases.open_trade_case(
                market,
                originating_discovery_reference=uuid4(),
                correlation_id=trace,
                idempotency_key=f"budget-case-{index}",
                expires_at=now + timedelta(hours=1),
            )
        )
    return opened


async def test_the_case_budget_bounds_which_cases_a_worker_touches(risk_db, now, trace):
    """Three cases with claimable work, a budget of one, one case worked.

    No market is recorded, so intake opens nothing and the whole budget is spent
    by the worker stage — which is exactly where it was not being applied.
    """
    from tests.runner.specialists import ScriptedSpecialists

    _, sessions = risk_db
    await claimable_cases(sessions, now, trace, 3)
    settings = runner_settings(
        paper_runner_max_cases=1,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )

    summary = await run(
        sessions,
        settings,
        now,
        ports=__import__("src.runner.composition", fromlist=["RunnerPorts"]).RunnerPorts(
            reasoning=ScriptedSpecialists()
        ),
    )

    assert summary.exit_code is ExitCode.COMPLETED, summary
    touched = await worked_cases(sessions)
    assert len(touched) <= 1, touched
    assert await cases_written(sessions) == 3


async def test_intake_and_existing_work_share_one_case_budget(risk_db, now, trace):
    """A new case and existing ones draw on the same allowance."""
    from src.runner.composition import RunnerPorts
    from tests.runner.specialists import ScriptedSpecialists

    _, sessions = risk_db
    await record_market(sessions, now)
    await claimable_cases(sessions, now, trace, 2)
    settings = runner_settings(
        paper_runner_max_cases=1,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )

    summary = await run(sessions, settings, now, ports=RunnerPorts(reasoning=ScriptedSpecialists()))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    # One case opened, and that is the whole allowance: no worker may then pick
    # up one of the two that were already there.
    assert summary.cases_opened == 1
    touched = await worked_cases(sessions)
    assert len(touched) <= 1, touched


async def test_a_claim_that_times_out_still_spends_its_place(risk_db, now, trace):
    """Started work counts, and the case it started on is still attributable."""
    import asyncio

    from src.runner.composition import RunnerPorts

    _, sessions = risk_db
    opened = await claimable_cases(sessions, now, trace, 2)
    settings = runner_settings(
        paper_runner_max_cases=1,
        paper_runner_step_timeout_seconds=1,
        paper_runner_max_seconds=30,
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
    )
    stack = stack_for(sessions, settings, now, ports=RunnerPorts(reasoning=_never()))

    class Hanging:
        role = AgentRole.ORBIT
        task_type = "VERIFY_DISCOVERY"

        def __init__(self) -> None:
            self.cases: list = []

        async def handle(self, lease: TaskLease, capabilities: object):
            self.cases.append(lease.trade_case_id)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    handler = Hanging()
    object.__setattr__(stack.runners[0], "handler", handler)

    summary = await asyncio.wait_for(BoundedPaperRun(stack).execute(), timeout=25)

    assert summary.exit_code is ExitCode.COMPLETED
    # Exactly one claim, on a case inside the allowance, and it is counted.
    assert len(handler.cases) == 1
    assert handler.cases[0] in {case.id for case in opened}
    assert summary.steps_timed_out == 1
    assert summary.stop in (RunStop.NOTHING_LEFT_TO_DO, RunStop.CASE_BUDGET_REACHED)


def _never():
    from tests.runner.specialists import ScriptedSpecialists

    return ScriptedSpecialists()
