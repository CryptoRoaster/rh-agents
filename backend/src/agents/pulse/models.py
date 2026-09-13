"""PULSE contracts: what a trigger observation is, and what it deliberately is not.

PULSE answers exactly one question — *has the currently authoritative VECTOR
trigger condition become true?* — and it answers it with arithmetic. There is no
model here, no prompt, no provider and no judgement. A comparison between two
Decimals either holds or it does not.

That narrowness is the design. VECTOR wrote the condition down as a machine
grammar precisely so that the thing watching for it would not need to reason, and
a monitor that reasoned would put a second probabilistic judgement between the
setup and the act while leaving no way to say afterwards what the system had
actually been waiting for.

Three things PULSE is not allowed to become:

* **A second opinion on the setup.** Whether the idea is good was VECTOR's
  question and is now settled. PULSE does not re-litigate it, and nothing here
  can express approval or doubt.
* **A risk or execution authority.** No size, no route, no slippage, no
  liquidity judgement, no approval. Those belong to ANCHOR and SENTINEL.
* **A price source.** PULSE reads one assembled observation. It cannot choose
  what to query, cannot reach a second market, and cannot smooth, average or
  reinterpret what it was given.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.agents.vector.models import PRICE_BASIS, TriggerType

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]

PULSE_OUTPUT_SCHEMA_VERSION: Literal[1] = 1


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TriggerOutcome(StrEnum):
    """Every way a check can end, and they are not all failures.

    The distinction this enum exists to make is between *the condition is not yet
    true*, which is a monitor working correctly, and everything that actually went
    wrong. Collapsing the two would fill an audit trail with incidents that never
    happened and spend a retry budget on ordinary patience.
    """

    TRIGGERED = "TRIGGERED"
    # Normal operation. Not an error, not a failure, not worth an alert.
    NOT_TRIGGERED = "NOT_TRIGGERED"
    # The window closed before the condition became true. The watch is over and
    # no trigger exists; this is an answer, not a fault.
    SETUP_EXPIRED = "SETUP_EXPIRED"
    # There is nothing current to watch. A new setup may yet arrive.
    NO_CURRENT_SETUP = "NO_CURRENT_SETUP"
    # The observation is too old to say anything about now.
    OBSERVATION_STALE = "OBSERVATION_STALE"
    # The only price we have predates the setup. Ordinary right after a setup is
    # published — the market layer's newest snapshot is older than the proposal —
    # and it resolves itself as soon as the next observation is recorded.
    OBSERVATION_PRECEDES_SETUP = "OBSERVATION_PRECEDES_SETUP"
    # The observation cannot be compared to this condition at all: another
    # market, another unit, or a timestamp outside the setup's own window.
    OBSERVATION_INVALID = "OBSERVATION_INVALID"
    # More observations arrived in the window than the bounded read may return,
    # and none of the visible ones crossed. A negative answer would be a claim
    # about rows nobody looked at.
    OBSERVATION_BUDGET_EXCEEDED = "OBSERVATION_BUDGET_EXCEEDED"


# Which outcomes mean "ask again later" rather than "something is wrong". Stated
# once, here, so the handler cannot quietly reclassify ordinary operation.
WAITING_OUTCOMES = frozenset(
    {
        TriggerOutcome.NOT_TRIGGERED,
        TriggerOutcome.NO_CURRENT_SETUP,
        TriggerOutcome.OBSERVATION_STALE,
        TriggerOutcome.OBSERVATION_PRECEDES_SETUP,
    }
)


class PulseReasonCode(StrEnum):
    """Why a check ended as it did. Bounded codes, never prose."""

    CONDITION_MET = "CONDITION_MET"
    CONDITION_NOT_MET = "CONDITION_NOT_MET"
    SETUP_EXPIRED = "SETUP_EXPIRED"
    SETUP_NOT_YET_VALID = "SETUP_NOT_YET_VALID"
    NO_CURRENT_SETUP = "NO_CURRENT_SETUP"
    OBSERVATION_TOO_STALE = "OBSERVATION_TOO_STALE"
    OBSERVATION_BEFORE_SETUP = "OBSERVATION_BEFORE_SETUP"
    OBSERVATION_AFTER_SETUP = "OBSERVATION_AFTER_SETUP"
    OBSERVATION_IN_FUTURE = "OBSERVATION_IN_FUTURE"
    OBSERVATION_BUDGET_EXCEEDED = "OBSERVATION_BUDGET_EXCEEDED"
    MARKET_IDENTITY_MISMATCH = "MARKET_IDENTITY_MISMATCH"
    PRICE_BASIS_MISMATCH = "PRICE_BASIS_MISMATCH"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"


class WatchedTrigger(Immutable):
    """The condition to watch, copied from the authoritative setup.

    A copy rather than a reference, so the evidence can record exactly what was
    being watched at the moment it fired — including if the setup is superseded
    a second later.
    """

    setup_evidence_id: UUID
    setup_id: UUID
    setup_fingerprint: Identifier
    type: TriggerType
    price_basis: Literal["USD_PER_BASE_UNIT"] = PRICE_BASIS
    reference_price: Price | None = None
    zone_low: Price | None = None
    zone_high: Price | None = None
    valid_from: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def shape_matches_type(self) -> Self:
        """The same grammar VECTOR wrote, re-checked rather than trusted.

        PULSE is downstream of the producer that guarantees this shape, and a
        condition that arrived malformed would be evaluated by whichever branch
        happened to match. Re-validating costs nothing and removes that.
        """
        if self.expires_at <= self.valid_from:
            raise ValueError("A trigger must expire after it becomes valid")
        if self.type == TriggerType.PRICE_IN_RANGE:
            if self.reference_price is not None:
                raise ValueError("A range trigger has no single reference price")
            if self.zone_low is None or self.zone_high is None:
                raise ValueError("A range trigger must name both bounds")
            if self.zone_low > self.zone_high:
                raise ValueError("A trigger zone cannot start above where it ends")
        else:
            if self.reference_price is None:
                raise ValueError("A threshold trigger must name its reference price")
            if self.zone_low is not None or self.zone_high is not None:
                raise ValueError("A threshold trigger has no zone")
        return self


class PriceObservation(Immutable):
    """One recorded market price, with the provenance needed to judge it.

    ``observed_at`` is the market's own account of when this price existed. It is
    never the moment we read it: a stale price fetched a second ago is still
    stale, and the whole freshness rule rests on that distinction.
    """

    observation_id: UUID
    snapshot_id: UUID
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    base_asset_id: Identifier
    quote_asset_id: Identifier
    provider: Identifier
    is_fixture: bool = Field(strict=True)
    price_basis: Literal["USD_PER_BASE_UNIT"] = PRICE_BASIS
    price: Price
    observed_at: AwareDatetime


class PulseTaskInput(Immutable):
    """Everything PULSE is given, and nothing else.

    Deliberately thin. A monitor comparing one number to one threshold needs the
    threshold, the number and enough identity to know they belong together. It is
    given no candles, no VECTOR rationale, no other roles' conclusions and no
    market history: none of that could change the answer, and all of it would
    invite a monitor to have an opinion.

    There is no session, repository, provider client, RPC client, wallet, signer
    or executor here, and no field through which one could arrive.
    """

    trade_case_id: UUID
    task_id: UUID
    market_pair_id: Identifier
    trigger: WatchedTrigger | None = None
    # Every recorded observation inside the bounded window, oldest first.
    #
    # A window rather than a single price, because a monitor given only the
    # latest one cannot see a level that was crossed and then left: the system
    # would have durably recorded the crossing and still reported that nothing
    # happened. What PULSE promises is to process faithfully what the market
    # layer recorded — not to watch every tick, which it cannot do.
    observations: tuple[PriceObservation, ...] = ()
    # True when more observations existed in the window than the bounded read
    # returns, so a negative answer cannot be trusted.
    window_truncated: bool = Field(default=False, strict=True)
    # The newest recorded observation of any age, used only to explain an empty
    # window: a market that has never been priced and one whose feed has stalled
    # are different situations and deserve different reason codes.
    latest: PriceObservation | None = None
    policy_version: Identifier
    evaluated_at: AwareDatetime


class TriggerEvaluation(Immutable):
    """The result of one deterministic check.

    ``observed_price`` and ``observed_at`` are present only when an observation
    was actually compared, so evidence can never claim a price that no check
    looked at.
    """

    outcome: TriggerOutcome
    reason_code: PulseReasonCode
    evaluated_at: AwareDatetime
    observed_price: Price | None = None
    observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.outcome == TriggerOutcome.TRIGGERED and (
            self.observed_price is None or self.observed_at is None
        ):
            raise ValueError("A trigger must record the observation that satisfied it")
        if (self.observed_price is None) != (self.observed_at is None):
            raise ValueError("An observed price and its source time travel together")
        return self

    @property
    def is_waiting(self) -> bool:
        return self.outcome in WAITING_OUTCOMES
