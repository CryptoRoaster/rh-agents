"""What a liquidity/FDV re-check is, and every reason there is not one.

A market SENTINEL refused only because its pool was out of proportion to the
token's valuation, or because that proportion could not be established, may be
asked about once more with fresh evidence. The caller names the rejected case;
what comes back is a new TradeCase in the workflow's own starting state, and
nothing else.

Four things this is not.

**Not a purchase.** The call opens a case. ORBIT, ATLAS, PULSE, ANCHOR and
SENTINEL all run again afterwards, through exactly the checks they always run
through. Nothing here buys, approves or judges anything.

**Not an inherited authorization.** The rejected case keeps its evidence,
binding, risk request and events, unrewritten. The successor proves itself
from nothing; no figure, verdict or approval crosses over.

**Not a relaxation of intake.** COMMANDER still bars the market as
`RISK_REJECTED_FOR_MARKET`, and the scout still skips it. Only this call opens
the successor, and only for a rejection made of the gate's own codes.

**Not a loop.** One rejected case gets at most one successor, a market at most
`max_per_market` of them, and only inside a fixed window after the rejection.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]

# The namespace every successor's open key lives in. The key is derived from the
# predecessor alone, so one rejected case can never have two successors, and it
# can never collide with an intake generation, a re-entry or an exit order.
RECHECK_KEY_PREFIX = "rh-agents:liq-fdv-recheck:"
RECHECK_OPENED_EVENT = "LIQ_FDV_RECHECK_OPENED"


@dataclass(frozen=True)
class RecheckPolicy:
    version: str
    # A rejection made only of these codes says nothing settled about the
    # market: a ratio outside the band can move into it, and an undecidable or
    # unknown ratio is no verdict at all. `FDV_DECIMALS_MISMATCH` is absent on
    # purpose: a contract and a provider disagreeing on the token's scale is
    # not expected to resolve by waiting.
    eligible_codes: frozenset[str]
    # Measured from the rejecting decision's own evaluation time.
    min_interval: timedelta
    max_window: timedelta
    # Successors over a market's whole history, however each one ended.
    max_per_market: int
    # How long a successor has to reach a decision before it expires.
    case_lifetime: timedelta

    def __post_init__(self) -> None:
        if not self.eligible_codes:
            raise ValueError("A re-check needs at least one eligible code")
        if self.min_interval <= timedelta(0) or self.max_window <= self.min_interval:
            raise ValueError("The re-check window must open after the rejection and close later")
        if self.max_per_market < 1 or self.case_lifetime <= timedelta(0):
            raise ValueError("Caps and lifetimes must admit one successor")


LIQ_FDV_RECHECK_V1 = RecheckPolicy(
    version="LIQ_FDV_RECHECK_V1",
    eligible_codes=frozenset(
        {
            "LIQUIDITY_FDV_RATIO_LIMIT",
            "LIQUIDITY_FDV_RATIO_UNVERIFIED",
            "LIQUIDITY_FDV_RATIO_UNDECIDABLE",
            "FDV_UNKNOWN",
        }
    ),
    min_interval=timedelta(minutes=30),
    max_window=timedelta(hours=24),
    max_per_market=2,
    case_lifetime=timedelta(hours=1),
)


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RecheckRefusal(StrEnum):
    """Why no successor case was opened. None of these is a failed opening."""

    # ------------------------------------------------- the predecessor
    PREDECESSOR_NOT_FOUND = "PREDECESSOR_NOT_FOUND"
    # The case did not end in a SENTINEL rejection. An approval, an execution,
    # an expiry and a live case are all outside this contract.
    PREDECESSOR_NOT_RISK_REJECTED = "PREDECESSOR_NOT_RISK_REJECTED"
    # No binding, or the latest one is not the decision the case's own status
    # was derived from. The rejection cannot be placed, so it is not re-checked.
    PREDECESSOR_DECISION_UNREADABLE = "PREDECESSOR_DECISION_UNREADABLE"
    # The rejecting decision was a system pause, not a REJECT.
    PREDECESSOR_NOT_REJECT = "PREDECESSOR_NOT_REJECT"
    # The rejection carries a code outside the gate's own. Every other refusal
    # is a verdict about the market and keeps barring it.
    REASON_NOT_ELIGIBLE = "REASON_NOT_ELIGIBLE"
    # ------------------------------------------------- the market
    # A later case exists for this market: the predecessor is not where its
    # history ends, and only the end of a history may be re-checked.
    PREDECESSOR_NOT_LATEST = "PREDECESSOR_NOT_LATEST"
    ACTIVE_CASE_EXISTS = "ACTIVE_CASE_EXISTS"
    # The market was executed. A position is spoken for by re-entry only.
    MARKET_EXECUTED = "MARKET_EXECUTED"
    # Some other case of this market was rejected on a code outside the gate.
    MARKET_REJECTED_OTHERWISE = "MARKET_REJECTED_OTHERWISE"
    RECHECK_LIMIT_REACHED = "RECHECK_LIMIT_REACHED"
    # ------------------------------------------------- the window
    RECHECK_TOO_EARLY = "RECHECK_TOO_EARLY"
    RECHECK_WINDOW_CLOSED = "RECHECK_WINDOW_CLOSED"
    # The market is on a chain intake no longer opens cases on, or is a
    # synthetic one. The re-check honours the same operational bars as intake.
    CHAIN_NOT_ENABLED = "CHAIN_NOT_ENABLED"
    FIXTURE_MARKET = "FIXTURE_MARKET"
    # Early-only operation works early cases alone; a normal successor would
    # sit unworked and block the market as an active case until it lapsed.
    STRATEGY_NOT_RUN = "STRATEGY_NOT_RUN"
    # ------------------------------------------------- the stops
    # Not PAPER: no case that could lead to a BUY opens.
    MODE_NOT_PAPER = "MODE_NOT_PAPER"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    ENTRY_HALTED = "ENTRY_HALTED"
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


class RecheckOpened(Immutable):
    """One successor case, and what it was opened on."""

    kind: Literal["recheck_opened"] = "recheck_opened"
    trade_case_id: UUID
    predecessor_trade_case_id: UUID
    predecessor_binding_id: UUID
    # The codes the predecessor was refused on, which made it eligible.
    reason_codes: tuple[Code, ...]
    policy_version: Identifier
    market_pair_id: Identifier
    trade_case_status: Identifier
    opened_at: AwareDatetime
    # True when this call found the successor rather than opening one.
    replayed: bool = Field(strict=True)

    @property
    def authorizes_execution(self) -> bool:
        """Whether this permits a purchase. Never.

        The successor starts where every case starts. Evidence has to be
        submitted, the completeness check has to pass, SENTINEL has to approve
        and the fill has to re-check — none of which this call does, or shortens.
        """
        return False


class RecheckRefused(Immutable):
    """No successor case, and the typed reason why."""

    kind: Literal["recheck_refused"] = "recheck_refused"
    reason: RecheckRefusal
    predecessor_trade_case_id: UUID
    detail: Code | None = None


RecheckReading = RecheckOpened | RecheckRefused
