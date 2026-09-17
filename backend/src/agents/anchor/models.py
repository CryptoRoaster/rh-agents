"""ANCHOR contracts: what executable capacity is, and what it deliberately is not.

ANCHOR answers one question — *for the exact setup that just triggered, what can
the current executable market support?* — and answers it from quotes, with
arithmetic. There is no model here, no prompt and no provider: a comparison of
Decimals has one correct answer, and a probabilistic one would put variance at
the execution boundary where it can be measured in money.

Three distinctions carry the design.

**A capacity is not a maximum.** If every size tested passed, the honest answer
is *the market supports at least this much*, not *this much is the limit*. The
difference matters downstream: SENTINEL sizing against a figure it believes is a
ceiling behaves differently from one it knows is a floor. The type system makes
the two impossible to confuse.

**A deviation is not slippage.** A quote's effective price differs from the
reference for several reasons at once — depth, fees, spread, the time between
the two readings. ANCHOR measures that difference and calls it a deviation. It
does not know what slippage a future trade will realise, and nothing here
pretends to.

**Market capacity is not an authorisation.** SENTINEL may permit far less than
the market supports, for reasons ANCHOR cannot see. Nothing in this module names
a position size, an approval or a limit, and the field that carries capacity is
deliberately named so it cannot be mistaken for one.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.markets.quotes import QuoteFailure

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Notional = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Bps = Annotated[Decimal, Field(allow_inf_nan=False, max_digits=38, decimal_places=18)]

ANCHOR_OUTPUT_SCHEMA_VERSION: Literal[1] = 1


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CapacitySemantics(StrEnum):
    """What the reported capacity figure actually claims.

    The whole point of this enum is that `AT_LEAST` must never be readable as a
    maximum. A bounded search that passed every size it tried has learned a lower
    bound and nothing else; reporting the top of its ladder as a limit would
    understate the market and, far worse, would teach a downstream reader to
    treat a tested figure as a measured one.
    """

    # A size failed, so the capacity is bracketed between a tested pass and a
    # tested rejection. The figure is the largest size that passed.
    BOUNDED = "BOUNDED"
    # Every tested size passed. The figure is the largest size *tried*, and the
    # real capacity is somewhere at or above it.
    AT_LEAST = "AT_LEAST"
    # Even the smallest tested size failed on its merits. The market is there and
    # it will not support this.
    NONE = "NONE"
    # Capacity could not be established at all. Not a statement about the market.
    UNKNOWN = "UNKNOWN"


class RejectionReason(StrEnum):
    """Why one ladder point was not accepted."""

    EXECUTION_DEVIATION_TOO_HIGH = "EXECUTION_DEVIATION_TOO_HIGH"
    PROVIDER_IMPACT_TOO_HIGH = "PROVIDER_IMPACT_TOO_HIGH"
    ROUTE_TOO_COMPLEX = "ROUTE_TOO_COMPLEX"
    NO_OUTPUT = "NO_OUTPUT"
    QUOTE_TOO_STALE = "QUOTE_TOO_STALE"
    QUOTE_IN_FUTURE = "QUOTE_IN_FUTURE"
    ASSET_MISMATCH = "ASSET_MISMATCH"
    CHAIN_MISMATCH = "CHAIN_MISMATCH"
    NO_ROUTE = "NO_ROUTE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    # Our USD valuation of the input and the provider's own disagree by more
    # than the policy tolerates. That points at wrong decimals, a wrong token or
    # a stale price — all of which make the quote's economics unreadable.
    USD_VALUATION_DISAGREEMENT = "USD_VALUATION_DISAGREEMENT"


class AnchorReasonCode(StrEnum):
    """Why an assessment concluded what it did."""

    CAPACITY_BRACKETED = "CAPACITY_BRACKETED"
    CAPACITY_AT_LEAST_TESTED_CEILING = "CAPACITY_AT_LEAST_TESTED_CEILING"
    NO_EXECUTABLE_CAPACITY = "NO_EXECUTABLE_CAPACITY"
    NO_ROUTE = "NO_ROUTE"
    QUOTES_UNAVAILABLE = "QUOTES_UNAVAILABLE"
    LADDER_INCOHERENT = "LADDER_INCOHERENT"
    REFERENCE_UNAVAILABLE = "REFERENCE_UNAVAILABLE"
    REFERENCE_TOO_STALE = "REFERENCE_TOO_STALE"
    # No authoritative USD value for the payment asset, so a USD capacity figure
    # cannot be produced. Reported rather than approximated.
    QUOTE_ASSET_USD_VALUE_UNAVAILABLE = "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"


class QuotedPoint(Immutable):
    """One tested size and what the market said about it.

    Kept whether it passed or failed, because "why did ANCHOR say five hundred?"
    is answered by the rejection as much as by the acceptance. A digest alone
    would prove the ladder was the same without saying what it contained.
    """

    # The USD size this rung actually tested. Not the policy's target: the
    # payment asset has a smallest unit, so the amount that could be sent is
    # recorded rather than the amount that was wanted.
    notional_usd: Notional
    # The exact payment-asset amount the provider was asked for, in human units.
    # Kept because "why did ANCHOR send that number?" must be answerable without
    # re-deriving it from a price that has since moved.
    amount_in_tokens: Notional
    accepted: bool = Field(strict=True)
    # Present when a quote was obtained; absent when the provider refused.
    amount_out: int | None = Field(default=None, strict=True, ge=0)
    # The provider's own USD valuation of the input, when published. A
    # cross-check that was made, recorded so the check is auditable.
    provider_amount_in_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    # USD per unit of the asset being bought, so it is comparable with the
    # reference. The provider answers in payment-asset units; the valuation
    # above is what makes the two the same kind of number.
    effective_price_usd: Price | None = None
    execution_deviation_bps: Bps | None = None
    provider_price_impact_bps: Bps | None = None
    route_hops: int | None = Field(default=None, strict=True, ge=0)
    venues: tuple[Identifier, ...] = Field(default=(), max_length=32)
    quoted_at: AwareDatetime | None = None
    source_block_number: int | None = Field(default=None, strict=True, ge=0)
    rejection: RejectionReason | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.accepted and self.rejection is not None:
            raise ValueError("An accepted point cannot also carry a rejection")
        if not self.accepted and self.rejection is None:
            raise ValueError("A rejected point must say why")
        if self.accepted and (self.effective_price_usd is None or self.amount_out is None):
            raise ValueError("An accepted point must record what it was quoted")
        return self


class ExecutionAssessment(Immutable):
    """The deterministic conclusion, with the ladder that produced it.

    ``market_capacity_notional`` is named at length on purpose. It is what the
    *market* was shown to support, not what anyone is permitted to trade — and
    it is meaningless without the semantics beside it, which is why the two
    always travel together.
    """

    policy_version: Identifier
    semantics: CapacitySemantics
    reason_code: AnchorReasonCode
    # The largest size that was *tested and accepted*, in USD. Named that way
    # because that is all it is: nothing here proves the market's maximum, and
    # nothing proves anything about the untested sizes in between. Absent when
    # nothing was established, because a zero would read as "the market supports
    # nothing", which is a different claim.
    largest_tested_acceptable_notional_usd: Notional | None = None
    # The smallest size that was *tested and rejected*, when one was. Together
    # with the figure above this brackets the answer without interpolating
    # between the two or claiming either is a boundary.
    first_tested_rejected_notional_usd: Notional | None = None
    reference_price: Price
    quote_asset_usd_price: Price
    effective_price_usd_at_capacity: Price | None = None
    execution_deviation_bps_at_capacity: Bps | None = None
    ladder: tuple[QuotedPoint, ...] = Field(min_length=1, max_length=12)
    quote_requests: int = Field(ge=0)
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        # UNKNOWN and NONE are the two verdicts with no figure to report, and
        # both must be reported without one: a zero capacity would read as a
        # measurement of an empty market rather than the absence of a measurement.
        if self.semantics in (CapacitySemantics.UNKNOWN, CapacitySemantics.NONE):
            if self.largest_tested_acceptable_notional_usd is not None:
                raise ValueError("No capacity was established, so none may be reported")
        elif self.largest_tested_acceptable_notional_usd is None:
            raise ValueError("An established capacity must carry its figure")
        if (
            self.semantics == CapacitySemantics.AT_LEAST
            and self.first_tested_rejected_notional_usd is not None
        ):
            raise ValueError("Nothing was rejected, so the capacity is not bracketed")
        if (
            self.semantics == CapacitySemantics.BOUNDED
            and self.first_tested_rejected_notional_usd is None
        ):
            raise ValueError("A bracketed capacity must name the size that failed")
        if (
            self.largest_tested_acceptable_notional_usd is not None
            and self.first_tested_rejected_notional_usd is not None
            and self.first_tested_rejected_notional_usd
            <= self.largest_tested_acceptable_notional_usd
        ):
            raise ValueError("The rejected size must sit above the supported one")
        return self

    @property
    def is_executable(self) -> bool:
        """Whether the market was shown to support anything at all."""
        return self.largest_tested_acceptable_notional_usd is not None


class QuoteAttempt(Immutable):
    """One ladder step as the context assembled it, before any judgement.

    Either a quote or the typed reason there is none. The separation between a
    market fact and a failure to observe survives all the way to here, because
    the assessment treats them differently and must not have to guess which it
    is looking at.
    """

    notional_usd: Notional
    amount_in_tokens: Notional
    amount_in: int = Field(strict=True, gt=0)
    quote: object | None = None
    failure: QuoteFailure | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.quote is None) == (self.failure is None):
            raise ValueError("A ladder step is either a quote or a stated failure")
        return self


class ReferenceMarket(Immutable):
    """The independent current price the quotes are judged against.

    Never the setup's entry level and never the trigger's threshold: those are
    historical statements about what somebody was waiting for. A deviation
    measured against them would describe how far the market has moved since,
    which is not what execution cost means.
    """

    snapshot_id: UUID
    observation_id: UUID
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    provider: Identifier
    price: Price
    price_basis: Literal["USD_PER_BASE_UNIT"]
    # Recorded pool liquidity. Context only, and never the source of a capacity
    # figure: reserves are not an assurance that any particular size can be
    # traded through them at an acceptable price.
    liquidity_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    observed_at: AwareDatetime
    age_seconds: int = Field(ge=0)


class QuoteAssetValuation(Immutable):
    """What one unit of the payment asset is worth in USD, from an observation.

    This is the bridge between the two units this system speaks, and it exists
    because nothing else could supply it honestly. The ladder is expressed in
    USD because SENTINEL sizes in USD; the provider is asked in base units of
    the payment asset; and the two are the same number only when that asset
    happens to trade at a dollar.

    It is always a recorded observation of the asset, never a peg, never a guess
    from a symbol, and never the provider's own valuation of the order — that
    one arrives with the answer and so cannot say how much to send.
    """

    asset_id: Identifier
    observation_id: UUID
    snapshot_id: UUID
    provider: Identifier
    usd_per_token: Price
    observed_at: AwareDatetime
    age_seconds: int = Field(ge=0)


class AnchorMarketContext(Immutable):
    """The exact market, assets and decimals an assessment is bound to."""

    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    base_asset_id: Identifier
    quote_asset_id: Identifier
    base_token: Identifier
    quote_token: Identifier
    base_decimals: int = Field(ge=0, le=36)
    quote_decimals: int = Field(ge=0, le=36)


class AnchorTaskInput(Immutable):
    """Everything ANCHOR is given, and nothing else.

    No session, provider client, RPC client, wallet, signer or executor appears
    here, and there is no field through which one could arrive. The quotes have
    already been obtained by trusted infrastructure; what reaches the worker is
    the answer, never the means of asking.
    """

    trade_case_id: UUID
    task_id: UUID
    setup_evidence_id: UUID
    setup_id: UUID
    setup_fingerprint: Identifier
    trigger_evidence_id: UUID
    market: AnchorMarketContext
    reference: ReferenceMarket | None = None
    # Absent only when no quote could be requested at all, which is itself the
    # reason the assessment cannot be made.
    quote_asset_valuation: QuoteAssetValuation | None = None
    ladder: tuple[QuoteAttempt, ...] = Field(default=(), max_length=12)
    quote_requests: int = Field(default=0, ge=0)
    policy_version: Identifier
    evaluated_at: AwareDatetime
    # The assessment this one replaces, when ANCHOR is assessing execution for a
    # case that already has an answer. Runtime bookkeeping for the envelope, and
    # never an input to the assessment: what the market will serve does not
    # depend on what it served before.
    supersedes_evidence_id: UUID | None = None
