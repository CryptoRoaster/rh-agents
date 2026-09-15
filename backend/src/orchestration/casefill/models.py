"""What a case-bound paper fill is, and every reason there is not one.

One stored risk request, one order, one fill. The identity comes from the
request that was already persisted; nothing here re-derives a quantity, a price
or a notional, because re-deriving any of them would make the order that is
filled a different order from the one that was authorised.

Three things this is not.

**Not a second risk engine.** `src.risk.engine.evaluate` decides, immediately
before the fill, on the portfolio and market as they are at that moment.

**Not a status owner.** The workflow evaluator says what the case is; a filled
entry ends it through the same guarded transition every other status uses.

**Not a standing permission.** An authorization recorded earlier does not
reserve cash and does not survive on its own: it is checked again here, and the
fill happens only if the re-check agrees.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.core.models import RiskOutcome
from src.orchestration.riskdata.models import RiskDataBlocker, RiskDataGap
from src.risk.authorization import RiskAuthorization

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ExecutionRefusal(StrEnum):
    """Why no fill happened. None of these is a fill that failed."""

    # No canonical risk request exists for this case, so there is no order.
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    # A request exists under a different key. Refused rather than answered:
    # two callers must not end up believing they own the same order.
    REQUEST_KEY_MISMATCH = "REQUEST_KEY_MISMATCH"
    # The stored request was not an approval, so there is nothing to execute.
    # `RISK_LIMITED` lands here too: a rejected size with a recorded capacity is
    # still a rejected size.
    REQUEST_NOT_APPROVED = "REQUEST_NOT_APPROVED"
    # The approval's own validity has run out. A decision issued with a short
    # life is not an authorization once that life is over.
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    # The binding the request was granted under is no longer the case's current
    # one, or does not belong to this request at all.
    AUTHORIZATION_SUPERSEDED = "AUTHORIZATION_SUPERSEDED"
    # The safety evidence the approval covered has been replaced. That is not a
    # weaker authorization; it is an authorization about a different case.
    SAFETY_EVIDENCE_CHANGED = "SAFETY_EVIDENCE_CHANGED"
    # Recomputed now, the case is not in an authorised state.
    TRADE_CASE_NOT_AUTHORIZED = "TRADE_CASE_NOT_AUTHORIZED"
    TRADE_CASE_TERMINAL = "TRADE_CASE_TERMINAL"
    # The facts a risk evaluation needs are no longer complete or current.
    RISK_DATA_INCOMPLETE = "RISK_DATA_INCOMPLETE"
    DECISION_BASIS_EXPIRED = "DECISION_BASIS_EXPIRED"
    SOURCE_OLDER_THAN_RISK_LIMIT = "SOURCE_OLDER_THAN_RISK_LIMIT"
    # A holding exists that this system cannot value, so the portfolio SENTINEL
    # would judge is unknown. A missing capability, not a verdict.
    PORTFOLIO_MARKS_UNAVAILABLE = "PORTFOLIO_MARKS_UNAVAILABLE"
    # A position appeared between the valuation and the account lock, so the
    # portfolio about to be judged is not the one that was valued. Refused
    # rather than judged on a partial picture, which would look like a figure.
    PORTFOLIO_CHANGED_DURING_VALUATION = "PORTFOLIO_CHANGED_DURING_VALUATION"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    # SENTINEL, asked again immediately before the fill, did not approve. The
    # outcome and authorization travel on the reading.
    RISK_RECHECK_REFUSED = "RISK_RECHECK_REFUSED"
    # A validity lapsed between the evaluation and the execution boundary — the
    # decision, the intent and the market are persisted in between, and each of
    # those writes takes real time. Approved and not executed, with everything
    # started rolled back. Never a risk verdict about the market.
    EXECUTION_WINDOW_EXPIRED = "EXECUTION_WINDOW_EXPIRED"


class PaperFillRecorded(Immutable):
    """One paper fill, bound to the request that authorised it."""

    kind: Literal["paper_fill_recorded"] = "paper_fill_recorded"
    case_execution_id: UUID
    trade_case_id: UUID
    request_id: UUID
    request_key: Identifier
    intent_id: UUID
    order_id: UUID
    execution_id: UUID
    # The approval this ran under, and the evaluation performed immediately
    # before the fill. Two decisions, and the first is never rewritten.
    authorizing_binding_id: UUID
    recheck_decision_id: UUID
    risk_input_digest: Digest
    quantity: Positive
    execution_price_usd: Positive
    notional_usd: Positive
    fees_usd: Amount
    filled_at: AwareDatetime
    # True when this call found the stored fill rather than performing one.
    replayed: bool = Field(strict=True)
    trade_case_status: Identifier

    @property
    def is_simulated(self) -> bool:
        """Whether this fill happened in a simulation. Today: always.

        Costs are configured assumptions and no order reached a venue. Stated as
        a property so the day a live path exists, the thing that has to change
        is visible rather than implied by a mode flag somewhere else.
        """
        return True


class ExecutionRefused(Immutable):
    """No fill, and the typed reason why.

    Carries the risk verdict when one was actually reached, so a refusal that
    came from SENTINEL is distinguishable from one that never got that far.
    """

    kind: Literal["execution_refused"] = "execution_refused"
    reason: ExecutionRefusal
    trade_case_id: UUID
    trade_case_status: Identifier
    outcome: RiskOutcome | None = None
    authorization: RiskAuthorization | None = None
    reason_codes: tuple[Code, ...] = Field(default=(), max_length=24)
    data_gaps: tuple[RiskDataGap, ...] = Field(default=(), max_length=24)
    blockers: tuple[RiskDataBlocker, ...] = Field(default=(), max_length=32)
    detail: Code | None = None
    # True when the refusal is a stored verdict returned unchanged rather than
    # one reached now. History, never a fresh decision.
    replayed: bool = Field(default=False, strict=True)


ExecutionReading = PaperFillRecorded | ExecutionRefused


def notional_of(quantity: Decimal, price: Decimal) -> Decimal:
    """What the fill was worth at the price it filled at."""
    from src.core.numbers import quantize

    return quantize(quantity * price)
