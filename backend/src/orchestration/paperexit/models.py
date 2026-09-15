"""What an explicit paper exit is, and every reason there is not one.

One open position, one SELL order, one fill. The caller names the position and
an idempotent key; the quantity is the whole holding, determined server-side
under the account lock and bound to the order it produced.

Four things this is not.

**Not a partial sale.** An exit closes the position. A quantity chosen here
would be a strategy, and this system does not have one.

**Not authorised by the entry.** The approval that permitted a purchase
permitted a purchase. The exit asks SENTINEL again, for a SELL, and is refused
exactly as often as SENTINEL refuses it.

**Not an emergency.** Nothing here bypasses a kill switch, a pause, the trading
mode or a risk limit, and there is no privileged path that sells anyway.

**Not a re-entry permit.** The entry case stays `EXECUTED` and the market stays
barred. Whether a later entry is a new trade is a contract that does not exist.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.core.models import RiskOutcome
from src.orchestration.riskdata.models import RiskDataBlocker, RiskDataGap

Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Signed = Annotated[Decimal, Field(allow_inf_nan=False, max_digits=38, decimal_places=18)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ExitRefusal(StrEnum):
    """Why nothing was sold. None of these is a sale that failed."""

    # ---------------------------------------------------- the holding itself
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    # Nothing is held. A position row at zero is not an open position, and a
    # second exit for one is not a second sale — it is no sale at all.
    POSITION_ALREADY_CLOSED = "POSITION_ALREADY_CLOSED"
    # ---------------------------------------------------- provenance
    # No case-bound PAPER entry accounts for this holding. Selling something
    # this system cannot say it bought would be a trade with no origin.
    POSITION_ORIGIN_UNKNOWN = "POSITION_ORIGIN_UNKNOWN"
    # More than one entry could account for it. Picking one would attribute a
    # sale to a case that may not have opened it.
    POSITION_ORIGIN_AMBIGUOUS = "POSITION_ORIGIN_AMBIGUOUS"
    # The position records no market, so nothing can be sold in one.
    POSITION_MARKET_UNKNOWN = "POSITION_MARKET_UNKNOWN"
    # The position's market is not the one its entry happened in. One of the
    # two records is wrong, and a sale is not the place to decide which.
    POSITION_MARKET_MISMATCH = "POSITION_MARKET_MISMATCH"
    # The entry case never reached a booked execution.
    ENTRY_NOT_EXECUTED = "ENTRY_NOT_EXECUTED"
    # ---------------------------------------------------- the key
    # This key already names a different position or a different holding size.
    # Refused rather than answered: two callers must not end up believing they
    # own the same order, and a changed holding is not the same order.
    EXIT_KEY_MISMATCH = "EXIT_KEY_MISMATCH"
    # ---------------------------------------------------- the data
    RISK_DATA_INCOMPLETE = "RISK_DATA_INCOMPLETE"
    DECISION_BASIS_EXPIRED = "DECISION_BASIS_EXPIRED"
    SOURCE_OLDER_THAN_RISK_LIMIT = "SOURCE_OLDER_THAN_RISK_LIMIT"
    PORTFOLIO_MARKS_UNAVAILABLE = "PORTFOLIO_MARKS_UNAVAILABLE"
    PORTFOLIO_CHANGED_DURING_VALUATION = "PORTFOLIO_CHANGED_DURING_VALUATION"
    # ---------------------------------------------------- the stops
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    # ---------------------------------------------------- the verdict
    # SENTINEL, asked about this SELL, did not approve. The outcome and reason
    # codes travel on the reading; nothing here overrides them.
    EXIT_RISK_REFUSED = "EXIT_RISK_REFUSED"
    # A validity lapsed between the evaluation and the execution boundary. The
    # decision, the intent and the market are persisted in between, and each of
    # those writes takes real time. Everything started is rolled back, and no
    # risk rejection is written in its place.
    EXECUTION_WINDOW_EXPIRED = "EXECUTION_WINDOW_EXPIRED"


class PaperExitRecorded(Immutable):
    """One paper exit, bound to the entry it closed."""

    kind: Literal["paper_exit_recorded"] = "paper_exit_recorded"
    exit_id: UUID
    trade_case_id: UUID
    case_execution_id: UUID
    request_key: Identifier
    position_id: UUID
    asset_id: Identifier
    market_pair_id: Identifier
    intent_id: UUID
    order_id: UUID
    execution_id: UUID
    # The exit's own decision, never the entry's approval.
    risk_decision_id: UUID
    quantity: Positive
    execution_price_usd: Positive
    notional_usd: Positive
    fees_usd: Amount
    realized_pnl_usd: Signed
    cost_basis_released_usd: Amount
    filled_at: AwareDatetime
    # True when this call found the stored exit rather than performing one.
    replayed: bool = Field(strict=True)

    @property
    def is_simulated(self) -> bool:
        """Whether this sale happened in a simulation. Today: always.

        Costs are configured assumptions and no order reached a venue. Stated as
        a property so the day a live path exists, the thing that has to change
        is visible rather than implied by a mode flag somewhere else.
        """
        return True

    @property
    def closes_position(self) -> bool:
        """An exit sells the whole holding. There are no partial sales."""
        return True


class ExitRefused(Immutable):
    """No sale, and the typed reason why."""

    kind: Literal["exit_refused"] = "exit_refused"
    reason: ExitRefusal
    position_id: UUID
    trade_case_id: UUID | None = None
    outcome: RiskOutcome | None = None
    reason_codes: tuple[Code, ...] = Field(default=(), max_length=24)
    data_gaps: tuple[RiskDataGap, ...] = Field(default=(), max_length=24)
    blockers: tuple[RiskDataBlocker, ...] = Field(default=(), max_length=32)
    detail: Code | None = None
    # True when the refusal is a stored verdict returned unchanged rather than
    # one reached now. History, never a fresh decision.
    replayed: bool = Field(default=False, strict=True)


ExitReading = PaperExitRecorded | ExitRefused
