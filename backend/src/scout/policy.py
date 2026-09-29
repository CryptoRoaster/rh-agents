"""EARLY_SCOUT_V1: when a watch is looked at, and when it matures.

Two schedules, both fixed offsets from `first_seen_at` and nothing else.

**ORBIT reviews** at T+0, 1h, 3h, 6h, 12h and 24h. No classification moves a
checkpoint: an INTERESTING answer does not earn more calls and NOT_INTERESTING
does not earn fewer, because the point of the timeline is to *measure* how a
young market develops, and a schedule that reacted to an early model opinion
would turn that opinion into a selection bias. A review that runs late takes
exactly one assessment on the current reading and moves on to the next future
checkpoint; missed checkpoints are never replayed in a burst.

**History checks** at T+24h, 48h and 72h. A structural VECTOR check — one OHLCV
read and `assess(...)` under the unchanged `VECTOR_SETUP_V1` — and never a
model call. Sufficient makes a watch PROMOTABLE. Still insufficient after the
last checkpoint makes it DORMANT: kept, with its history, and no longer asked
about.

The policy is code, versioned, and not configurable. Budgets decide how much
work one run may do; they never decide what is due.

**EARLY_SCOUT_V2: fresh first review only.** Under the measured intake (about
nine hundred new watches a day against a fixed ORBIT budget of ninety-six) the
V1 schedule is unfulfillable, and serving it oldest-first spent every call on
markets more than a day old. V2 keeps one ORBIT review per watch — its first —
and only inside a freshness window after discovery. A watch not reviewed within
the window is closed for ORBIT as `ORBIT_FIRST_REVIEW_SKIPPED_STALE`, without a
model call; the watch itself, its history checks, JEV and outcomes continue.
Time-based follow-ups are not scheduled; open V1 follow-ups are closed as
deferred. Which fresh watches are reviewed is decided by a keyed hash of the
market identity, never by any market figure or model opinion.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from src.agents.vector.policy import VECTOR_SETUP_V1, VectorSetupPolicy


class WatchStatus(StrEnum):
    """The operational lifecycle of a watch. Never an opinion about the market."""

    # Being looked at on the schedule.
    WATCHING = "WATCHING"
    # VECTOR-sufficient history exists: a full TradeCase may be formed by a
    # later, separate PAPER run. Not an approval of anything.
    PROMOTABLE = "PROMOTABLE"
    # Never matured within the history window. Kept, not deleted, and no longer
    # the cause of any provider or model traffic.
    DORMANT = "DORMANT"
    # The market can no longer be addressed as the same market: its stored
    # identity was contradicted by the provider. Fail closed.
    RETIRED = "RETIRED"


class OrbitState(StrEnum):
    """What became of a watch's ORBIT review debt. Absent means still pending."""

    # The first review was taken (whatever its outcome: the slot was spent).
    REVIEWED = "REVIEWED"
    # Not selected within the freshness window: closed without a model call.
    FIRST_REVIEW_SKIPPED_STALE = "ORBIT_FIRST_REVIEW_SKIPPED_STALE"
    # Reviewed under V1 with time-based follow-ups still open: closed, not taken.
    FOLLOW_UPS_DEFERRED = "ORBIT_FOLLOW_UPS_DEFERRED"


# Statuses whose remaining ORBIT checkpoints are still honoured.
REVIEWABLE = frozenset({WatchStatus.WATCHING, WatchStatus.PROMOTABLE})


@dataclass(frozen=True)
class OrbitReview:
    """Which checkpoint a review taken now satisfies, and when the next is due."""

    checkpoint_index: int
    checkpoint: timedelta
    next_review_at: datetime | None


@dataclass(frozen=True)
class HistoryOutcome:
    status: WatchStatus
    next_review_at: datetime | None


@dataclass(frozen=True)
class EarlyScoutPolicy:
    version: str
    orbit_checkpoints: tuple[timedelta, ...]
    history_checkpoints: tuple[timedelta, ...]
    vector: VectorSetupPolicy
    # V2: the first review is only taken this soon after discovery; after it the
    # review debt is closed as stale. None keeps V1's unbounded schedule.
    first_review_window: timedelta | None = None

    def __post_init__(self) -> None:
        for checkpoints in (self.orbit_checkpoints, self.history_checkpoints):
            if not checkpoints or list(checkpoints) != sorted(set(checkpoints)):
                raise ValueError("Checkpoints must be distinct and ascending")
            if checkpoints[0] < timedelta(0):
                raise ValueError("A checkpoint cannot precede discovery")
        if self.first_review_window is not None and (
            self.first_review_window <= timedelta(0) or len(self.orbit_checkpoints) != 1
        ):
            raise ValueError("A freshness window needs exactly one first-review checkpoint")

    @property
    def fresh_first_only(self) -> bool:
        return self.first_review_window is not None

    def first_orbit_review_at(self, first_seen_at: datetime) -> datetime:
        return first_seen_at + self.orbit_checkpoints[0]

    def first_history_review_at(self, first_seen_at: datetime) -> datetime:
        return first_seen_at + self.history_checkpoints[0]

    def orbit_review(self, first_seen_at: datetime, now: datetime) -> OrbitReview:
        """The one review a watch receives now, however many checkpoints it missed."""
        elapsed = max(now - first_seen_at, timedelta(0))
        index = max(
            position
            for position, checkpoint in enumerate(self.orbit_checkpoints)
            if checkpoint <= elapsed or position == 0
        )
        following = next((item for item in self.orbit_checkpoints if item > elapsed), None)
        return OrbitReview(
            checkpoint_index=index,
            checkpoint=self.orbit_checkpoints[index],
            next_review_at=None if following is None else first_seen_at + following,
        )

    def history_outcome(
        self, first_seen_at: datetime, now: datetime, *, sufficient: bool
    ) -> HistoryOutcome:
        """What one history check at `now` means for the watch.

        Maturity is age *and* structure: a series that already looks sufficient
        before the first history checkpoint does not promote a watch early.
        """
        elapsed = now - first_seen_at
        if elapsed < self.history_checkpoints[0]:
            return HistoryOutcome(WatchStatus.WATCHING, self.first_history_review_at(first_seen_at))
        if sufficient:
            return HistoryOutcome(WatchStatus.PROMOTABLE, None)
        following = next((item for item in self.history_checkpoints if item > elapsed), None)
        if following is None:
            return HistoryOutcome(WatchStatus.DORMANT, None)
        return HistoryOutcome(WatchStatus.WATCHING, first_seen_at + following)


EARLY_SCOUT_V1 = EarlyScoutPolicy(
    version="early-scout-v1",
    orbit_checkpoints=tuple(timedelta(hours=hours) for hours in (0, 1, 3, 6, 12, 24)),
    history_checkpoints=tuple(timedelta(hours=hours) for hours in (24, 48, 72)),
    vector=VECTOR_SETUP_V1,
)


EARLY_SCOUT_V2 = EarlyScoutPolicy(
    version="early-scout-v2",
    orbit_checkpoints=(timedelta(0),),
    history_checkpoints=EARLY_SCOUT_V1.history_checkpoints,
    vector=VECTOR_SETUP_V1,
    first_review_window=timedelta(minutes=60),
)
