"""Which due VECTOR history checks a scout run takes, and when a failed one may retry.

**Two lanes.** CURRENT holds checkpoints that fell due within the last hour and
is served newest first, so a watch reaching its 24/48/72h checkpoint is checked
close to it instead of behind every overdue check. CATCH-UP holds everything
older and is served oldest first, so the backlog is always worked from its
head. With six slots a run takes five CURRENT and one CATCH-UP; a lane with
fewer candidates hands its slots to the other. Within a lane the chains are
interleaved, so neither can crowd the other out.

Nothing here reads a price, liquidity, volume, momentum, JEV answer or ORBIT
classification: the order is due time, chain and pair identity only.

**Bounded retry.** A failed read sets a retry-not-before instant apart from the
checkpoint, after 15, 30, 60 and then at most 120 minutes, so a failing watch
can never hold the head of the queue run after run. A successful check clears
it. `next_history_review_at` keeps meaning the 24/48/72h checkpoint throughout.
"""

from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum

from src.scout.models import DiscoveryWatch

CURRENT_WINDOW = timedelta(hours=1)
CATCH_UP_SLOTS = 1
BACKOFF = (
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(minutes=60),
    timedelta(minutes=120),
)
MAX_FAILURE_COUNT = 10
# Our own request limit ran out: not the watch's failure, so no backoff.
BUDGET_EXHAUSTED = "MARKET_HISTORY_REQUEST_BUDGET_EXHAUSTED"
RATE_LIMITED = "MARKET_HISTORY_PROVIDER_RATE_LIMITED"


class Lane(StrEnum):
    CURRENT = "CURRENT"
    CATCH_UP = "CATCH_UP"


def backoff_after(failures: int) -> timedelta:
    """The pause after the `failures`-th consecutive failed read (1-based), bounded."""
    return BACKOFF[min(max(failures, 1), len(BACKOFF)) - 1]


def interleave[T](by_chain: dict[str, list[T]]) -> list[T]:
    """Round-robin across chains in name order, each chain keeping its own order."""
    queues = [list(by_chain[name]) for name in sorted(by_chain)]
    merged: list[T] = []
    while any(queues):
        for queue in queues:
            if queue:
                merged.append(queue.pop(0))
    return merged


def pick(
    current: Sequence[DiscoveryWatch], catch_up: Sequence[DiscoveryWatch], slots: int
) -> list[tuple[DiscoveryWatch, Lane]]:
    """Five CURRENT to one CATCH-UP for six slots; an empty lane lends its slots."""
    if slots <= 0:
        return []
    reserved = min(CATCH_UP_SLOTS, slots - 1) if slots > 1 else 0
    take_current = min(len(current), slots - min(reserved, len(catch_up)))
    take_catch_up = min(len(catch_up), slots - take_current)
    return [(watch, Lane.CURRENT) for watch in current[:take_current]] + [
        (watch, Lane.CATCH_UP) for watch in catch_up[:take_catch_up]
    ]
