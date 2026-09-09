"""Trusted infrastructure time; clocks are injected at service construction only."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return timezone-aware current time."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.utcoffset() is None:
            raise ValueError("FixedClock requires timezone-aware time")

    def now(self) -> datetime:
        return self.instant.astimezone(UTC)
