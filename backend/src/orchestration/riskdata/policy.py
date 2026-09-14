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
    # How old the base asset's recorded symbol and decimals may be.
    #
    # A separate field rather than a reuse of the one above, because SENTINEL
    # checks the token snapshot's own age independently of the market
    # snapshot's — a bound inherited by accident would be a contract nobody
    # chose. The value matches, because the market layer re-observes pair
    # metadata as part of the same snapshot at the same cadence, so a tighter
    # bound would refuse every reading the recorder can produce rather than
    # catch anything.
    #
    # Necessary, not sufficient. SENTINEL applies its own tolerance —
    # `RiskLimits.max_snapshot_age_seconds`, configurable and tighter by default
    # — when it evaluates, so data reported complete here can still be refused
    # there. What this bound guarantees is the other direction: nothing stale by
    # the recorder's own cadence is ever reported as present.
    max_token_metadata_age: timedelta

    def __post_init__(self) -> None:
        if self.max_price_age <= timedelta(0) or self.max_token_metadata_age <= timedelta(0):
            raise ValueError("Freshness tolerances must be positive")


RISK_DATA_V1 = RiskDataPolicy(
    version="risk-data-v1",
    max_price_age=timedelta(seconds=90),
    max_token_metadata_age=timedelta(seconds=90),
)
