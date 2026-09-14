"""What a canonical risk request is, and everything it deliberately is not.

One narrow server-side call takes a TradeCase that already has explicit sizing,
provable data and a real portfolio, runs it through the existing SENTINEL, and
binds the verdict to the whole basis it was reached from.

Four things it is not.

**Not a second risk engine.** `src.risk.engine.evaluate` decides, and
`classify_risk_authorization` interprets. Nothing here re-derives either.

**Not a status owner.** The central workflow evaluator owns every status. This
records a decision and lets that evaluator conclude what the case now is.

**Not an execution.** No fill happens, no cash is reserved, no position moves.
An `APPROVE` says SENTINEL permitted the requested size against the facts it was
given; it is not a standing permission for a later unchecked fill, and a
reservation contract does not exist yet.

**Not a retry loop.** A rejection is a rejection. There is no downsize, no
second attempt at a smaller number, and no new request identity minted because
the data changed.
"""

import json
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.core.models import RiskOutcome
from src.orchestration.riskdata.models import RiskDataBlocker, RiskDataGap
from src.orchestration.sizing.canonical import lossless_decimal
from src.orchestration.sizing.models import SizingRefusal
from src.risk.authorization import RiskAuthorization

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RiskRequestRefusal(StrEnum):
    """Why SENTINEL was not asked. None of these is a risk verdict.

    That distinction is the whole point. A rejection is terminal for the case
    and permanently bars the market from opening another one, so a data gap, a
    stale source or a stop in force must never be spent as if it were a
    judgement about the market.
    """

    TRADE_CASE_UNAVAILABLE = "TRADE_CASE_UNAVAILABLE"
    # The evaluator has not published READY_FOR_RISK, so there is nothing to ask
    # about — or it has already answered and the case is terminal.
    TRADE_CASE_NOT_READY_FOR_RISK = "TRADE_CASE_NOT_READY_FOR_RISK"
    TRADE_CASE_TERMINAL = "TRADE_CASE_TERMINAL"
    # Facts are missing, unattributable or older than the data policy allows.
    RISK_DATA_INCOMPLETE = "RISK_DATA_INCOMPLETE"
    # No configured size, or one that could not be derived honestly.
    SIZING_INPUT_UNAVAILABLE = "SIZING_INPUT_UNAVAILABLE"
    # Present and provable, and still older than SENTINEL's own configured
    # tolerance. Checked here rather than left to SENTINEL, because there it
    # would come back as a terminal rejection of the market.
    SOURCE_OLDER_THAN_RISK_LIMIT = "SOURCE_OLDER_THAN_RISK_LIMIT"
    # A holding exists that cannot be valued, so exposure and the day's loss are
    # unknown. SENTINEL would answer `PORTFOLIO_DATA_UNKNOWN`, terminally.
    PORTFOLIO_MARKS_UNAVAILABLE = "PORTFOLIO_MARKS_UNAVAILABLE"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    # The case already has its one canonical trade request under another key.
    RISK_REQUEST_ALREADY_EXISTS = "RISK_REQUEST_ALREADY_EXISTS"
    # The caller named a revision or a safety digest the case has since left.
    SOURCE_CHANGED_DURING_REQUEST = "SOURCE_CHANGED_DURING_REQUEST"
    # Recomputed at the decision instant, the case is no longer eligible: its
    # own lifetime lapsed, or a safety envelope aged out. The stored status said
    # otherwise only because nothing had touched the row since.
    TRADE_CASE_NO_LONGER_ELIGIBLE = "TRADE_CASE_NO_LONGER_ELIGIBLE"
    # The completeness reading or the sizing assessment expired between being
    # taken and being used. Both are read before the locks settle, and a basis
    # that has aged out in between is not the basis of anything.
    DECISION_BASIS_EXPIRED = "DECISION_BASIS_EXPIRED"
    # The same key arrived with a different basis. Never silently recomputed.
    RISK_REQUEST_CONFLICT = "RISK_REQUEST_CONFLICT"


class RiskRequestEvaluated(Immutable):
    """A SENTINEL verdict, bound to the request identity it was reached under."""

    kind: Literal["risk_request_evaluated"] = "risk_request_evaluated"
    request_id: UUID
    request_key: Identifier
    trade_case_id: UUID
    case_revision: int = Field(ge=1)
    outcome: RiskOutcome
    authorization: RiskAuthorization
    risk_decision_id: UUID
    binding_id: UUID
    # The safety-evidence digest, copied verbatim. Never redefined here.
    risk_input_digest: Digest
    # Everything else the verdict rested on, bound separately and explicitly.
    risk_request_digest: Digest
    intent_id: UUID
    intent_fingerprint: Digest
    reason_codes: tuple[Code, ...] = Field(min_length=1)
    evaluated_at: AwareDatetime
    expires_at: AwareDatetime
    # True when this call found the stored request rather than evaluating.
    replayed: bool = Field(strict=True)
    # Established negative evidence, carried through unchanged.
    blockers: tuple[RiskDataBlocker, ...] = Field(default=(), max_length=32)

    @property
    def authorizes_execution(self) -> bool:
        """Whether this permits a fill. Today: never, whatever the outcome.

        An approval says SENTINEL permitted the requested size against the facts
        it was shown. Acting on it later requires re-checking those facts under
        a lock and a reservation contract that does not exist, so the property
        is stated here rather than inferred by whoever writes that code.
        """
        return False

    @property
    def reserves_cash(self) -> bool:
        """Whether this holds any cash against the account. Today: never."""
        return False


class RiskRequestRefused(Immutable):
    """SENTINEL was not asked, and the typed reason why.

    Carries what was found rather than only what was missing: the gaps, and the
    blockers beside them. A case can be simultaneously unmeasurable and known to
    be dangerous, and the second must not disappear behind the first.
    """

    kind: Literal["risk_request_refused"] = "risk_request_refused"
    reason: RiskRequestRefusal
    trade_case_id: UUID
    trade_case_status: Identifier
    data_gaps: tuple[RiskDataGap, ...] = Field(default=(), max_length=24)
    sizing_reason: SizingRefusal | None = None
    blockers: tuple[RiskDataBlocker, ...] = Field(default=(), max_length=32)
    detail: Code | None = None


RiskRequestReading = RiskRequestEvaluated | RiskRequestRefused


def risk_request_digest(basis: dict[str, object]) -> str:
    """Hash the whole decision basis, canonically.

    Deliberately a *second* digest rather than an extension of the safety one.
    `risk_input_digest` means one thing — the active safety-critical evidence —
    and code, tests and stored rows already rest on that meaning. Folding sizing,
    a portfolio and a limits set into it would redefine every historical value
    silently, and would drag SIGNAL and FUSE no closer to risk authority only by
    accident of what happens to be excluded today.
    """
    return sha256(
        json.dumps(
            basis, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True
        ).encode()
    ).hexdigest()


def canonical_amount(value: object) -> object:
    """One textual form per Decimal in the basis, lossless."""
    from decimal import Decimal

    return lossless_decimal(value) if isinstance(value, Decimal) else value
