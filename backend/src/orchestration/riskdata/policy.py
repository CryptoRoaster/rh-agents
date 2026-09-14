"""Versioned bounds on when a recorded market fact still counts as current."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class RiskDataPolicy:
    version: str
    # How old a recorded market observation may be. Ninety seconds, matching
    # `ANCHOR_EXECUTION_V1.max_reference_age` and `PAPER_SIZING_V1.max_price_age`
    # because it is the same fact from the same recorder at the same cadence.
    # Three different numbers for one question would mean three different
    # opinions about when a price stops describing a market, and the loosest
    # would win by accident.
    max_price_age: timedelta

    def __post_init__(self) -> None:
        if self.max_price_age <= timedelta(0):
            raise ValueError("Freshness tolerance must be positive")


RISK_DATA_V1 = RiskDataPolicy(version="risk-data-v1", max_price_age=timedelta(seconds=90))
