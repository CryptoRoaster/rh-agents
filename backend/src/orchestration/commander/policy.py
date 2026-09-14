"""Versioned COMMANDER control policy. Operational bounds, never strategy.

Everything here decides *whether the machinery may act*, never *whether a trade
is worth making*. The distinction is the reason the file is short: a coordinator
that could prefer one candidate over another on market grounds would be a
strategy competing with ORBIT, which exists precisely to judge whether a
candidate is interesting.

So there is no ranking by liquidity, no momentum filter, no hype threshold and
no scoring of any kind. What is bounded is traffic, freshness, identity and
duplication — the things an operator tunes, not the things an analyst decides.
"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class CommanderControlPolicy:
    version: str
    # Chains the system is willing to open cases on at all. An allow-list rather
    # than a deny-list: a chain nobody has verified support for is not a chain to
    # discover opportunities on by default.
    enabled_chains: frozenset[str]
    # How old a recorded candidate may be, measured from the market's own
    # observation time rather than from when we fetched it. A candidate from an
    # hour ago describes a market that has moved on, and opening a case from it
    # would start a workflow against a picture nobody holds any more.
    max_candidate_age: timedelta
    # The ceiling on new cases one intake cycle may open. A provider burst is a
    # fact about a provider, not a sudden abundance of opportunity, and without
    # a bound one would become thousands of TradeCases in a single pass.
    max_cases_per_cycle: int
    # How long an opened case may live before the workflow expires it.
    case_lifetime: timedelta
    # Whether fixture markets may open cases. False everywhere real: a synthetic
    # market must never be able to start a real workflow.
    allow_fixtures: bool

    def __post_init__(self) -> None:
        if not self.enabled_chains:
            raise ValueError("Intake needs at least one enabled chain")
        if self.max_candidate_age <= timedelta(0):
            raise ValueError("Candidate age bound must be positive")
        if not 1 <= self.max_cases_per_cycle <= 50:
            raise ValueError("One intake cycle must stay bounded")
        if self.case_lifetime <= timedelta(0):
            raise ValueError("A case must be opened before its expiry")


# Provisional PAPER-mode control policy.
#
# The two supported chains, a candidate freshness window matching the market
# layer's own recording cadence, and a small per-cycle bound. None of these is a
# view on what to trade; they are the shape of the tap, not the water.
COMMANDER_CONTROL_V1 = CommanderControlPolicy(
    version="commander-control-v1",
    enabled_chains=frozenset({"robinhood", "bsc"}),
    max_candidate_age=timedelta(minutes=5),
    max_cases_per_cycle=5,
    case_lifetime=timedelta(hours=6),
    allow_fixtures=False,
)
