"""One monotonic budget for a whole attempt, cleanup included.

The clock starts before the process is spawned, so spawning, writing stdin,
reading both pipes, parsing, schema validation and domain validation all draw
from the same budget. A per-step timeout would let a slow start and a slow read
each stay "inside" their own limit while the attempt as a whole ran long.

A slice of the budget is reserved up front for cleanup. Work never gets that
slice, so terminating and reaping the child does not have to borrow time that
has already run out. The reserve is a reservation, not a guarantee: the
operating system is under no obligation to finish within it, which is why
`Deadline.cleanup_exhausted` exists and why an overrun is reported rather than
hidden.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_WORK_SECONDS = 0.05


@dataclass
class Deadline:
    """Remaining-time arithmetic for one attempt.

    `remaining_for_work` is what may still be spent on the attempt itself and
    excludes the cleanup reserve. `remaining_for_cleanup` is what is left for
    termination once work has stopped, however work ended.
    """

    total_seconds: float
    cleanup_reserve_seconds: float
    monotonic: Callable[[], float] = field(default=time.monotonic)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        if self.total_seconds <= 0:
            raise ValueError("A deadline needs a positive total budget")
        if self.cleanup_reserve_seconds < 0:
            raise ValueError("A cleanup reserve cannot be negative")
        if self.total_seconds - self.cleanup_reserve_seconds < MIN_WORK_SECONDS:
            raise ValueError("The cleanup reserve leaves no time for work")
        self.started_at = self.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return self.monotonic() - self.started_at

    @property
    def elapsed_ms(self) -> int:
        return int(self.elapsed_seconds * 1000)

    @property
    def remaining_for_work(self) -> float:
        budget = self.total_seconds - self.cleanup_reserve_seconds - self.elapsed_seconds
        return max(0.0, budget)

    @property
    def work_exhausted(self) -> bool:
        return self.remaining_for_work <= 0.0

    @property
    def remaining_for_cleanup(self) -> float:
        # Cleanup may use its reserve plus any work time the attempt did not
        # need, but never more than the total budget.
        return max(0.0, self.total_seconds - self.elapsed_seconds)

    @property
    def cleanup_exhausted(self) -> bool:
        return self.remaining_for_cleanup <= 0.0
