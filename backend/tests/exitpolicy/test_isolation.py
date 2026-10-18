"""Issue #111: one position's failure never stops the exits of the others.

Three booked PAPER holdings, all past their stop. One of them fails in a way
no code path names -- while it is judged, while its sale is attempted, or
after its sale was booked -- and the other two must still be sold in the same
sweep. The failing one is counted as `failed` (so the exit run is not ready,
which alarms and holds entries), is never sold twice, and is judged and sold
again by the next sweep. Real chain from `tests/paperexit/test_exit_rpc_failures.py`:
the real exit service, ledger and ATLAS read; nothing calls a provider.
"""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.orchestration.exitpolicy.drive import ExitSweep, alone, attempt_exits
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.jobruns.models import (
    Coverage,
    ExitHealth,
    ExitRunFacts,
    PolicyClass,
    SweepFacts,
    assess_exit_run,
)
from tests.paperexit.conftest import build_exit_service, entered, read_account
from tests.paperexit.test_exit_rpc_failures import (
    LATER,
    POLICY,
    SECOND,
    ScriptedClient,
    counts,
    exit_read,
    observed,
)
from tests.riskdata.conftest import IDENTITY, market_for

THIRD = market_for(token="e9" * 20, pool="fa" * 20)
MARKETS = (IDENTITY, SECOND, THIRD)


class Breaking:
    """The real exit read, raising an unnamed error for one market's holding."""

    def __init__(self, inner: Any, broken: str, *, times: int = 1) -> None:
        self.inner, self.broken, self.remaining = inner, broken, times

    async def read(self, trade_case_id, market, request_key):
        if market.base_asset_id == self.broken and self.remaining > 0:
            self.remaining -= 1
            raise ValueError("an error no code path names")
        return await self.inner.read(trade_case_id, market, request_key)


async def three_holdings(sessions, now) -> None:
    feed = observed(now, *MARKETS, price="1.25")
    await entered(sessions, now, uuid4(), key="first", feed=feed)
    await entered(sessions, now, uuid4(), key="second", feed=feed, identity=SECOND)
    await entered(sessions, now, uuid4(), key="third", feed=feed, identity=THIRD)


def sweeper(sessions, at, read, **overrides) -> AutoExitService:
    feed = observed(at, *MARKETS)
    return AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=feed, exit_read=read),
        markets=feed,
        policy=POLICY,
        limits=RiskLimits(),
        clock=FixedClock(at),
        **overrides,
    )


def healthy_read(at):
    return exit_read(at, ScriptedClient(at, failing="none", code=None, times=0))  # type: ignore[arg-type]


async def test_an_attempt_that_raises_does_not_stop_the_other_sales(risk_db, now) -> None:
    _, sessions = risk_db
    await three_holdings(sessions, now)
    at = now + LATER
    read = Breaking(healthy_read(at), SECOND.base_asset_id)

    result = await sweeper(sessions, at, read).sweep()

    assert (result.triggered, result.executed, result.failed) == (3, 2, 1), result
    assert result.refusals == {"EXIT_ATTEMPT_FAILED": 1}
    assert (await counts(sessions))[0] == 2
    assert (await read_account(sessions)).paused is False
    # The run is not ready: that alarms, and holds every new entry.
    health = assess_exit_run_for(result)
    assert health is ExitHealth.ATTEMPT_FAILED

    # The failed holding is judged again and sold by the next sweep, once.
    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, healthy_read(later)).sweep()
    assert (again.triggered, again.executed, again.failed) == (1, 1, 0), again
    assert (await counts(sessions))[0] == 3


async def test_a_judgement_that_raises_does_not_stop_the_other_sales(risk_db, now) -> None:
    _, sessions = risk_db
    await three_holdings(sessions, now)
    at = now + LATER
    service = sweeper(sessions, at, healthy_read(at))
    original = service._inputs
    broken: list[Any] = []

    async def inputs(position, entry, instant):
        if not broken:
            broken.append(position.id)
            raise ValueError("a position nobody can judge")
        return await original(position, entry, instant)

    object.__setattr__(service, "_inputs", inputs)
    result = await service.sweep()

    assert (result.triggered, result.executed, result.failed) == (2, 2, 1), result
    assert result.refusals == {"EXIT_JUDGEMENT_FAILED": 1}
    assert result.unevaluated == 0
    assert (await counts(sessions))[0] == 2

    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, healthy_read(later)).sweep()
    assert again.executed == 1
    assert (await counts(sessions))[0] == 3


async def test_a_failure_after_the_booking_counts_the_sale_once(risk_db, now) -> None:
    _, sessions = risk_db
    await three_holdings(sessions, now)
    at = now + LATER
    service = sweeper(sessions, at, healthy_read(at))
    result = ExitSweep()
    planned = await service.plan(result)
    first = planned[0]

    async def booked_then_raises(context):
        await first.start(context)
        raise ValueError("after the commit")

    candidates = [replace(first, start=booked_then_raises), *planned[1:]]
    await attempt_exits(
        candidates, alone(max_exits=5, max_attempts=5, max_refreshes=5), sessions=sessions
    )

    # Booked, then raised: settled by its key as booked, never failed or redone.
    assert (result.executed, result.failed) == (3, 0), result
    assert (await counts(sessions))[0] == 3
    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, healthy_read(later)).sweep()
    assert (again.triggered, again.executed) == (0, 0)
    assert (await counts(sessions))[0] == 3


def assess_exit_run_for(result: ExitSweep) -> ExitHealth | None:
    """The exit run's health as the job record would judge this sweep."""
    facts = SweepFacts(
        unevaluated=result.unevaluated,
        plan_stopped=result.plan_stop is not None,
        unknown=result.unknown,
        failed=result.failed,
        blocked=result.blocked,
        timed_out=result.timed_out,
        deferred=sum(result.deferred.values()),
        mark_unknown=result.mark_unknown,
    )
    assessment = assess_exit_run(
        ExitRunFacts(
            lock="ACQUIRED",
            stop="NOTHING_LEFT_TO_DO",
            errors=(),
            policies=frozenset({PolicyClass.NORMAL}),
            coverage=Coverage(normal=3),
            acquisition_enabled=True,
            acquisition_stop="COMPLETED",
            acquisition_shortfall=0,
            sweeps=(facts,),
        )
    )
    return assessment.reason


async def test_an_early_judgement_that_raises_is_failed_and_retried(risk_db, now) -> None:
    from tests.early.test_exit import early_entry, observe
    from tests.early.test_exit import sweeper as early_sweeper

    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.40")
    service = early_sweeper(sessions, at)

    async def broken(position, entry, instant):
        raise ValueError("a position nobody can judge")

    object.__setattr__(service, "_inputs", broken)
    result = await service.sweep()
    assert (result.evaluated, result.failed, result.executed) == (0, 1, 0), result
    assert result.refusals == {"EXIT_JUDGEMENT_FAILED": 1}

    later = at + timedelta(minutes=1)
    await observe(sessions, later, price="0.40")
    again = await early_sweeper(sessions, later).sweep()
    assert again.executed == 1


async def test_a_booked_sale_whose_confirmation_fails_is_never_sold_again(
    risk_db, now, monkeypatch
) -> None:
    # The sale was booked, then the attempt raised, and the settling read also
    # failed: counted failed, conservatively -- and still never sold twice.
    import src.orchestration.exitpolicy.drive as drive

    _, sessions = risk_db
    await three_holdings(sessions, now)
    at = now + LATER
    result = ExitSweep()
    planned = await sweeper(sessions, at, healthy_read(at)).plan(result)
    first = planned[0]

    async def booked_then_raises(context):
        await first.start(context)
        raise ValueError("after the commit")

    async def unconfirmed(sessions, request_key, seconds):
        return False

    monkeypatch.setattr(drive, "_booked", unconfirmed)
    await attempt_exits(
        [replace(first, start=booked_then_raises)],
        alone(max_exits=5, max_attempts=5, max_refreshes=5),
        sessions=sessions,
    )
    assert (result.executed, result.failed) == (0, 1), result
    assert (await counts(sessions))[0] == 1
    monkeypatch.undo()

    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, healthy_read(later)).sweep()
    # The booked holding is closed: only the other two are sold, once each.
    assert (again.triggered, again.executed) == (2, 2), again
    assert (await counts(sessions))[0] == 3
