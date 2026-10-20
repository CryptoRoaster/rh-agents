"""Discovery coverage: an offline model of the new-pool list, and measured coverage.

`simulate_discovery` replays a sequence of polls against a newest-first pool
list under the scout's own page rule: page 1 of 20; page 2 once, only when
page 1 was full and shared no pool with anything seen before, only after the
first poll (which has no reference) and only within the page-2 budget. It is a
deterministic model for offline regression, never evidence about the provider.

`measured_coverage` compares what was observed with a reference set of pools
known to have been created. Without a reference — or with an empty one — the
total coverage is UNKNOWN: discovery alone cannot see what it missed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

PAGE_SIZE = 20


@dataclass(frozen=True)
class Simulation:
    created: int
    captured: int
    # Polls whose pages never reached a pool seen before: pools may be missing.
    gap_polls: int
    page2_reads: int
    # `new_pools` requests, page 1 and page 2 together.
    requests: int


def simulate_discovery(
    *, per_poll: Sequence[int], page2: bool, page2_per_day: int, page_size: int = PAGE_SIZE
) -> Simulation:
    """`per_poll[i]` new pools appear before poll i; one simulated day at most."""
    pools: list[int] = []  # newest first
    seen: set[int] = set()
    gap_polls = page2_reads = requests = 0
    for poll, count in enumerate(per_poll):
        start = len(pools)
        pools[:0] = reversed(range(start, start + count))
        first = pools[:page_size]
        requests += 1
        reached = bool(seen.intersection(first))
        full = len(first) == page_size
        captured = set(first)
        if poll > 0 and full and not reached:
            if page2 and page2_reads < page2_per_day:
                second = pools[page_size : 2 * page_size]
                requests += 1
                page2_reads += 1
                reached = bool(seen.intersection(second))
                captured.update(second)
            if not reached:
                gap_polls += 1
        seen |= captured
    return Simulation(
        created=len(pools),
        captured=len(seen),
        gap_polls=gap_polls,
        page2_reads=page2_reads,
        requests=requests,
    )


@dataclass(frozen=True)
class Coverage:
    status: Literal["KNOWN", "UNKNOWN"]
    ratio: Decimal | None
    missing: int | None
    outside_reference: int | None


def measured_coverage(*, reference: set[str] | None, observed: set[str]) -> Coverage:
    if not reference:
        return Coverage("UNKNOWN", None, None, None)
    hit = len(reference & observed)
    ratio = (Decimal(hit) / Decimal(len(reference))).quantize(Decimal("0.0001")).normalize()
    return Coverage("KNOWN", ratio, len(reference) - hit, len(observed - reference))
