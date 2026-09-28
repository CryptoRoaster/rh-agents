"""PAPER_EXIT_V1: when an open PAPER position is to be closed, decided by arithmetic.

No model is asked. The policy compares the entry fill with the current mark,
the holding time with a limit, and the market's current liquidity with
SENTINEL's own entry bound, and names at most one trigger:

1. **STOP_LOSS** — the mark is at or below entry × (1 − stop).
2. **SENTINEL_INVALIDATION** — the held market's liquidity is known and below
   SENTINEL's `min_liquidity_usd`: SENTINEL would refuse to buy it now.
3. **TAKE_PROFIT** — the mark is at or above entry × (1 + target).
4. **TIME_EXIT** — the position has been held for the maximum holding time.

The order is fixed and protective first: a position that is both below its
stop and past its time is recorded as a stop. An unknown mark triggers no
price exit (unknown is not a breach), and unknown liquidity triggers no
invalidation; a time exit needs no price. Whatever triggers, the sale itself
still goes through the existing exit service and its own SENTINEL SELL check,
which may refuse it.

The numbers are not chosen here. `PaperExitPolicy` has no defaults for stop,
target or holding time: a deployment states them, and until it does no
automatic exit runs.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

POLICY_VERSION: Literal["PAPER_EXIT_V1"] = "PAPER_EXIT_V1"
BPS = Decimal(10000)


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ExitTrigger(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    SENTINEL_INVALIDATION = "SENTINEL_INVALIDATION"
    TAKE_PROFIT = "TAKE_PROFIT"
    TIME_EXIT = "TIME_EXIT"


class PaperExitPolicy(Immutable):
    version: Literal["PAPER_EXIT_V1"] = POLICY_VERSION
    stop_loss_bps: int = Field(gt=0, le=10000)
    take_profit_bps: int = Field(gt=0, le=1_000_000)
    max_holding_seconds: int = Field(gt=0, le=90 * 86400)
    invalidate_below_min_liquidity: bool = True


class ExitInputs(Immutable):
    """What was observed about one open position at one instant."""

    entry_price_usd: Decimal = Field(gt=0)
    entered_at: AwareDatetime
    mark_price_usd: Decimal | None = Field(default=None, gt=0)
    mark_observed_at: AwareDatetime | None = None
    liquidity_usd: Decimal | None = Field(default=None, ge=0)
    min_liquidity_usd: Decimal = Field(gt=0)
    now: AwareDatetime


class ExitVerdict(Immutable):
    """The one trigger, if any, and the numbers it was decided on."""

    policy_version: Literal["PAPER_EXIT_V1"] = POLICY_VERSION
    trigger: ExitTrigger | None = None
    reason: str
    return_bps: Decimal | None = None
    held_seconds: int
    liquidity_usd: Decimal | None = None


def evaluate(policy: PaperExitPolicy, inputs: ExitInputs) -> ExitVerdict:
    held = max(0, int((inputs.now - inputs.entered_at).total_seconds()))
    move = (
        ((inputs.mark_price_usd / inputs.entry_price_usd - 1) * BPS).quantize(Decimal("0.01"))
        if inputs.mark_price_usd is not None
        else None
    )

    def verdict(trigger: ExitTrigger | None, reason: str) -> ExitVerdict:
        return ExitVerdict(
            trigger=trigger,
            reason=reason,
            return_bps=move,
            held_seconds=held,
            liquidity_usd=inputs.liquidity_usd,
        )

    if move is not None and move <= -policy.stop_loss_bps:
        return verdict(ExitTrigger.STOP_LOSS, "MARK_AT_OR_BELOW_STOP")
    if (
        policy.invalidate_below_min_liquidity
        and inputs.liquidity_usd is not None
        and inputs.liquidity_usd < inputs.min_liquidity_usd
    ):
        return verdict(ExitTrigger.SENTINEL_INVALIDATION, "LIQUIDITY_BELOW_SENTINEL_MINIMUM")
    if move is not None and move >= policy.take_profit_bps:
        return verdict(ExitTrigger.TAKE_PROFIT, "MARK_AT_OR_ABOVE_TARGET")
    if held >= policy.max_holding_seconds:
        return verdict(ExitTrigger.TIME_EXIT, "MAX_HOLDING_TIME_REACHED")
    return verdict(None, "HOLD" if move is not None else "HOLD_MARK_UNKNOWN")


__all__ = [
    "POLICY_VERSION",
    "ExitInputs",
    "ExitTrigger",
    "ExitVerdict",
    "PaperExitPolicy",
    "evaluate",
]
