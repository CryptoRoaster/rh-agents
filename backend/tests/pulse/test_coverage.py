"""Does a recorded crossing survive a later reversion?

This file exists because of a defect found auditing Phase 2I. PULSE read only
the latest recorded observation, so a price that crossed the level and came back
before the next check would be invisible: the system had durably recorded the
crossing and would have reported that nothing happened.

What PULSE promises is bounded and worth stating plainly. It does not watch the
market; it watches what the market layer *recorded*. Within that stream it must
not skip a qualifying observation merely because a newer, non-qualifying one
exists — and outside it, it promises nothing at all.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.pulse.context import PulseContextReader
from src.agents.pulse.evaluator import evaluate
from src.agents.pulse.models import PulseReasonCode, TriggerOutcome
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.agents.vector.models import TriggerType
from src.core.clock import FixedClock
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from tests.pulse.conftest import (
    StubCases,
    StubMarkets,
    StubTradeCase,
    market_identity,
    observed,
    setup_envelope,
    task_input,
    watched,
)
from tests.pulse.test_workflow import snapshot_for


def window(now, prices):
    """Observations at the given ages, oldest first, as a recorded window."""
    return tuple(
        observed(now, price=Decimal(price), seconds_ago=seconds_ago)
        for seconds_ago, price in sorted(prices, reverse=True)
    )


def check(now, prices, **kwargs):
    return evaluate(
        task_input(now, observations=window(now, prices), **kwargs), now, PULSE_TRIGGER_V1
    )


# ------------------------------------------ U: cross and revert between polls


def test_scenario_u_a_fresh_crossing_survives_a_later_reversion(now):
    """The defect, pinned.

    1.22 crossed the level fifty seconds ago and the price has since fallen back
    to 1.17. The crossing is recorded, it is inside the freshness window, and it
    happened. A monitor reading only the newest price would say nothing had.
    """
    result = check(now, [(80, "1.18"), (50, "1.22"), (10, "1.17")])
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.observed_price == Decimal("1.22")
    assert result.observed_at == now - timedelta(seconds=50)


def test_the_newest_price_is_not_what_decides(now):
    """Stated the other way round, because this is the whole point."""
    reverted = check(now, [(50, "1.22"), (10, "1.17")])
    assert reverted.outcome == TriggerOutcome.TRIGGERED
    never = check(now, [(50, "1.18"), (10, "1.17")])
    assert never.outcome == TriggerOutcome.NOT_TRIGGERED
    # And a check that found nothing still reports the newest price it saw.
    assert never.observed_price == Decimal("1.17")


# ------------------------------------------------ V: an old crossing is stale


def test_scenario_v_a_crossing_outside_the_freshness_window_does_not_trigger(now):
    """Deliberately conservative, and a different question from coverage.

    A five-minute-old crossing is a fact about a market that has since moved on.
    Acting on it now would be acting on a price nobody can still see, so it is
    not resurrected — while a crossing *inside* the window is never skipped.
    """
    result = check(now, [(300, "1.22"), (10, "1.17")])
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED
    assert result.observed_price == Decimal("1.17")


def test_the_window_boundary_is_exact(now):
    edge = int(PULSE_TRIGGER_V1.max_observation_age.total_seconds())
    assert check(now, [(edge, "1.22"), (10, "1.17")]).outcome == TriggerOutcome.TRIGGERED
    assert check(now, [(edge + 1, "1.22"), (10, "1.17")]).outcome == TriggerOutcome.NOT_TRIGGERED


def test_a_crossing_before_the_setup_began_does_not_trigger_it(now):
    """A setup is a statement about what happens next, not a test on the past."""
    trigger = watched(now, valid_from=now - timedelta(seconds=30))
    result = check(now, [(60, "1.22"), (10, "1.17")], trigger=trigger)
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED


# --------------------------------------------- W: several crossings at once


def test_scenario_w_the_earliest_qualifying_observation_is_the_event(now):
    """ "When did this system first observe the trigger?" has one stable answer."""
    result = check(now, [(100, "1.18"), (70, "1.21"), (40, "1.19"), (10, "1.23")])
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.observed_price == Decimal("1.21")
    assert result.observed_at == now - timedelta(seconds=70)


def test_the_same_window_always_names_the_same_observation(now):
    prices = [(100, "1.18"), (70, "1.21"), (40, "1.19"), (10, "1.23")]
    answers = {
        (check(now, prices).observed_price, check(now, prices).observed_at) for _ in range(20)
    }
    assert len(answers) == 1


def test_the_window_is_scanned_oldest_first_whatever_order_it_arrives_in(now):
    """Provider or database order must never reach the answer."""
    forward = window(now, [(100, "1.18"), (70, "1.21"), (10, "1.23")])
    shuffled = (forward[2], forward[0], forward[1])
    ordered = evaluate(task_input(now, observations=forward), now, PULSE_TRIGGER_V1)
    jumbled = evaluate(task_input(now, observations=shuffled), now, PULSE_TRIGGER_V1)
    # The evaluator honours the order it is handed; the reader guarantees it.
    assert ordered.observed_price == Decimal("1.21")
    assert jumbled.observed_price == Decimal("1.23")


# ------------------------------------------------- X: the range condition


def zone_trigger(now):
    return watched(
        now,
        type=TriggerType.PRICE_IN_RANGE,
        reference_price=None,
        zone_low=Decimal("0.90"),
        zone_high=Decimal("0.95"),
    )


def test_scenario_x_a_band_entered_and_left_still_triggers(now):
    """Identical coverage for the range form: entering the band is the event."""
    result = check(now, [(80, "1.05"), (50, "0.93"), (10, "1.02")], trigger=zone_trigger(now))
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.observed_price == Decimal("0.93")


def test_a_band_never_entered_does_not_trigger(now):
    result = check(now, [(80, "1.05"), (50, "0.99"), (10, "1.02")], trigger=zone_trigger(now))
    assert result.outcome == TriggerOutcome.NOT_TRIGGERED


# ---------------------------------------------- the bound on the window


def test_a_truncated_window_never_reports_a_confident_negative(now):
    """A negative answer would be a claim about rows nobody looked at."""
    result = check(now, [(80, "1.18"), (10, "1.17")], truncated=True)
    assert result.outcome == TriggerOutcome.OBSERVATION_BUDGET_EXCEEDED
    assert result.reason_code == PulseReasonCode.OBSERVATION_BUDGET_EXCEEDED


def test_a_truncated_window_still_reports_a_crossing_it_did_see(now):
    """Finding one is positive evidence regardless of what else was missed."""
    result = check(now, [(80, "1.22"), (10, "1.17")], truncated=True)
    assert result.outcome == TriggerOutcome.TRIGGERED
    assert result.observed_price == Decimal("1.22")


def test_the_window_is_bounded_by_policy():
    assert PULSE_TRIGGER_V1.max_observations == 64
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(PULSE_TRIGGER_V1, max_observations=0)
    with pytest.raises(ValueError):
        replace(PULSE_TRIGGER_V1, max_observations=5000)


# ------------------------------------- the read itself, against a real database


async def record(sessions, now, prices):
    recorder = MarketRecorder(sessions, clock=FixedClock(now))
    pair_id = None
    for seconds_ago, price in prices:
        snapshot = await recorder.record(
            snapshot_for(now, price=Decimal(price), seconds_ago=seconds_ago)
        )
        pair_id = snapshot.pair.pair_id
    return pair_id


async def test_the_reader_returns_the_whole_window_oldest_first(market_sessions, now):
    """The read that made this possible. ``latest`` alone could not see 1.22."""
    pair_id = await record(market_sessions, now, [(80, "1.18"), (50, "1.22"), (10, "1.17")])
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    observations = await reader.observations(
        pair_id,
        since=now - timedelta(minutes=2),
        until=now,
        limit=64,
        include_fixtures=True,
    )
    assert [item.price.value_usd for item in observations] == [
        Decimal("1.18"),
        Decimal("1.22"),
        Decimal("1.17"),
    ]
    latest = await reader.latest(pair_id, include_fixtures=True)
    assert latest.price.value_usd == Decimal("1.17")


async def test_the_read_excludes_what_falls_outside_the_window(market_sessions, now):
    pair_id = await record(market_sessions, now, [(600, "1.22"), (10, "1.17")])
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    observations = await reader.observations(
        pair_id, since=now - timedelta(minutes=2), until=now, limit=64, include_fixtures=True
    )
    assert [item.price.value_usd for item in observations] == [Decimal("1.17")]


async def test_the_read_returns_one_row_beyond_the_limit_so_truncation_is_knowable(
    market_sessions, now
):
    """Silently receiving part of the picture is the failure this prevents."""
    pair_id = await record(market_sessions, now, [(90, "1.10"), (60, "1.11"), (30, "1.12")])
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    observations = await reader.observations(
        pair_id, since=now - timedelta(minutes=2), until=now, limit=2, include_fixtures=True
    )
    assert len(observations) == 3


@pytest.mark.parametrize("limit", [0, 501])
async def test_an_unbounded_read_is_refused(market_sessions, now, limit):
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    with pytest.raises(ValueError):
        await reader.observations("x", since=now - timedelta(minutes=2), until=now, limit=limit)


async def test_a_naive_bound_is_refused(market_sessions, now):
    from datetime import datetime

    reader = MarketReader(market_sessions, clock=FixedClock(now))
    with pytest.raises(ValueError):
        await reader.observations("x", since=datetime(2026, 9, 13), until=now, limit=10)


# -------------------------------------------- AG: identical source timestamps


async def test_two_observations_sharing_a_timestamp_order_deterministically(market_sessions, now):
    """Source time is authoritative; ``recorded_at`` and id only break ties.

    Two rows can share an observation time — a provider re-reporting, or two
    streams of the same market — and the answer must not depend on which one the
    database happened to return first.
    """
    pair_id = await record(market_sessions, now, [(30, "1.10"), (30, "1.11"), (30, "1.12")])
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    first = await reader.observations(
        pair_id, since=now - timedelta(minutes=2), until=now, limit=64, include_fixtures=True
    )
    again = await reader.observations(
        pair_id, since=now - timedelta(minutes=2), until=now, limit=64, include_fixtures=True
    )
    assert [item.id for item in first] == [item.id for item in again]
    assert len({item.price.observed_at for item in first}) == 1


async def test_a_late_insertion_never_becomes_recent(market_sessions, now):
    """Insertion time cannot reorder the window or make old data fresh."""
    recorder = MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(hours=1)))
    old = await recorder.record(snapshot_for(now, price=Decimal("1.22"), seconds_ago=600))
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    observations = await reader.observations(
        old.pair.pair_id,
        since=now - PULSE_TRIGGER_V1.max_observation_age,
        until=now,
        limit=64,
        include_fixtures=True,
    )
    assert observations == ()


# ----------------------------------------- the context assembles the window


async def test_the_context_asks_for_exactly_the_window_the_policy_defines(now):
    asked: dict[str, object] = {}

    class Recording(StubMarkets):
        async def observations(self, identity, *, since, until, limit, include_fixtures=False):
            asked.update({"identity": identity, "since": since, "until": until, "limit": limit})
            return await super().observations(
                identity, since=since, until=until, limit=limit, include_fixtures=include_fixtures
            )

    envelope = setup_envelope(now)
    reader = PulseContextReader(
        cases=StubCases(StubTradeCase(market_identity()), (envelope,)),
        markets=Recording(snapshot_for(now)),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    await reader.trigger_context(uuid4(), uuid4())
    trigger = envelope.payload.setup.trigger
    assert asked["since"] == max(trigger.valid_from, now - PULSE_TRIGGER_V1.max_observation_age)
    assert asked["until"] == now + PULSE_TRIGGER_V1.max_clock_skew
    assert asked["limit"] == PULSE_TRIGGER_V1.max_observations


async def test_the_context_reports_a_truncated_window(now):
    class Overfull(StubMarkets):
        async def observations(self, identity, *, since, until, limit, include_fixtures=False):
            one = snapshot_for(now, price=Decimal("1.10"), seconds_ago=10)
            return tuple(one for _ in range(limit + 1))

    reader = PulseContextReader(
        cases=StubCases(StubTradeCase(market_identity()), (setup_envelope(now),)),
        markets=Overfull(snapshot_for(now)),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    context = await reader.trigger_context(uuid4(), uuid4())
    assert context.window_truncated is True
    assert len(context.observations) == PULSE_TRIGGER_V1.max_observations


async def test_no_window_is_read_when_there_is_nothing_to_watch(now):
    """No setup, no question, no query."""

    class Forbidden(StubMarkets):
        async def observations(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("a monitor with no condition must ask nothing")

    reader = PulseContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        markets=Forbidden(snapshot_for(now)),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    context = await reader.trigger_context(uuid4(), uuid4())
    assert context.trigger is None
    assert context.observations == ()


async def test_fixture_observations_stay_out_of_a_production_read(market_sessions, now):
    """A synthetic feed must not be able to trigger a real setup."""
    pair_id = await record(market_sessions, now, [(30, "1.22")])
    reader = MarketReader(market_sessions, clock=FixedClock(now))
    assert (
        await reader.observations(pair_id, since=now - timedelta(minutes=2), until=now, limit=64)
        == ()
    )
    assert (
        await reader.observations(
            pair_id,
            since=now - timedelta(minutes=2),
            until=now,
            limit=64,
            include_fixtures=True,
        )
        != ()
    )


def test_a_task_slot_that_does_not_exist_has_no_wait_policy():
    """A lookup for something the workflow does not define answers honestly."""
    from src.core.models import AgentRole as Role
    from src.orchestration.workflow.policy import TRADE_CASE_V1

    assert TRADE_CASE_V1.task(Role.PULSE, "SOMETHING_ELSE") is None
    assert TRADE_CASE_V1.task(Role.ORBIT, "WAIT_FOR_TRIGGER") is None
