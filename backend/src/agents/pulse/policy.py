"""Versioned deterministic PULSE monitoring policy.

Everything here is a data-quality or operational bound. None of it is a trading
view: there is no threshold that makes a trigger more or less likely, no
confirmation rule, no smoothing and no filter on which crossings count. The
condition is VECTOR's and the comparison is exact.

Two numbers do the work. **Freshness** decides how recent an observation must be
before it may be called a statement about now. **Cadence** decides how often to
look, and is bounded below by how often the data it reads actually changes —
checking faster than the market layer records would burn attempts to re-read the
same number.
"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class PulseTriggerPolicy:
    version: str
    # A trigger is a claim that the market is at a level *now*. VECTOR's five
    # minutes is deliberately not copied: a setup generator reasons about where
    # levels are and tolerates a slightly older picture, while a monitor asserts
    # that a level has just been reached. Two minutes covers one market-watcher
    # interval plus slack, so an ordinary missed tick does not stall the watch
    # while a genuinely stalled feed cannot produce a trigger.
    max_observation_age: timedelta
    # How long to wait before looking again. The market layer records on a
    # ninety-second cadence and the upstream provider caches for a minute, so
    # anything faster re-reads a number that cannot have changed.
    poll_interval: timedelta
    # A provider's clock and ours can disagree slightly. Beyond this an
    # observation timestamped in the future is a fault rather than skew, and a
    # future-dated price must never satisfy a condition.
    max_clock_skew: timedelta

    def __post_init__(self) -> None:
        if self.max_observation_age <= timedelta(0):
            raise ValueError("Observation age tolerance must be positive")
        if self.poll_interval <= timedelta(0):
            raise ValueError("The poll interval must be positive")
        if self.max_clock_skew < timedelta(0):
            raise ValueError("Clock skew tolerance cannot be negative")
        if self.max_clock_skew >= self.max_observation_age:
            # Otherwise an observation could be too far in the future to trust
            # and still inside the freshness window, and the two rules would
            # disagree about the same timestamp.
            raise ValueError("Skew tolerance must stay below the freshness window")
        if self.poll_interval > self.max_observation_age:
            # Checking less often than data goes stale would guarantee that most
            # checks see an observation they must refuse.
            raise ValueError("Polling must be at least as frequent as data goes stale")


PULSE_TRIGGER_V1 = PulseTriggerPolicy(
    version="pulse-trigger-v1",
    max_observation_age=timedelta(minutes=2),
    poll_interval=timedelta(seconds=90),
    max_clock_skew=timedelta(seconds=5),
)
