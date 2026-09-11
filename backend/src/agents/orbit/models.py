"""ORBIT discovery contracts: what the scout is shown, and what it may return.

ORBIT is the discovery specialist. It never approves, sizes, routes or executes
anything. Its only authoritative output is a bounded typed assessment that the
deterministic workflow interprets; a strong ORBIT opinion is not a risk
authorization and cannot bypass ATLAS, VECTOR, PULSE, ANCHOR or SENTINEL.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.markets.models import Availability

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
# Free text from a model is bounded hard: a summary, never a transcript.
SafeSummary = Annotated[str, Field(min_length=1, max_length=400)]

ORBIT_OUTPUT_SCHEMA_VERSION: Literal[1] = 1


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class OrbitClassification(StrEnum):
    """INSUFFICIENT_DATA is never a quiet synonym for NOT_INTERESTING."""

    INTERESTING = "INTERESTING"
    NOT_INTERESTING = "NOT_INTERESTING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class OrbitStrength(StrEnum):
    """Coarse qualitative strength.

    Deliberately not a numeric score: a model's number is not a calibrated
    probability, and nothing downstream may treat it as one. It never overrides a
    blocker and never reaches SENTINEL.
    """

    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class OrbitReasonCode(StrEnum):
    """Only facts the recorded market schema can actually support.

    There is no pool age, holder history or volume trend in the observation
    model, so there is deliberately no reason code that would invite the model to
    invent one.
    """

    PRICE_AVAILABLE = "PRICE_AVAILABLE"
    PRICE_UNKNOWN = "PRICE_UNKNOWN"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    LIQUIDITY_PRESENT = "LIQUIDITY_PRESENT"
    LIQUIDITY_ZERO = "LIQUIDITY_ZERO"
    LIQUIDITY_BELOW_DISCOVERY_FLOOR = "LIQUIDITY_BELOW_DISCOVERY_FLOOR"
    LIQUIDITY_UNKNOWN = "LIQUIDITY_UNKNOWN"
    LIQUIDITY_UNAVAILABLE = "LIQUIDITY_UNAVAILABLE"
    VOLUME_PRESENT = "VOLUME_PRESENT"
    VOLUME_ZERO = "VOLUME_ZERO"
    VOLUME_UNKNOWN = "VOLUME_UNKNOWN"
    VOLUME_UNAVAILABLE = "VOLUME_UNAVAILABLE"
    FIXTURE_DATA = "FIXTURE_DATA"


UNKNOWN_REASON_CODES = frozenset(
    {
        OrbitReasonCode.PRICE_UNKNOWN,
        OrbitReasonCode.PRICE_UNAVAILABLE,
        OrbitReasonCode.LIQUIDITY_UNKNOWN,
        OrbitReasonCode.LIQUIDITY_UNAVAILABLE,
        OrbitReasonCode.VOLUME_UNKNOWN,
        OrbitReasonCode.VOLUME_UNAVAILABLE,
    }
)


class ObservedMeasurement(Immutable):
    """A recorded measurement with its availability preserved exactly.

    UNKNOWN and an available zero are different facts and must stay different all
    the way to the model. A missing value is never serialized as 0.
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


class OrbitCandidateContext(Immutable):
    """Everything ORBIT is allowed to see, and nothing else.

    No other role's evidence, no risk outcome, no TradeCase status, no provider
    client and no credential appears here.
    """

    snapshot_id: UUID
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    venue: Identifier
    base_symbol: Identifier
    quote_symbol: Identifier
    provider: Identifier
    is_fixture: bool = Field(strict=True)
    observed_at: AwareDatetime
    age_seconds: int = Field(ge=0)
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


class OrbitTaskInput(Immutable):
    """The exact snapshot ORBIT reasons over, plus the identity it must not stray from.

    ``discovery_reference`` and ``supersedes_evidence_id`` are runtime bookkeeping
    used to build the evidence envelope. They are never shown to the model, which
    sees only ``candidate``.
    """

    trade_case_id: UUID
    task_id: UUID
    candidate: OrbitCandidateContext
    discovery_liquidity_floor_usd: Amount
    evaluated_at: AwareDatetime
    discovery_reference: UUID
    supersedes_evidence_id: UUID | None = None


class OrbitAssessment(Immutable):
    """The model's bounded output. It cannot express a trade, a size or a route."""

    schema_version: Literal[1] = ORBIT_OUTPUT_SCHEMA_VERSION
    classification: OrbitClassification
    strength: OrbitStrength
    reason_codes: tuple[OrbitReasonCode, ...] = Field(min_length=1, max_length=12)
    data_gaps: tuple[OrbitReasonCode, ...] = Field(default=(), max_length=12)
    cited_observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    pair_id: Identifier
    chain: Identifier
    summary: SafeSummary

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Reason codes must be distinct")
        if len(set(self.cited_observation_ids)) != len(self.cited_observation_ids):
            raise ValueError("Cited observations must be distinct")
        if not set(self.data_gaps) <= UNKNOWN_REASON_CODES:
            raise ValueError("A data gap must name a missing or unavailable input")
        if self.classification == OrbitClassification.INSUFFICIENT_DATA and not self.data_gaps:
            raise ValueError("Insufficient data must say which input was missing")
        return self


class OrbitEvidencePayloadView(Immutable):
    """What the runtime derives for evidence, after validation."""

    classification: OrbitClassification
    reason_codes: tuple[OrbitReasonCode, ...]
    summary: SafeSummary


def age_seconds(observed_at: datetime, now: datetime) -> int:
    return max(0, int((now - observed_at).total_seconds()))
