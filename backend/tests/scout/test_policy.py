"""EARLY_SCOUT_V1: when a watch is looked at, independent of what was seen.

Pure schedule arithmetic. The checkpoints are fixed offsets from `first_seen_at`
and nothing a model says moves them: a classification is a measurement to be
compared across checkpoints, and letting it change the schedule would turn an
early model error into a selection bias.
"""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from src.scout.policy import EARLY_SCOUT_V1, WatchStatus

FIRST = datetime(2026, 9, 26, 6, tzinfo=UTC)
H = timedelta(hours=1)


def test_the_policy_is_versioned_and_states_its_checkpoints():
    assert EARLY_SCOUT_V1.version == "early-scout-v1"
    assert [int(item.total_seconds()) for item in EARLY_SCOUT_V1.orbit_checkpoints] == [
        0,
        3600,
        10800,
        21600,
        43200,
        86400,
    ]
    assert [int(item.total_seconds()) for item in EARLY_SCOUT_V1.history_checkpoints] == [
        86400,
        172800,
        259200,
    ]


def test_t0_orbit_is_due_immediately():
    assert EARLY_SCOUT_V1.first_orbit_review_at(FIRST) == FIRST


@pytest.mark.parametrize(
    ("elapsed", "index", "checkpoint", "following"),
    [
        (timedelta(0), 0, timedelta(0), 1 * H),
        (1 * H, 1, 1 * H, 3 * H),
        (3 * H, 2, 3 * H, 6 * H),
        (6 * H, 3, 6 * H, 12 * H),
        (12 * H, 4, 12 * H, 24 * H),
        (24 * H, 5, 24 * H, None),
    ],
    ids=["t0", "one_hour", "three_hour", "six_hour", "twelve_hour", "twenty_four_hour"],
)
def test_each_checkpoint_leads_to_the_next(elapsed, index, checkpoint, following):
    review = EARLY_SCOUT_V1.orbit_review(FIRST, FIRST + elapsed)
    assert review.checkpoint_index == index
    assert review.checkpoint == checkpoint
    assert review.next_review_at == (None if following is None else FIRST + following)


def test_missed_checkpoints_collapse_into_one_review():
    """At T+4h the 1h and 3h checkpoints are both past: one review, then T+6h."""
    review = EARLY_SCOUT_V1.orbit_review(FIRST, FIRST + 4 * H)
    assert review.checkpoint_index == 2
    assert review.checkpoint == 3 * H
    assert review.next_review_at == FIRST + 6 * H


def test_a_review_long_after_the_last_checkpoint_ends_the_schedule():
    review = EARLY_SCOUT_V1.orbit_review(FIRST, FIRST + 30 * H)
    assert review.checkpoint_index == 5
    assert review.next_review_at is None


def test_the_schedule_cannot_see_a_classification():
    """No parameter exists through which an ORBIT verdict could move a checkpoint."""
    for method in (EARLY_SCOUT_V1.orbit_review, EARLY_SCOUT_V1.history_outcome):
        names = set(inspect.signature(method).parameters)
        assert not names & {"classification", "strength", "assessment", "interesting"}


def test_history_is_first_checked_at_twenty_four_hours():
    assert EARLY_SCOUT_V1.first_history_review_at(FIRST) == FIRST + 24 * H


@pytest.mark.parametrize(
    ("elapsed", "status", "following"),
    [
        (24 * H, WatchStatus.WATCHING, 48 * H),
        (48 * H, WatchStatus.WATCHING, 72 * H),
        (72 * H, WatchStatus.DORMANT, None),
        (90 * H, WatchStatus.DORMANT, None),
    ],
)
def test_insufficient_history_waits_then_goes_dormant(elapsed, status, following):
    outcome = EARLY_SCOUT_V1.history_outcome(FIRST, FIRST + elapsed, sufficient=False)
    assert outcome.status is status
    assert outcome.next_review_at == (None if following is None else FIRST + following)


@pytest.mark.parametrize("elapsed", [24 * H, 48 * H, 72 * H])
def test_sufficient_history_makes_a_watch_promotable(elapsed):
    outcome = EARLY_SCOUT_V1.history_outcome(FIRST, FIRST + elapsed, sufficient=True)
    assert outcome.status is WatchStatus.PROMOTABLE
    assert outcome.next_review_at is None


def test_history_before_maturity_never_promotes():
    """A structurally sufficient series before T+24h is not maturity."""
    outcome = EARLY_SCOUT_V1.history_outcome(FIRST, FIRST + 23 * H, sufficient=True)
    assert outcome.status is WatchStatus.WATCHING
    assert outcome.next_review_at == FIRST + 24 * H


def test_the_policy_uses_the_unchanged_vector_contract():
    from src.agents.vector.policy import VECTOR_SETUP_V1

    assert EARLY_SCOUT_V1.vector is VECTOR_SETUP_V1
    assert VECTOR_SETUP_V1.min_closed_bars == 24
    assert VECTOR_SETUP_V1.history_bars == 48
    assert VECTOR_SETUP_V1.history_timeframe == "hour"
    assert VECTOR_SETUP_V1.history_aggregate == 1
