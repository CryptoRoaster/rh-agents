"""The exit phase's driver: order, admission, one attempt's outcome, and stops.

Over the real chain from `tests/exitpolicy/test_sweep.py` — a booked PAPER
entry, the real `PaperExitService`, the fixture ATLAS read — with the read
counted, so "no chain read was paid for" is a number, not a hope.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from src.core.models import TradingMode
from src.data.tables import AccountRow
from src.orchestration.exitpolicy.drive import (
    ExitBudget,
    ExitCandidate,
    ExitLimits,
    ExitSweep,
    attempt_exits,
    exit_order,
)
from src.orchestration.exitpolicy.refresh import RefreshBudget, RefreshContext
from src.runner.service import Deadline
from tests.exitpolicy.test_sweep import LATER, fresh_read, observed, sweeper
from tests.paperexit.conftest import build_exit_service, entered, exits


@dataclass
class Counted:
    """A chain read that counts how often it was asked."""

    inner: Any
    calls: int = 0

    async def read(self, trade_case_id, market, request_key):
        self.calls += 1
        return await self.inner.read(trade_case_id, market, request_key)


def candidate(name: str, result: ExitSweep | None = None) -> ExitCandidate:
    async def start(context: RefreshContext) -> Any:  # pragma: no cover - never started
        raise AssertionError

    return ExitCandidate(
        # Named, not random: the order is a function of the minute and the
        # position, and a test of it must be deterministic.
        position_id=uuid5(NAMESPACE_URL, f"rh-agents:test-position:{name}"),
        request_key=name,
        result=result if result is not None else ExitSweep(),
        port=None,
        start=start,
    )


def budget(**values: Any) -> ExitBudget:
    bounds = {"max_attempts": 5, "max_exits": 5, "max_refreshes": 5, **values}
    return ExitBudget(ExitLimits(**bounds))


async def falling(sessions, now, trace):
    """One booked entry, and its market recorded below the stop ten minutes later."""
    await entered(sessions, now, trace)
    at = now + LATER
    return at, observed(at, price=Decimal("0.9"))


# ---------------------------------------------------------------------- order


def test_the_order_is_deterministic_for_one_minute_and_interleaves_policies():
    at = datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    normal = [candidate(f"n{index}") for index in range(3)]
    early = [candidate("e0")]

    first = exit_order([normal, early], at)
    again = exit_order([normal, early], at + timedelta(seconds=59))

    assert [item.request_key for item in first] == [item.request_key for item in again]
    assert sorted(item.request_key for item in first) == ["e0", "n0", "n1", "n2"]
    # The early candidate is never behind more than one normal one.
    assert [item.request_key for item in first].index("e0") <= 1


def test_no_position_is_always_last_when_only_one_attempt_fits():
    """A stable identity order hands one position every first slot; this does not."""
    at = datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    held = [candidate(f"p{index}") for index in range(3)]

    firsts = {
        exit_order([held], at + timedelta(minutes=index))[0].request_key for index in range(12)
    }

    assert firsts == {"p0", "p1", "p2"}


# ------------------------------------------------------------------ admission


def test_a_new_attempt_needs_a_whole_attempt_and_its_settling_left():
    spend = budget(attempt_seconds=10.0, settle_seconds=5.0)

    assert spend.admit(Deadline(14)) == "EXIT_DEFERRED_RUN_BUDGET"
    assert spend.admit(Deadline(16)) is None
    assert budget(max_attempts=0).admit(Deadline(60)) == "EXIT_ATTEMPT_LIMIT_REACHED"
    assert budget(max_exits=0).admit(Deadline(60)) == "EXIT_BUDGET_REACHED"


async def test_deferred_positions_are_counted_by_reason_never_dropped():
    result = ExitSweep()
    spend = budget(max_attempts=0)

    await attempt_exits(
        [candidate("a", result), candidate("b", result)],
        spend,
        sessions=None,  # type: ignore[arg-type]
    )

    assert result.deferred == {"EXIT_ATTEMPT_LIMIT_REACHED": 2}
    assert spend.deferred == {"EXIT_ATTEMPT_LIMIT_REACHED": 2}
    assert result.attempts == 0


class Requests:
    def __init__(self, spent: int) -> None:
        self.spent = spent
        self.calls = 0

    async def refresh(self, trade_case, deadline, *, networks=None):
        self.calls += 1
        spent = self.spent

        class Reading:
            ready = True
            reason = None
            provider_requests = spent

        return Reading()


async def test_refreshes_stop_before_the_phase_could_pass_its_request_ceiling():
    port = Requests(spent=3)
    shared = RefreshBudget(5, max_provider_requests=5, per_refresh_requests=3)
    context = RefreshContext(port=port, budget=shared)

    assert (await context.observe(None))[0] is None  # type: ignore[arg-type]
    assert (await context.observe(None))[0] == "PRE_EXIT_PROVIDER_BUDGET_REACHED"  # type: ignore[arg-type]
    assert (port.calls, shared.provider_requests) == (1, 3)


# ---------------------------------------------------- one attempt's outcome


async def test_a_replayed_sale_is_counted_as_replayed_never_as_a_new_one(risk_db, now, trace):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)
    service = sweeper(sessions, at, feed)
    result = ExitSweep()
    [planned] = await service.plan(result)
    # The same key's sale is made first, as a racing run would have made it.
    first = await service.exits.execute_position_exit(
        planned.position_id, request_key=planned.request_key
    )
    assert first.kind == "paper_exit_recorded"
    spend = budget()

    await attempt_exits([planned], spend, sessions=sessions)

    assert (result.executed, result.replayed, spend.exits) == (0, 1, 0), result
    assert len(await exits(sessions)) == 1


async def test_an_attempt_cut_off_after_its_booking_is_counted_as_booked(risk_db, now, trace):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)
    result = ExitSweep()
    [planned] = await sweeper(sessions, at, feed).plan(result)

    async def booked_then_stuck(context: RefreshContext) -> Any:
        await planned.start(context)
        await asyncio.Event().wait()

    spend = budget(attempt_seconds=1.0, settle_seconds=1.0)
    await attempt_exits(
        [replace(planned, start=booked_then_stuck)], spend, sessions=sessions, deadline=Deadline(10)
    )

    assert (result.executed, result.timed_out, result.unknown) == (1, 1, 0), result
    assert spend.exits == 1
    assert len(await exits(sessions)) == 1


async def test_an_attempt_cut_off_before_its_booking_is_unknown_and_retried_once(
    risk_db, now, trace
):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)
    result = ExitSweep()
    [planned] = await sweeper(sessions, at, feed).plan(result)

    async def stuck(context: RefreshContext) -> Any:
        await asyncio.Event().wait()

    spend = budget(attempt_seconds=0.3, settle_seconds=1.0)
    await attempt_exits(
        [replace(planned, start=stuck)], spend, sessions=sessions, deadline=Deadline(10)
    )

    # Not "failed": nothing confirms either way, and the report says so by name.
    assert (result.executed, result.timed_out, result.unknown) == (0, 1, 1), result
    assert result.refusals == {"EXIT_OUTCOME_UNKNOWN": 1}
    assert await exits(sessions) == []

    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, observed(later, price=Decimal("0.9"))).sweep()
    assert again.executed == 1
    assert len(await exits(sessions)) == 1


# --------------------------------------------------- stops before the chain


async def _stopped(sessions, at, feed, **overrides):
    read = Counted(fresh_read(at))
    service = sweeper(sessions, at, feed, read=read)
    service = replace(
        service,
        exits=build_exit_service(sessions, at, feed=feed, exit_read=read, **overrides),
    )
    return await service.sweep(), read


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"kill_switch": True}, "KILL_SWITCH_ENGAGED"),
        ({"trading_mode": TradingMode.OBSERVE}, "KILL_SWITCH_ENGAGED"),
        ({"pause": None}, "SYSTEM_STOP_UNREADABLE"),
    ],
    ids=["kill-switch", "observe", "stop-unreadable"],
)
async def test_a_known_stop_is_honoured_before_any_chain_read(risk_db, now, trace, overrides, code):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)

    result, read = await _stopped(sessions, at, feed, **overrides)

    assert read.calls == 0
    assert result.refusals == {code: 1}, result
    assert await exits(sessions) == []


async def test_a_paused_account_is_honoured_before_any_chain_read(risk_db, now, trace):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)
    async with sessions.begin() as session:
        await session.execute(update(AccountRow).values(paused=True))

    result, read = await _stopped(sessions, at, feed)

    assert read.calls == 0
    assert result.refusals == {"SYSTEM_PAUSED": 1}, result
    assert await exits(sessions) == []


# ------------------------------------------------------------- configuration


def test_an_exit_run_must_leave_room_for_one_attempt_after_acquisition():
    from tests.runner.conftest import runner_settings

    with pytest.raises(ValidationError, match="PAPER_EXIT_RUN_MAX_SECONDS"):
        runner_settings(
            early_paper_exit_enabled=True,
            paper_exit_acquisition_max_seconds=40,
            paper_exit_attempt_max_seconds=25,
        )
    # Without an exit policy there is no exit phase to leave room for.
    runner_settings(paper_exit_acquisition_max_seconds=40, paper_exit_attempt_max_seconds=25)
    assert runner_settings(early_paper_exit_enabled=True).paper_exit_run_max_seconds == 60


async def test_a_judgement_stopped_between_positions_leaves_them_unevaluated_not_deferred(
    risk_db, now, trace
):
    _, sessions = risk_db
    at, feed = await falling(sessions, now, trace)
    result = ExitSweep()

    planned = await sweeper(sessions, at, feed).plan(result, deadline=Deadline(0))

    assert planned == []
    assert (result.evaluated, result.unevaluated, result.deferred) == (0, 1, {}), result
    assert result.plan_stop == "EXIT_PLAN_TIME_BUDGET_REACHED"


# ------------------------------------------------- what a refusal counts as


@pytest.mark.parametrize(
    ("reason", "blocked"),
    [
        ("EXIT_TRIGGER_CLEARED", 0),
        ("POSITION_ALREADY_CLOSED", 0),
        ("POSITION_NOT_FOUND", 0),
        ("EXIT_READ_UNAVAILABLE", 1),
        ("EXIT_RISK_REFUSED", 1),
        ("PRE_EXIT_REFRESH_FAILED", 1),
        ("SYSTEM_PAUSED", 1),
    ],
)
async def test_a_refused_triggered_exit_is_blocked_unless_nothing_is_left_to_sell(reason, blocked):
    """Only a gone trigger or a gone holding leaves nothing to sell; anything else is blocked."""
    from src.orchestration.paperexit.models import ExitRefusal, ExitRefused

    sweep = ExitSweep()
    one = candidate("refused", sweep)

    async def start(context: RefreshContext) -> Any:
        return ExitRefused(reason=ExitRefusal(reason), position_id=one.position_id)

    await attempt_exits([replace(one, start=start)], budget(), sessions=None)

    assert (sweep.attempts, sweep.blocked, sweep.refusals) == (1, blocked, {reason: 1})
    assert sweep.failed == 0
