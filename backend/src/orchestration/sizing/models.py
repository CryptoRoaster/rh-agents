"""Contracts for a requested PAPER entry size, and for every way of having none.

The whole output of this phase is one typed answer to *how many base tokens does
the configured USD amount buy at the recorded reference price?* — together with
enough provenance that the answer can be recomputed and checked years later.

Three things the answer is deliberately not.

**It is not a maximum.** ANCHOR reports the largest size the market was tested
at and SENTINEL reports a ceiling. Both are numbers in dollars sitting exactly
where a desired size would go, and both are limits. Nothing here reads either.

**It is not an authorisation.** A computed quantity means the sizing input
exists. Whether a trade may happen is SENTINEL's question, asked later, from
portfolio facts this module cannot see and must not anticipate.

**It is not an execution key.** ``input_digest`` identifies the inputs a
quantity was derived from, so recomputation with the same inputs is recognisably
the same reading. It is not an idempotency key for an order, it guarantees
nothing exactly-once, and no durable case-to-fill link exists yet.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from src.core.models import Side, TradingMode
from src.markets.models import exact_number, market_decimal_bounds
from src.orchestration.sizing.canonical import lossless_decimal
from src.orchestration.sizing.policy import PaperSizingPolicy

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# A recorded price, kept at exactly the precision it was observed with. It is
# deliberately *not* narrowed to the ledger's eighteen places: the price is an
# input that was written by something else, and truncating a copy of it would
# make the digest describe a number the market layer never recorded.
ObservedPrice = Annotated[
    Decimal,
    BeforeValidator(exact_number),
    Field(gt=0, allow_inf_nan=False),
    AfterValidator(market_decimal_bounds),
]
# A derived amount, which must survive `Numeric(38, 18)` because that is where
# quantities and notionals live everywhere else in this system.
LedgerAmount = Annotated[
    Decimal,
    BeforeValidator(exact_number),
    Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18),
]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SizingOutcome(StrEnum):
    """What a sizing reading concluded. Exactly one value means success."""

    # A requested size was computed from recorded inputs. This states that the
    # *input* exists — not that the risk input is complete, and not that
    # anything may be traded.
    SIZING_INPUT_AVAILABLE = "SIZING_INPUT_AVAILABLE"


class SizingRefusal(StrEnum):
    """Why no requested size exists. Typed, because "why not?" must be a field.

    Every one of these is a stop. None of them has a fallback value, a default
    or a retry: a system that answers "how much?" with a guess whenever it
    cannot answer properly has not been stopped by any of them.
    """

    # Nobody configured an amount. The standing gap, reported unchanged from
    # what the control plane already reports, so the two cannot drift apart.
    AUTONOMOUS_SIZING_INPUT_MISSING = "AUTONOMOUS_SIZING_INPUT_MISSING"
    # No recorded observation exists for this market at all.
    SIZING_MARKET_NOT_RECORDED = "SIZING_MARKET_NOT_RECORDED"
    # An observation exists but carries no usable price.
    SIZING_PRICE_UNAVAILABLE = "SIZING_PRICE_UNAVAILABLE"
    # The recorded price prices a different asset than this case's base asset.
    SIZING_PRICE_ASSET_MISMATCH = "SIZING_PRICE_ASSET_MISMATCH"
    # The price is older than the policy allows.
    SIZING_PRICE_STALE = "SIZING_PRICE_STALE"
    # The price claims to have been observed after the instant of the reading.
    # Kept distinct from staleness: one is a market that moved on, the other is
    # a clock or a recorder that cannot be trusted about when anything happened.
    SIZING_PRICE_NOT_YET_OBSERVED = "SIZING_PRICE_NOT_YET_OBSERVED"
    # The base asset's decimals are unknown. Never defaulted to eighteen: that
    # assumption turns a hundred-dollar order into a hundred-trillion one on a
    # six-decimal token, and the recorded metadata simply says nothing here.
    SIZING_TOKEN_METADATA_MISSING = "SIZING_TOKEN_METADATA_MISSING"
    # The amount buys less than one representable unit of the base token.
    SIZING_BELOW_MINIMUM_UNIT = "SIZING_BELOW_MINIMUM_UNIT"
    # The quantity does not fit the ledger's own `Numeric(38, 18)` envelope.
    SIZING_QUANTITY_NOT_REPRESENTABLE = "SIZING_QUANTITY_NOT_REPRESENTABLE"
    # The case has no current trade setup, so there is no entry to size.
    SIZING_NO_CURRENT_SETUP = "SIZING_NO_CURRENT_SETUP"
    # The setup is not a long entry.
    SIZING_SIDE_NOT_SUPPORTED = "SIZING_SIDE_NOT_SUPPORTED"
    # The deployment is not in PAPER mode.
    SIZING_MODE_NOT_SUPPORTED = "SIZING_MODE_NOT_SUPPORTED"
    # The case could not be read, or its market identity is incoherent.
    SIZING_CASE_UNAVAILABLE = "SIZING_CASE_UNAVAILABLE"


class SizingPolicySnapshot(Immutable):
    """Every policy parameter a result or its validity depends on.

    Carried on the reading and hashed into its identity, because a version
    string is a label and a label can be reused. Two policies both calling
    themselves `paper-sizing-v1` while tolerating different price ages produce
    different validity windows, and binding only the name made that difference
    invisible: identical digests, and an assessment that expired ninety seconds
    apart depending on which object happened to be configured.

    The freshness bound is held in whole microseconds rather than as a float
    number of seconds, so the value that reaches the hash is exactly the value
    the policy holds.
    """

    version: Identifier
    max_price_age_microseconds: int = Field(strict=True, gt=0)
    supported_sides: tuple[Side, ...] = Field(min_length=1)
    supported_modes: tuple[TradingMode, ...] = Field(min_length=1)
    max_quantity_decimal_places: int = Field(strict=True, ge=0, le=18)
    max_quantity_total_digits: int = Field(strict=True, ge=1, le=38)

    @classmethod
    def of(cls, policy: PaperSizingPolicy) -> "SizingPolicySnapshot":
        """Read a policy into the form that travels with a reading.

        Sets are ordered on the way in, so the same policy always produces the
        same bytes regardless of how a frozenset happened to iterate.
        """
        age = policy.max_price_age
        return cls(
            version=policy.version,
            max_price_age_microseconds=(age.days * 86400 + age.seconds) * 1_000_000
            + age.microseconds,
            supported_sides=tuple(sorted(policy.supported_sides, key=lambda item: item.value)),
            supported_modes=tuple(sorted(policy.supported_modes, key=lambda item: item.value)),
            max_quantity_decimal_places=policy.max_quantity_decimal_places,
            max_quantity_total_digits=policy.max_quantity_total_digits,
        )

    @property
    def canonical(self) -> dict[str, object]:
        """The policy as hashable content, in one fixed shape."""
        return {
            "version": self.version,
            "max_price_age_microseconds": self.max_price_age_microseconds,
            "supported_sides": [item.value for item in self.supported_sides],
            "supported_modes": [item.value for item in self.supported_modes],
            "max_quantity_decimal_places": self.max_quantity_decimal_places,
            "max_quantity_total_digits": self.max_quantity_total_digits,
        }


class ReferencePrice(Immutable):
    """One recorded observation of what one base token is worth in USD.

    Carries its own identity and provenance because a price is only meaningful
    against the asset, the source and the instant it was observed for. There is
    no field here for a setup price or a quote: a setup states where somebody
    would like to act and a quote is an offer for a specific amount, and neither
    is an independent valuation of one token.
    """

    snapshot_id: UUID
    observation_id: UUID
    provider: Identifier
    # The asset this price is *of*. Checked against the case's base asset rather
    # than assumed, because a price taken from the wrong side of a pair is
    # wrong by a factor of the exchange rate and looks perfectly plausible.
    asset_id: Identifier
    price_basis: Literal["USD_PER_BASE_UNIT"] = "USD_PER_BASE_UNIT"
    usd_per_base_unit: ObservedPrice
    observed_at: AwareDatetime


class BaseAssetMetadata(Immutable):
    """The base token's decimals, and where that figure came from.

    Provenance travels with the number because the number alone cannot be
    checked. These decimals are what the market provider recorded when it
    observed the pair; they are not an on-chain read, and the type says so by
    naming the observation rather than claiming verification.
    """

    asset_id: Identifier
    symbol: Identifier
    decimals: int = Field(strict=True, ge=0, le=36)
    source_provider: Identifier
    source_observation_id: UUID
    source_observed_at: AwareDatetime


class SizingAssessment(Immutable):
    """A requested entry size, and everything it was derived from."""

    kind: Literal["sizing_assessment"] = "sizing_assessment"
    outcome: Literal[SizingOutcome.SIZING_INPUT_AVAILABLE] = SizingOutcome.SIZING_INPUT_AVAILABLE
    policy: SizingPolicySnapshot
    trade_case_id: UUID
    base_asset_id: Identifier
    setup_evidence_id: UUID
    side: Side
    trading_mode: TradingMode
    # The configured amount, exactly as configured. Deliberately named for what
    # it is: the notional before fees, gas and slippage. It is not a cash debit
    # and not a worst-case cost — the paper executor adds fees on top of the
    # fill, and SENTINEL computes the worst case from its own limits.
    requested_notional_usd: LedgerAmount
    reference_price: ReferencePrice
    base_asset: BaseAssetMetadata
    # The decimal places actually used, which is the coarser of the token's own
    # and the ledger's eighteen. Recorded beside `base_asset.decimals` so a
    # quantity rounded to something other than the token's smallest unit says so.
    quantity_decimal_places: int = Field(strict=True, ge=0, le=18)
    # The derived amount of base tokens, rounded down.
    quantity: LedgerAmount
    # What that quantity is worth at the reference price, floored to the
    # ledger's precision. Always at or below the requested notional, which is
    # the arithmetic guarantee rounding down exists to provide.
    reference_notional_usd: LedgerAmount
    # The instant this reading stops describing the market, anchored to the
    # price's own observation time rather than to when it was computed.
    # Recomputing cannot move it: a source does not get younger by being read
    # again, and a validity that renewed itself on every read would never expire.
    valid_until: AwareDatetime
    input_digest: Digest

    @property
    def policy_version(self) -> str:
        return self.policy.version

    def is_current_at(self, instant: datetime) -> bool:
        """Whether this reading still describes the market at `instant`.

        Half-open, exactly like every other validity in this system: an evidence
        envelope is `STALE` at `now >= valid_until` and a COMMANDER context is
        current only while `instant < valid_until`. The boundary instant belongs
        to the expired side, so a successful reading is never one that is
        already unusable — see `assess_paper_sizing`, which refuses there.
        """
        return instant < self.valid_until

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.reference_price.asset_id != self.base_asset_id:
            raise ValueError("The reference price must price this case's base asset")
        if self.base_asset.asset_id != self.base_asset_id:
            raise ValueError("Token metadata must describe this case's base asset")
        if self.quantity_decimal_places > self.base_asset.decimals:
            raise ValueError("A quantity cannot be finer than the token's own smallest unit")
        exponent = self.quantity.as_tuple().exponent
        if not isinstance(exponent, int) or -exponent > self.quantity_decimal_places:
            raise ValueError("A quantity must be expressed in the units it was rounded to")
        if self.reference_notional_usd > self.requested_notional_usd:
            raise ValueError("A rounded-down size cannot exceed the requested notional")
        if self.valid_until <= self.reference_price.observed_at:
            raise ValueError("Validity must extend beyond the observation it rests on")
        return self


class SizingRefused(Immutable):
    """No requested size exists, and the typed reason why.

    A refusal is an answer, not an error: it is returned rather than raised so a
    caller has to handle it, and it carries the same binding as a success so the
    audit trail says which case was refused and under which policy.
    """

    kind: Literal["sizing_refused"] = "sizing_refused"
    reason: SizingRefusal
    # The policy the refusal was reached under, whole. Several reasons depend on
    # its content rather than only on its name — a price is stale against a
    # tolerance, a side is unsupported against a set — so a refusal that carried
    # only a version label would not say what it was measured against.
    policy: SizingPolicySnapshot
    trade_case_id: UUID
    base_asset_id: Identifier | None = None

    @property
    def policy_version(self) -> str:
        return self.policy.version


SizingReading = SizingAssessment | SizingRefused


def sizing_input_digest(
    *,
    policy: SizingPolicySnapshot,
    trade_case_id: UUID,
    base_asset_id: str,
    setup_evidence_id: UUID,
    side: Side,
    trading_mode: TradingMode,
    requested_notional_usd: Decimal,
    price: ReferencePrice,
    base_asset: BaseAssetMetadata,
    quantity_decimal_places: int,
) -> str:
    """Hash exactly the inputs a quantity follows from.

    Canonical by construction: fixed key order through `sort_keys`, fixed
    separators, ASCII only, every Decimal through one unambiguous textual form,
    every instant normalised to UTC. No dict, database or arrival ordering can
    reach the hash.

    What is deliberately absent is as load-bearing as what is present. There is
    no read time, no generated identifier, no worker instance and no attempt
    number here — anything that moved between two identical readings would make
    replay indistinguishable from a genuinely different assessment, which is the
    exact failure this digest exists to make impossible.

    The derived quantity is absent too, for the opposite reason: it is the
    output. A digest that included it could never detect a computation that
    changed while its inputs did not.
    """
    canonical = json.dumps(
        {
            "policy": policy.canonical,
            "trade_case_id": str(trade_case_id),
            "base_asset_id": base_asset_id,
            "setup_evidence_id": str(setup_evidence_id),
            "side": side.value,
            "trading_mode": trading_mode.value,
            "requested_notional_usd": lossless_decimal(requested_notional_usd),
            "reference_price": {
                "snapshot_id": str(price.snapshot_id),
                "observation_id": str(price.observation_id),
                "provider": price.provider,
                "asset_id": price.asset_id,
                "price_basis": price.price_basis,
                "usd_per_base_unit": lossless_decimal(price.usd_per_base_unit),
                "observed_at": _instant(price.observed_at),
            },
            "base_asset": {
                "asset_id": base_asset.asset_id,
                "symbol": base_asset.symbol,
                "decimals": base_asset.decimals,
                "source_provider": base_asset.source_provider,
                "source_observation_id": str(base_asset.source_observation_id),
                "source_observed_at": _instant(base_asset.source_observed_at),
            },
            "quantity_decimal_places": quantity_decimal_places,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def _instant(value: datetime) -> str:
    """One textual form per instant, independent of the offset it arrived in."""
    return value.astimezone(UTC).isoformat()
