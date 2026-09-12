"""Versioned deterministic VECTOR setup policy.

This module decides whether a proposed setup is *coherent* — whether its geometry
means what its kind claims, whether its levels are plausibly about the market it
names, and whether it expires inside a sane horizon. It contains no model, reads
no prompt, and nothing a model returns can change what it concludes.

It is emphatically **not** a risk engine. There is no exposure limit here, no
position size, no daily loss cap, no cash check and no slippage tolerance —
those are SENTINEL's and ANCHOR's, and duplicating them here would create a
second opinion on questions that must have exactly one.

Nor is it a trading strategy. The price envelope below is a sanity bound, not an
edge: it exists so that a model which has lost track of the decimal point cannot
produce a setup, and its numbers are deliberately loose enough that no reasonable
proposal is refused by them.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.agents.vector.models import SetupKind, TriggerType
from src.core.models import Side

# Which trigger grammar each supported setup shape must use. The mapping is
# one-way and total: a kind that could arrive with either trigger would let a
# model choose its own validator.
REQUIRED_TRIGGER: dict[SetupKind, TriggerType] = {
    SetupKind.BREAKOUT_LONG: TriggerType.PRICE_GTE,
    SetupKind.PULLBACK_LONG: TriggerType.PRICE_IN_RANGE,
}


@dataclass(frozen=True)
class VectorSetupPolicy:
    """Structural constraints a proposal must satisfy to become evidence."""

    version: str
    # Long only, because the paper execution service is long-only
    # weighted-average-cost. A short setup would describe something this system
    # cannot do.
    supported_sides: frozenset[Side]
    supported_kinds: frozenset[SetupKind]
    min_setup_lifetime: timedelta
    max_setup_lifetime: timedelta
    max_targets: int
    # How far a level may sit from the observed price before the proposal stops
    # being about this market. A sanity envelope, not a view on what is likely.
    max_level_multiple: Decimal
    min_level_fraction: Decimal
    # How old the market observation may be before a setup built on it would be
    # describing a price that no longer exists.
    max_input_age: timedelta

    def __post_init__(self) -> None:
        if not self.supported_sides or not self.supported_kinds:
            raise ValueError("A policy must support at least one side and one setup kind")
        if self.min_setup_lifetime <= timedelta(0):
            raise ValueError("A setup must live for a positive interval")
        if self.max_setup_lifetime < self.min_setup_lifetime:
            raise ValueError("The maximum lifetime cannot sit below the minimum")
        if not 1 <= self.max_targets <= 5:
            raise ValueError("Target count must stay small enough to manage")
        if self.max_level_multiple <= Decimal(1):
            raise ValueError("The upper envelope must sit above the observed price")
        if not Decimal(0) < self.min_level_fraction < Decimal(1):
            raise ValueError("The lower envelope must sit below the observed price")
        if self.max_input_age <= timedelta(0):
            raise ValueError("Input age tolerance must be positive")

    def envelope(self, reference: Decimal) -> tuple[Decimal, Decimal]:
        """The band every proposed level must fall inside."""
        return reference * self.min_level_fraction, reference * self.max_level_multiple


# Provisional PAPER-mode policy.
#
# The lifetime bounds say a setup is a short-lived proposal about current
# conditions: five minutes is long enough to be watched, four hours is long
# enough that nobody is tempted to leave one standing overnight against a market
# that has moved. Three targets is the most a simple position-management scheme
# could act on, and more would only be a model filling a list.
#
# The envelope allows a quarter to four times the observed price. That is very
# wide as a trading view and very narrow as a typo filter, which is exactly the
# intent: it catches a lost decimal point or a hallucinated number and refuses
# nothing a person would defend.
VECTOR_SETUP_V1 = VectorSetupPolicy(
    version="vector-setup-v1",
    supported_sides=frozenset({Side.BUY}),
    supported_kinds=frozenset({SetupKind.BREAKOUT_LONG, SetupKind.PULLBACK_LONG}),
    min_setup_lifetime=timedelta(minutes=5),
    max_setup_lifetime=timedelta(hours=4),
    max_targets=3,
    max_level_multiple=Decimal(4),
    min_level_fraction=Decimal("0.25"),
    # Tighter than ORBIT's discovery window on purpose: discovery asks whether a
    # market is worth a look, and a fifteen-minute-old answer is still useful.
    # This proposes price levels, and a fifteen-minute-old price is a different
    # price.
    max_input_age=timedelta(minutes=5),
)
