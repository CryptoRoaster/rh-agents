"""What a PAPER cost assumption is, and the several things it is not.

Two numbers describe how a simulated fill is priced: a proportional fee and an
assumed adverse price move. Both are **stated by an operator**, and the contract
says so in its own type — `basis` has exactly one value, and that value is
`OPERATOR_CONFIGURED_ASSUMPTION`.

They are not observations. Nothing here came from a market, a provider or a
fill, and four figures in this system that *are* observed must never be read
into these fields or filled from them:

* ANCHOR's **execution deviation** is how far a quote's effective price sat from
  an independent reference. It measures a quote, at a size, at an instant.
* A provider's **price impact** is that provider's own figure in its own
  semantics, published or absent.
* ANCHOR's **tested capacity** is a size the market accepted. It is a fact about
  depth, not a cost.
* A **realised** slippage would be measured after a fill against the price that
  was actually paid. No such measurement exists in this system.

Nothing in this module is written back into ANCHOR evidence. Evidence records
what was observed; an assumption recorded as an observation would be indelible
and wrong.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models import TradingMode
from src.orchestration.costs.policy import PAPER_COST_V1, PaperCostPolicy

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Bps = Annotated[
    Decimal, Field(ge=0, le=10000, allow_inf_nan=False, max_digits=38, decimal_places=18)
]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PaperCostRefusal(StrEnum):
    """Why no cost basis exists. Never a zero-cost fallback."""

    # Neither figure is configured. A simulation without a cost basis is not a
    # cheap simulation, it is one that has not been told what trading costs.
    PAPER_COST_BASIS_NOT_CONFIGURED = "PAPER_COST_BASIS_NOT_CONFIGURED"
    PAPER_COST_FEE_NOT_CONFIGURED = "PAPER_COST_FEE_NOT_CONFIGURED"
    PAPER_COST_SLIPPAGE_NOT_CONFIGURED = "PAPER_COST_SLIPPAGE_NOT_CONFIGURED"
    # Configured beyond the policy's typo guard.
    PAPER_COST_OUT_OF_BOUNDS = "PAPER_COST_OUT_OF_BOUNDS"
    PAPER_COST_MODE_NOT_SUPPORTED = "PAPER_COST_MODE_NOT_SUPPORTED"


class PaperCostPolicySnapshot(Immutable):
    """The bounds an assumption was accepted under, bound by content not by name."""

    version: Identifier
    supported_modes: tuple[TradingMode, ...] = Field(min_length=1)
    max_fee_bps: Bps
    max_slippage_bps: Bps

    @classmethod
    def of(cls, policy: PaperCostPolicy) -> "PaperCostPolicySnapshot":
        return cls(
            version=policy.version,
            supported_modes=tuple(sorted(policy.supported_modes, key=lambda item: item.value)),
            max_fee_bps=policy.max_fee_bps,
            max_slippage_bps=policy.max_slippage_bps,
        )


class PaperCostAssumptions(Immutable):
    """The configured simulation cost basis, with its meaning written down."""

    kind: Literal["paper_cost_assumptions"] = "paper_cost_assumptions"
    basis: Literal["OPERATOR_CONFIGURED_ASSUMPTION"] = "OPERATOR_CONFIGURED_ASSUMPTION"
    mode: Literal[TradingMode.PAPER] = TradingMode.PAPER
    policy: PaperCostPolicySnapshot

    # A proportional charge on the executed notional of **one** side of a trade.
    # Stated precisely because the alternative readings differ by a factor of
    # two: it is not a round-trip figure, so a later exit charges it again, and
    # a caller that applied it twice to one fill would double-count.
    fee_bps: Bps
    fee_meaning: Literal["PROPORTIONAL_FEE_ON_ONE_SIDE_EXECUTED_NOTIONAL"] = (
        "PROPORTIONAL_FEE_ON_ONE_SIDE_EXECUTED_NOTIONAL"
    )

    # The assumed adverse move between the reference price and the simulated
    # fill price. It moves the fill price; it is not an additional charge, so it
    # is never added to the fee.
    slippage_bps: Bps
    slippage_meaning: Literal["ASSUMED_ADVERSE_MOVE_FROM_REFERENCE_TO_FILL"] = (
        "ASSUMED_ADVERSE_MOVE_FROM_REFERENCE_TO_FILL"
    )

    # What this basis does *not* cover, named so nobody mistakes it for a
    # complete cost model. A simulation priced from these two numbers alone is
    # cheaper than reality by at least these.
    excludes: tuple[Code, ...] = (
        "GAS",
        "PRICE_IMPACT_BEYOND_ASSUMED_MOVE",
        "PARTIAL_FILL_COST",
        "FAILED_TRANSACTION_COST",
        "EXIT_SIDE_FEE",
    )


class PaperCostUnavailable(Immutable):
    """No usable cost basis, and the typed reason."""

    kind: Literal["paper_cost_unavailable"] = "paper_cost_unavailable"
    reason: PaperCostRefusal
    policy: PaperCostPolicySnapshot


PaperCostReading = PaperCostAssumptions | PaperCostUnavailable


def paper_cost_assumptions(
    *,
    fee_bps: Decimal | None,
    slippage_bps: Decimal | None,
    trading_mode: TradingMode,
    policy: PaperCostPolicy = PAPER_COST_V1,
) -> PaperCostReading:
    """Read the configured cost basis, or say precisely why there is none.

    No figure is defaulted, inferred from a market, or borrowed from ANCHOR.
    Having a cost basis authorises nothing on its own: it is one of several
    facts a later risk input needs, and the rest are checked elsewhere.
    """
    bound = PaperCostPolicySnapshot.of(policy)

    def refused(reason: PaperCostRefusal) -> PaperCostUnavailable:
        return PaperCostUnavailable(reason=reason, policy=bound)

    if trading_mode not in policy.supported_modes:
        return refused(PaperCostRefusal.PAPER_COST_MODE_NOT_SUPPORTED)
    if fee_bps is None and slippage_bps is None:
        return refused(PaperCostRefusal.PAPER_COST_BASIS_NOT_CONFIGURED)
    if fee_bps is None:
        return refused(PaperCostRefusal.PAPER_COST_FEE_NOT_CONFIGURED)
    if slippage_bps is None:
        return refused(PaperCostRefusal.PAPER_COST_SLIPPAGE_NOT_CONFIGURED)
    if not fee_bps.is_finite() or not slippage_bps.is_finite():
        return refused(PaperCostRefusal.PAPER_COST_OUT_OF_BOUNDS)
    if fee_bps < 0 or slippage_bps < 0:
        return refused(PaperCostRefusal.PAPER_COST_OUT_OF_BOUNDS)
    if fee_bps > policy.max_fee_bps or slippage_bps > policy.max_slippage_bps:
        return refused(PaperCostRefusal.PAPER_COST_OUT_OF_BOUNDS)
    return PaperCostAssumptions(policy=bound, fee_bps=fee_bps, slippage_bps=slippage_bps)
