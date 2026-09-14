"""Bounds on what a PAPER cost assumption may be configured to say.

These are not costs. They are the outer limits within which an operator may
state an assumption, so that a typo cannot quietly configure a fee of a hundred
percent and a simulation cannot be made to look profitable by making costs
vanish.

No number here is a default cost. The policy ships bounds; the amounts are
configured, and an unconfigured amount means there is no cost basis at all.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.core.models import TradingMode

BPS = Decimal(10000)


@dataclass(frozen=True)
class PaperCostPolicy:
    version: str
    # PAPER only. These assumptions describe a simulation; there is nothing to
    # assume about a live fill, and nothing here may be reachable from one.
    supported_modes: frozenset[TradingMode]
    # A fee above this is far more likely to be a misplaced decimal than a
    # venue's actual charge. Deliberately generous: the bound exists to catch a
    # mistake, not to express a view on what a fair fee is.
    max_fee_bps: Decimal
    # The same reasoning for the assumed adverse price move. A simulation that
    # assumed half the notional disappears on the way to the fill is not a
    # conservative simulation, it is a broken one.
    max_slippage_bps: Decimal

    def __post_init__(self) -> None:
        if not self.supported_modes:
            raise ValueError("A cost policy must support at least one trading mode")
        if TradingMode.LIVE_AUTONOMOUS in self.supported_modes:
            raise ValueError("Live execution is unavailable")
        for name, value in (
            ("fee", self.max_fee_bps),
            ("slippage", self.max_slippage_bps),
        ):
            if not Decimal(0) < value < BPS:
                raise ValueError(f"The {name} bound must be a positive sub-100% basis-point figure")


PAPER_COST_V1 = PaperCostPolicy(
    version="paper-cost-v1",
    supported_modes=frozenset({TradingMode.PAPER}),
    # Five percent each. Two orders of magnitude above an ordinary aggregator
    # fee and an ordinary execution deviation on a liquid pair, which is what
    # makes them a typo guard rather than a policy on acceptable trading costs.
    max_fee_bps=Decimal(500),
    max_slippage_bps=Decimal(500),
)
