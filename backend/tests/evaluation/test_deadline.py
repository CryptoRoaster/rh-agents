"""One monotonic budget, with cleanup time reserved before work starts."""

import pytest

from src.evaluation.codex.deadline import Deadline


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_work_never_sees_the_cleanup_reserve() -> None:
    clock = Clock()
    deadline = Deadline(total_seconds=20.0, cleanup_reserve_seconds=2.0, monotonic=clock)
    assert deadline.remaining_for_work == pytest.approx(18.0)
    clock.advance(17.0)
    assert deadline.remaining_for_work == pytest.approx(1.0)
    clock.advance(1.5)
    assert deadline.work_exhausted
    # Work is out of time, cleanup is not.
    assert deadline.remaining_for_cleanup == pytest.approx(1.5)


def test_cleanup_may_reclaim_unused_work_time() -> None:
    clock = Clock()
    deadline = Deadline(total_seconds=20.0, cleanup_reserve_seconds=2.0, monotonic=clock)
    clock.advance(5.0)
    assert deadline.remaining_for_cleanup == pytest.approx(15.0)


def test_cleanup_exhaustion_is_reported_not_hidden() -> None:
    clock = Clock()
    deadline = Deadline(total_seconds=5.0, cleanup_reserve_seconds=1.0, monotonic=clock)
    clock.advance(5.5)
    assert deadline.work_exhausted
    assert deadline.cleanup_exhausted
    assert deadline.remaining_for_cleanup == 0.0


def test_a_reserve_that_starves_work_is_rejected() -> None:
    with pytest.raises(ValueError):
        Deadline(total_seconds=2.0, cleanup_reserve_seconds=2.0)
    with pytest.raises(ValueError):
        Deadline(total_seconds=0.0, cleanup_reserve_seconds=0.0)
    with pytest.raises(ValueError):
        Deadline(total_seconds=5.0, cleanup_reserve_seconds=-1.0)
