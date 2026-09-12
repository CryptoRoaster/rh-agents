"""VECTOR contracts: what a trade setup is, and what it deliberately is not.

VECTOR proposes a precise setup — a side, a condition under which to act, a level
at which the idea is wrong, objectives, and a moment after which the proposal
expires. It is the first specialist whose output describes an *action*, which
makes the boundary around it the important part of the design.

A proposal is not an authorization. Nothing here can approve a trade, size a
position, choose a route, set a slippage tolerance, create a risk binding or move
a TradeCase into an executable state. Those belong to PULSE, ANCHOR and SENTINEL,
and the schema below has no field through which any of them could be expressed —
so a model that tried could not, and a prompt that asked would have nowhere to
put the answer.

The other half of the design is the contract with a future PULSE. A setup whose
trigger is prose cannot be watched by anything but another model, so the trigger
here is a tiny explicit grammar that a deterministic evaluator can apply to a
market observation without reasoning about it at all.

**Price orientation.** Every level in this module is **USD per one base-asset
unit**, because that is the only orientation the recorded market layer provides:
``Measurement.value_usd``. There is no quote-denominated price anywhere, so there
is no reciprocal to get backwards, and every level shares one unit by
construction.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.core.models import Side
from src.markets.models import Availability

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
# Money never passes through a float, and the scale matches the ledger's.
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
# Free text from a model is bounded hard: a rationale, never a transcript, and
# never hidden reasoning.
SafeSummary = Annotated[str, Field(min_length=1, max_length=400)]

VECTOR_OUTPUT_SCHEMA_VERSION: Literal[1] = 1

# The one price orientation this system records, stated everywhere it matters so
# no later reader has to infer it.
PRICE_BASIS: Literal["USD_PER_BASE_UNIT"] = "USD_PER_BASE_UNIT"


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SetupKind(StrEnum):
    """The shapes of setup this phase supports, and nothing beyond them.

    Two, deliberately. Each names both what the trader is waiting for and what
    geometry the validator will hold it to, so "enter on strength" cannot be a
    setup: it says nothing a watcher could evaluate and nothing a checker could
    refuse.

    Both are long. The paper execution service is long-only weighted-average-cost,
    so a short setup would describe something this system cannot do, and
    proposing one would be a lie about capability rather than a strategy.
    """

    BREAKOUT_LONG = "BREAKOUT_LONG"
    PULLBACK_LONG = "PULLBACK_LONG"


class TriggerType(StrEnum):
    """The complete grammar a future PULSE has to implement.

    Three comparisons against an observed price. No indicator, no confirmation,
    no "momentum": a trigger that needed a model to evaluate it would put a
    second probabilistic judgement between the setup and the act, and there would
    be no way to say afterwards what the system had been waiting for.
    """

    PRICE_GTE = "PRICE_GTE"
    PRICE_LTE = "PRICE_LTE"
    PRICE_IN_RANGE = "PRICE_IN_RANGE"


class TriggerCondition(Immutable):
    """Exactly what must become true, in terms a comparison can decide.

    ``valid_from`` and ``expires_at`` bound the watch: a condition with no end is
    a standing instruction, and nothing in this system is entitled to leave one
    behind.
    """

    type: TriggerType
    price_basis: Literal["USD_PER_BASE_UNIT"] = PRICE_BASIS
    # Present for the two threshold comparisons, absent for the range.
    reference_price: Price | None = None
    # Present for the range comparison, absent for the thresholds.
    zone_low: Price | None = None
    zone_high: Price | None = None
    valid_from: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def shape_matches_type(self) -> Self:
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

    def is_met(self, price: Decimal) -> bool:
        """Whether an observed price satisfies this condition.

        Deterministic and total, so a future PULSE can evaluate a setup without
        a model, and so this module's own tests can prove what the grammar means
        rather than describing it.
        """
        if self.type == TriggerType.PRICE_GTE:
            assert self.reference_price is not None
            return price >= self.reference_price
        if self.type == TriggerType.PRICE_LTE:
            assert self.reference_price is not None
            return price <= self.reference_price
        assert self.zone_low is not None and self.zone_high is not None
        return self.zone_low <= price <= self.zone_high


class VectorReasonCode(StrEnum):
    """Structured rationale a later FUSE can compare without reading prose.

    Only observations the supplied context can actually support. There is no
    recorded price history in this system, so there is deliberately no reason
    code that would invite a model to describe a trend it was never shown.
    """

    PRICE_AVAILABLE = "PRICE_AVAILABLE"
    LIQUIDITY_PRESENT = "LIQUIDITY_PRESENT"
    LIQUIDITY_THIN = "LIQUIDITY_THIN"
    VOLUME_PRESENT = "VOLUME_PRESENT"
    VOLUME_ABSENT = "VOLUME_ABSENT"
    ONCHAIN_CLEAR = "ONCHAIN_CLEAR"
    ONCHAIN_CONCERN = "ONCHAIN_CONCERN"
    SOCIAL_SUPPORTIVE = "SOCIAL_SUPPORTIVE"
    SOCIAL_WEAK = "SOCIAL_WEAK"
    SOCIAL_MANIPULATION_CONCERN = "SOCIAL_MANIPULATION_CONCERN"
    DISCOVERY_INTERESTING = "DISCOVERY_INTERESTING"


class ObservedMeasurement(Immutable):
    """A recorded measurement with its availability preserved exactly.

    UNKNOWN and an available zero stay different facts all the way to the model.
    A missing price is never serialized as 0, because a setup built on a zero
    that meant "we do not know" would be arithmetic on a fiction.
    """

    observation_id: UUID
    status: Availability
    value_usd: Amount | None = None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def availability_matches_value(self) -> Self:
        if (self.status == Availability.AVAILABLE) != (self.value_usd is not None):
            raise ValueError("AVAILABLE requires a value; unknown/unavailable values must be null")
        return self


class VectorMarketContext(Immutable):
    """Everything VECTOR is allowed to see about the market, and nothing else.

    One snapshot, because one snapshot is what the market layer records and
    exposes. There is no price history here and none is invented: the reader
    deliberately returns only the newest observation per stream, so a candle
    series assembled from sparse snapshots would be a fabrication wearing the
    name of market data.
    """

    snapshot_id: UUID
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    base_asset_id: Identifier
    quote_asset_id: Identifier
    base_symbol: Identifier
    provider: Identifier
    is_fixture: bool = Field(strict=True)
    observed_at: AwareDatetime
    age_seconds: int = Field(ge=0)
    price_basis: Literal["USD_PER_BASE_UNIT"] = PRICE_BASIS
    price: ObservedMeasurement
    liquidity: ObservedMeasurement
    volume: ObservedMeasurement
    volume_window_seconds: int = Field(gt=0)

    @property
    def observation_ids(self) -> frozenset[UUID]:
        return frozenset(
            {
                self.snapshot_id,
                self.price.observation_id,
                self.liquidity.observation_id,
                self.volume.observation_id,
            }
        )


class EvidenceSummary(Immutable):
    """One other specialist's current conclusion, flattened to what VECTOR needs.

    Bounded codes and identifiers rather than another role's payload, so VECTOR
    reads a conclusion without inheriting the reasoning behind it — and so no
    prose written for one audience becomes an instruction to another.
    """

    evidence_id: UUID
    evidence_type: Identifier
    status: Identifier
    acceptance: Identifier
    headline: Identifier | None = None
    codes: tuple[Identifier, ...] = Field(default=(), max_length=12)


class VectorTaskInput(Immutable):
    """The exact view VECTOR reasons over.

    No session, repository, provider client, RPC client, wallet, signer or
    executor appears here, and there is no field through which one could arrive.
    ``supersedes_evidence_id`` is runtime bookkeeping for the envelope and is
    never shown to the model.
    """

    trade_case_id: UUID
    task_id: UUID
    market: VectorMarketContext
    # Whatever the workflow already considers current and usable. Absent roles
    # are absent rather than defaulted, because "no on-chain verdict yet" and "a
    # clean on-chain verdict" are different situations.
    evidence: tuple[EvidenceSummary, ...] = Field(default=(), max_length=6)
    policy_version: Identifier
    evaluated_at: AwareDatetime
    supersedes_evidence_id: UUID | None = None

    @property
    def latest_price(self) -> Decimal:
        """The price every level is judged against. Only valid once admitted."""
        assert self.market.price.value_usd is not None
        return self.market.price.value_usd

    @property
    def evidence_ids(self) -> frozenset[UUID]:
        return frozenset(item.evidence_id for item in self.evidence)


class VectorSetupProposal(Immutable):
    """The model's bounded output: a setup, and nothing that could execute one.

    Every field a trade would additionally need — size, notional, portfolio
    fraction, route, venue preference, slippage tolerance, gas, a risk verdict —
    is absent, and ``extra="forbid"`` means proposing one is a parse error rather
    than a field somebody downstream might read. That is the authority boundary,
    expressed as a schema instead of an instruction.

    The model does not mint the setup identity either. Identity is derived by the
    runtime from the geometry and the input, so two identical proposals are the
    same setup and a changed level is a different one.
    """

    schema_version: Literal[1] = VECTOR_OUTPUT_SCHEMA_VERSION
    kind: SetupKind
    side: Side
    entry_low: Price
    entry_high: Price
    invalidation_price: Price
    targets: tuple[Price, ...] = Field(min_length=1, max_length=3)
    expires_at: AwareDatetime
    reason_codes: tuple[VectorReasonCode, ...] = Field(min_length=1, max_length=8)
    cited_observation_ids: tuple[UUID, ...] = Field(default=(), max_length=8)
    cited_evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=6)
    summary: SafeSummary

    @model_validator(mode="after")
    def internally_coherent(self) -> Self:
        if self.entry_low > self.entry_high:
            raise ValueError("An entry zone cannot start above where it ends")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Targets must be distinct")
        if list(self.targets) != sorted(self.targets):
            raise ValueError("Targets must be ordered")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Reason codes must be distinct")
        if len(set(self.cited_observation_ids)) != len(self.cited_observation_ids):
            raise ValueError("Cited observations must be distinct")
        return self


class VectorSetup(Immutable):
    """The accepted setup, as the runtime records it.

    Built from a validated proposal plus system-owned identity and provenance.
    This is what a future PULSE watches and what a future FUSE compares, so
    everything it needs is a field rather than a sentence.
    """

    setup_id: UUID
    setup_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Identifier
    kind: SetupKind
    side: Side
    price_basis: Literal["USD_PER_BASE_UNIT"] = PRICE_BASIS
    entry_low: Price
    entry_high: Price
    # The level at which the idea is wrong. Explicitly **not** a stop order and
    # explicitly not a guaranteed exit price: VECTOR cannot place an order, and
    # nothing here promises a fill. What it states is when the thesis has failed.
    invalidation_price: Price
    targets: tuple[Price, ...] = Field(min_length=1, max_length=3)
    trigger: TriggerCondition
    expires_at: AwareDatetime
    reason_codes: tuple[VectorReasonCode, ...] = Field(min_length=1, max_length=8)
    summary: SafeSummary
    reference_price: Price
    input_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
