"""The exit job (`--exits-once`) and the per-job run locks.

The exit job runs the production `BoundedPaperRun` in `EXITS_ONLY` mode over the
production stack: acquisition of open positions' markets only, then both exit
sweeps. Nothing else may happen in it — no discovery, intake, worker, risk
request or BUY. Each job holds its own PostgreSQL advisory lock for the whole
run; a second start of the same job ends as `ALREADY_RUNNING`.
"""

import asyncio
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import ExecutionRow, TradeCaseExitRow, TradeCaseRow
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.orchestration.exitpolicy.early import EarlyExitService
from src.orchestration.exitpolicy.service import ExitSweep
from src.orchestration.paperexit.exitread import AtlasExitRead
from src.runner.composition import RunnerPorts
from src.runner.locks import LockOutcome, RunJob, job_lock
from src.runner.models import RunMode, RunStop
from src.runner.service import BoundedPaperRun, Deadline
from tests.atlas.conftest import builder_for
from tests.early.test_exit import early_entry
from tests.paperexit.conftest import build_exit_service
from tests.riskdata.conftest import IDENTITY, configured_costs
from tests.riskrequest.conftest import fresh_snapshot
from tests.runner.conftest import runner_settings, stack_for
from tests.runner.provider import MarketProvider, pool
from tests.runner.test_position_refresh import answer, early_positions

ZERO = configured_costs(fee="0", slippage="0")
POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL advisory locks required"
)


def exit_settings(**overrides):
    values = {
        "market_provider": "geckoterminal",
        "market_chains": "robinhood",
        "paper_runner_market_acquisition_enabled": True,
        "paper_runner_acquisition_max_discovery_requests": 1,
        "geckoterminal_retry_delay_seconds": 0,
        "early_paper_exit_enabled": True,
    }
    values.update(overrides)
    return runner_settings(**values)


def early_sweeper(sessions, at):
    clock = FixedClock(at)
    markets = MarketReader(sessions, clock=clock)
    return EarlyExitService(
        sessions=sessions,
        exits=build_exit_service(
            sessions,
            at,
            feed=markets,
            exit_read=AtlasExitRead(builder=builder_for(at), clock=clock),
            costs=ZERO,
        ),
        markets=markets,
        limits=RiskLimits(),
        clock=clock,
    )


def exit_stack(sessions, at, provider, **overrides):
    stack = stack_for(
        sessions,
        exit_settings(**overrides),
        at,
        ports=RunnerPorts(market_http=provider.transport()),
    )
    # The production early sweep, with the fixture chain read a test can run.
    return replace(stack, early_exits=early_sweeper(sessions, at))


async def held_early_position(sessions, now):
    """One real early holding on the default market, entered at 1.00."""
    await early_entry(sessions, now)
    await MarketRecorder(sessions, clock=FixedClock(now)).record(
        fresh_snapshot(now, price=Decimal("1.00"), label="entry-reading")
    )


async def count(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


# ------------------------------------------------------------ exit-only mode


async def test_an_exit_run_sells_on_its_own_fresh_reading_and_nothing_else(risk_db, now):
    _, sessions = risk_db
    await held_early_position(sessions, now)
    cases_before = await count(sessions, TradeCaseRow)
    fills_before = await count(sessions, ExecutionRow)
    at = now + timedelta(minutes=10)
    new_pool = pool("0x" + "77" * 20, base="0x" + "78" * 20, quote="0x" + "b2" * 20, price="1")
    provider = MarketProvider(discovery=[new_pool], targeted=[answer(IDENTITY, price="0.40")])

    summary = await BoundedPaperRun(
        exit_stack(sessions, at, provider), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.mode is RunMode.EXITS_ONLY
    assert summary.early_exits.triggers == ("STOP_LOSS",), summary
    assert summary.early_exits.executed == 1
    # Nothing on the entry side: no discovery request, no intake, no step, no
    # risk request, no new case and no BUY — the only new fill is the sale.
    assert provider.discovery_requests == []
    assert (summary.candidates_seen, summary.cases_opened, summary.steps_taken) == (0, 0, 0)
    assert (summary.risk_requests, summary.fills) == (0, 0)
    assert await count(sessions, TradeCaseRow) == cases_before
    assert await count(sessions, ExecutionRow) == fills_before + 1
    async with sessions() as session:
        sold = await session.scalar(select(TradeCaseExitRow))
    assert sold.exit_trigger == "STOP_LOSS"
    assert summary.acquisition.positions.answered == 1
    assert summary.duration_seconds >= 0


async def test_an_exit_run_observes_all_five_early_positions(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 5)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(item, price="1.20") for item in held])

    summary = await BoundedPaperRun(
        exit_stack(sessions, at, provider), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.acquisition.positions.answered == 5
    assert summary.acquisition.positions.complete is True
    assert len(provider.multi_requests) == 1 and provider.discovery_requests == []


async def test_an_exit_run_never_sells_without_its_own_fresh_reading(risk_db, now):
    _, sessions = risk_db
    await held_early_position(sessions, now)
    at = now + timedelta(minutes=10)
    # The provider does not answer for the held market: no fresh own mark.
    provider = MarketProvider(targeted=[])

    summary = await BoundedPaperRun(
        exit_stack(sessions, at, provider), mode=RunMode.EXITS_ONLY
    ).execute()

    assert summary.early_exits.executed == 0
    assert summary.early_exits.refusals == ("EARLY_EXIT_MARK_UNKNOWN",)
    assert await count(sessions, TradeCaseExitRow) == 0


async def test_an_exit_run_with_no_exit_policy_refuses_to_spend_anything(risk_db, now):
    _, sessions = risk_db
    provider = MarketProvider()
    stack = stack_for(
        sessions,
        exit_settings(early_paper_exit_enabled=False),
        now,
        ports=RunnerPorts(market_http=provider.transport()),
    )

    reading = await BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute()

    assert reading.kind == "run_configuration_refused"
    assert reading.reason == "NO_EXIT_SWEEP_CONFIGURED"
    assert provider.paths == []


async def test_an_exit_run_refuses_outside_paper(risk_db, now):
    from src.core.models import TradingMode
    from src.runner.service import refuse

    settings = exit_settings()
    assert refuse(settings.model_copy(update={"trading_mode": TradingMode.OBSERVE})) is not None


async def test_a_full_run_is_unchanged_and_reports_its_mode(risk_db, now):
    _, sessions = risk_db
    summary = await BoundedPaperRun(stack_for(sessions, runner_settings(), now)).execute()
    assert summary.mode is RunMode.FULL
    assert summary.stop is RunStop.NOTHING_LEFT_TO_DO
    expected = (
        LockOutcome.ACQUIRED if os.environ.get("TEST_DATABASE_URL") else LockOutcome.NOT_SUPPORTED
    )
    assert summary.lock == expected.value


async def test_an_exit_run_that_runs_out_of_time_still_releases_its_lock(risk_db, now):
    _, sessions = risk_db
    await held_early_position(sessions, now)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(IDENTITY, price="0.40")])
    expired = Deadline(0)

    first = await BoundedPaperRun(
        exit_stack(sessions, at, provider), mode=RunMode.EXITS_ONLY, deadline=expired
    ).execute()
    second = await BoundedPaperRun(
        exit_stack(sessions, at, provider), mode=RunMode.EXITS_ONLY
    ).execute()

    assert first.stop is RunStop.TIME_BUDGET_REACHED
    assert second.stop is not RunStop.ALREADY_RUNNING
    assert second.early_exits.executed == 1


def test_the_cli_offers_the_exit_job_as_its_own_exclusive_mode(monkeypatch):
    import sys

    from src.runner import main

    monkeypatch.setattr(sys, "argv", ["runner", "--exits-once", "--once"])
    with pytest.raises(SystemExit) as stopped:
        main.main()
    assert stopped.value.code == 2  # rejected by the parser before anything ran
    assert callable(main.run_exits_once)


# ----------------------------------------------------------------- locks


class Gated:
    """An exit sweep that waits until released, so a second run can collide."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def sweep(self, **_: object) -> ExitSweep:
        self.entered.set()
        await self.release.wait()
        return ExitSweep()


async def _collide(sessions, now, mode):
    gate = Gated()
    stack = replace(stack_for(sessions, runner_settings(), now), exits=gate)
    first = asyncio.create_task(BoundedPaperRun(stack, mode=mode).execute())
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    second = await BoundedPaperRun(stack, mode=mode).execute()
    gate.release.set()
    return await first, second


@POSTGRES
@pytest.mark.parametrize("mode", [RunMode.EXITS_ONLY, RunMode.FULL], ids=["exit", "entry"])
async def test_a_second_start_of_the_same_job_is_already_running(risk_db, now, mode):
    _, sessions = risk_db
    first, second = await _collide(sessions, now, mode)

    assert second.stop is RunStop.ALREADY_RUNNING
    assert second.lock == LockOutcome.ALREADY_RUNNING.value
    assert (second.steps_taken, second.fills, second.exits) == (0, 0, None)
    assert first.lock == LockOutcome.ACQUIRED.value
    assert first.stop is not RunStop.ALREADY_RUNNING
    # Released afterwards: the next start of the job runs.
    after = await BoundedPaperRun(
        replace(stack_for(sessions, runner_settings(), now), exits=_Done()), mode=mode
    ).execute()
    assert after.lock == LockOutcome.ACQUIRED.value


class _Done:
    async def sweep(self, **_: object) -> ExitSweep:
        return ExitSweep()


@POSTGRES
async def test_the_exit_job_and_the_entry_job_do_not_exclude_each_other(risk_db, now):
    _, sessions = risk_db
    gate = Gated()
    entry = replace(stack_for(sessions, runner_settings(), now), exits=gate)
    running = asyncio.create_task(BoundedPaperRun(entry, mode=RunMode.FULL).execute())
    await asyncio.wait_for(gate.entered.wait(), timeout=10)

    exits = await BoundedPaperRun(
        replace(stack_for(sessions, runner_settings(), now), exits=_Done()),
        mode=RunMode.EXITS_ONLY,
    ).execute()
    gate.release.set()
    full = await running

    assert exits.lock == LockOutcome.ACQUIRED.value
    assert full.lock == LockOutcome.ACQUIRED.value


@POSTGRES
async def test_an_exit_job_and_an_entry_job_racing_on_one_position_sell_once(risk_db, now):
    _, sessions = risk_db
    await held_early_position(sessions, now)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(IDENTITY, price="0.40")])
    exit_job = exit_stack(sessions, at, provider)
    entry_job = replace(
        stack_for(sessions, runner_settings(early_paper_exit_enabled=True), at),
        early_exits=early_sweeper(sessions, at),
        acquisition=exit_job.acquisition,
    )

    results = await asyncio.gather(
        BoundedPaperRun(exit_job, mode=RunMode.EXITS_ONLY).execute(),
        BoundedPaperRun(entry_job, mode=RunMode.FULL).execute(),
    )

    assert all(item.stop is not RunStop.ALREADY_RUNNING for item in results)
    assert await count(sessions, TradeCaseExitRow) == 1


@POSTGRES
async def test_a_lock_held_by_a_dead_connection_is_released(risk_db, now):
    """A crashed process: its connection goes, and with it the lock."""
    from sqlalchemy import text

    _, sessions = risk_db
    engine = sessions.kw["bind"]
    connection = await engine.connect()
    held = await connection.scalar(
        text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": RunJob.PAPER_EXIT_JOB.value}
    )
    await connection.commit()
    assert held is True
    async with job_lock(sessions, RunJob.PAPER_EXIT_JOB) as busy:
        assert busy is LockOutcome.ALREADY_RUNNING
    # The process dies: the connection is dropped without unlocking.
    await connection.invalidate()
    await connection.close()

    async with job_lock(sessions, RunJob.PAPER_EXIT_JOB) as again:
        assert again is LockOutcome.ACQUIRED


@POSTGRES
async def test_a_cancelled_run_releases_its_lock(risk_db, now):
    _, sessions = risk_db
    gate = Gated()
    stack = replace(stack_for(sessions, runner_settings(), now), exits=gate)
    task = asyncio.create_task(BoundedPaperRun(stack, mode=RunMode.EXITS_ONLY).execute())
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with job_lock(sessions, RunJob.PAPER_EXIT_JOB) as again:
        assert again is LockOutcome.ACQUIRED


async def test_an_exit_run_retries_a_sale_after_a_chain_rpc_failure(risk_db, now):
    """The exit job inherits the named RPC refusal of the exit read: retried next run."""
    from src.runtime.models import ErrorCode
    from tests.paperexit.test_exit_rpc_failures import ScriptedClient, exit_read

    _, sessions = risk_db
    await held_early_position(sessions, now)
    at = now + timedelta(minutes=10)
    client = ScriptedClient(at, failing="verify_chain", code=ErrorCode.TIMEOUT, times=1)

    def stack_at(moment):
        clock = FixedClock(moment)
        markets = MarketReader(sessions, clock=clock)
        sweeper = EarlyExitService(
            sessions=sessions,
            exits=build_exit_service(
                sessions, moment, feed=markets, exit_read=exit_read(moment, client), costs=ZERO
            ),
            markets=markets,
            limits=RiskLimits(),
            clock=clock,
        )
        provider = MarketProvider(targeted=[answer(IDENTITY, price="0.40")])
        return replace(exit_stack(sessions, moment, provider), early_exits=sweeper)

    first = await BoundedPaperRun(stack_at(at), mode=RunMode.EXITS_ONLY).execute()
    later = at + timedelta(minutes=1)
    second = await BoundedPaperRun(stack_at(later), mode=RunMode.EXITS_ONLY).execute()

    assert first.early_exits.refusals == ("EXIT_READ_UNAVAILABLE",)
    assert second.early_exits.executed == 1
    assert await count(sessions, TradeCaseExitRow) == 1
