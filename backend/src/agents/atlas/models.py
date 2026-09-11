"""ATLAS on-chain intelligence contracts.

ATLAS is safety-critical, so three things are kept rigorously apart:

* **Fact availability** — could this be observed at all (AVAILABLE / UNKNOWN /
  UNAVAILABLE), with provenance;
* **Safety verdict** — what a deterministic versioned policy concludes from the
  facts (CLEAR / BLOCKED / INSUFFICIENT_DATA);
* **Model interpretation** — advisory commentary that has no authority.

Known-bad is never the same as unknown. A measured violation is an available
fact that blocks; an unobtainable fact is insufficient data that also blocks,
for a different reason and with a different remedy.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.markets.models import Availability, MarketIdentity

EvmAddress = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{40}$")]
Hash32 = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
SafeSummary = Annotated[str, Field(min_length=1, max_length=600)]
Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]

ATLAS_OUTPUT_SCHEMA_VERSION: Literal[1] = 1

# The one canonical burn address recognised here. Nothing is treated as burned
# because its hex "looks like" 0xdead; an unproven sink stays an ordinary holder.
ZERO_ADDRESS = "0x" + "0" * 40
BURN_ADDRESSES = frozenset({ZERO_ADDRESS, "0x" + "0" * 36 + "dead"})


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AtlasDomain(StrEnum):
    """The three fact domains Phase 2A already distinguishes for on-chain evidence."""

    CONTRACT = "CONTRACT"
    HOLDERS = "HOLDERS"
    ORIGIN = "ORIGIN"


class AtlasVerdict(StrEnum):
    CLEAR = "CLEAR"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AtlasSourceFailure(StrEnum):
    """Why a source could not answer, mapped safely from provider detail."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNSUPPORTED_CHAIN = "UNSUPPORTED_CHAIN"
    UNSUPPORTED_ENDPOINT = "UNSUPPORTED_ENDPOINT"
    REQUIRES_ARCHIVE = "REQUIRES_ARCHIVE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CHAIN_MISMATCH = "CHAIN_MISMATCH"
    TOKEN_MISMATCH = "TOKEN_MISMATCH"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    SUPPLY_INCONSISTENT = "SUPPLY_INCONSISTENT"
    DENOMINATOR_UNKNOWN = "DENOMINATOR_UNKNOWN"


class HolderCompleteness(StrEnum):
    """How much of the holder universe the source actually proved.

    ``COMPLETE`` means every holder row was retrieved. ``TOP_N_ONLY`` means a
    provably balance-ordered prefix was retrieved, which is sufficient for a
    top-N concentration against an independently known supply but says nothing
    about the rest of the distribution. ``UNKNOWN`` means neither could be
    established, and no concentration metric may be derived from it.
    """

    COMPLETE = "COMPLETE"
    TOP_N_ONLY = "TOP_N_ONLY"
    UNKNOWN = "UNKNOWN"


class HolderObservationBasis(StrEnum):
    """What the holder observation time actually refers to.

    ``SOURCE_BLOCK`` means the provider named the block its holder state belongs
    to and the timestamp is that block's chain time. ``RESPONSE_TIME`` means the
    provider only guarantees "current" state with no block provenance, so the
    moment of the response is the best anchor available — a materially weaker
    assurance that is recorded rather than disguised.
    """

    SOURCE_BLOCK = "SOURCE_BLOCK"
    RESPONSE_TIME = "RESPONSE_TIME"


class OriginVerification(StrEnum):
    """Whether the creation claim was independently checked against the chain.

    A provider returning well-formed JSON is not verification. Only a creation
    receipt whose ``contractAddress`` equals the token counts as confirmed.
    """

    RECEIPT_CONFIRMED = "RECEIPT_CONFIRMED"
    UNVERIFIED = "UNVERIFIED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class ProxyObservation(StrEnum):
    """Proxy detection is an observation, not a proof of absence.

    ``EIP1967_SLOTS_EMPTY`` means the documented slots were read and were empty.
    That is not the same as "this contract is not a proxy": other proxy patterns
    exist and are not checked here.
    """

    EIP1967_DETECTED = "EIP1967_DETECTED"
    EIP1967_SLOTS_EMPTY = "EIP1967_SLOTS_EMPTY"
    NOT_CHECKED = "NOT_CHECKED"


class AtlasReasonCode(StrEnum):
    """Reason codes, split by what they mean for remediation."""

    # Data quality: the fact could not be established.
    CONTRACT_FACTS_UNAVAILABLE = "CONTRACT_FACTS_UNAVAILABLE"
    HOLDER_SOURCE_NOT_CONFIGURED = "HOLDER_SOURCE_NOT_CONFIGURED"
    HOLDER_FACTS_UNAVAILABLE = "HOLDER_FACTS_UNAVAILABLE"
    ORIGIN_SOURCE_NOT_CONFIGURED = "ORIGIN_SOURCE_NOT_CONFIGURED"
    ORIGIN_FACTS_UNAVAILABLE = "ORIGIN_FACTS_UNAVAILABLE"
    TOTAL_SUPPLY_UNKNOWN = "TOTAL_SUPPLY_UNKNOWN"
    SNAPSHOT_SKEW_EXCEEDED = "SNAPSHOT_SKEW_EXCEEDED"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"

    # Risk blockers: the fact was established and violates policy.
    CHAIN_ID_MISMATCH = "CHAIN_ID_MISMATCH"
    CONTRACT_CODE_ABSENT = "CONTRACT_CODE_ABSENT"
    TOTAL_SUPPLY_ZERO = "TOTAL_SUPPLY_ZERO"
    HOLDER_CONCENTRATION_EXCEEDED = "HOLDER_CONCENTRATION_EXCEEDED"
    PROXY_ADMIN_PRESENT = "PROXY_ADMIN_PRESENT"


DATA_QUALITY_REASONS = frozenset(
    {
        AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE,
        AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED,
        AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE,
        AtlasReasonCode.ORIGIN_SOURCE_NOT_CONFIGURED,
        AtlasReasonCode.ORIGIN_FACTS_UNAVAILABLE,
        AtlasReasonCode.TOTAL_SUPPLY_UNKNOWN,
        AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED,
        AtlasReasonCode.SNAPSHOT_STALE,
    }
)

RISK_BLOCKER_REASONS = frozenset(
    {
        AtlasReasonCode.CHAIN_ID_MISMATCH,
        AtlasReasonCode.CONTRACT_CODE_ABSENT,
        AtlasReasonCode.TOTAL_SUPPLY_ZERO,
        AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED,
        AtlasReasonCode.PROXY_ADMIN_PRESENT,
    }
)


class ChainSnapshot(Immutable):
    """Which chain and block the contract facts were read against.

    ``block_timestamp`` is chain time and is what freshness is judged by.
    ``observed_at`` records when we happened to fetch it, which is useful for
    audit but must never be mistaken for how current the data is.
    """

    chain: Identifier
    network: Identifier
    chain_id: int = Field(gt=0)
    block_number: int = Field(ge=0)
    block_hash: Hash32 | None = None
    block_timestamp: AwareDatetime
    observed_at: AwareDatetime
    source: Identifier


class ContractFacts(Immutable):
    """Deterministically observable token contract state.

    Every field is null unless the corresponding read actually succeeded. Nothing
    is inferred from a failed or guessed call, because ERC-20 deployments are
    heterogeneous and a reverting method proves nothing about intent.
    """

    status: Availability = Availability.UNKNOWN
    failure: AtlasSourceFailure | None = None
    source: Identifier
    observed_block: int | None = Field(default=None, ge=0)
    code_present: bool | None = None
    code_hash: Hash32 | None = None
    decimals: int | None = Field(default=None, ge=0, le=36)
    total_supply_raw: int | None = Field(default=None, ge=0)
    proxy: ProxyObservation = ProxyObservation.NOT_CHECKED
    implementation_address: EvmAddress | None = None
    admin_address: EvmAddress | None = None

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None:
                raise ValueError("Available contract facts cannot carry a source failure")
            if self.code_present is None or self.observed_block is None:
                raise ValueError("Available contract facts require code presence and a block")
        elif self.code_present is not None or self.code_hash is not None:
            raise ValueError("Unavailable contract facts must not carry observations")
        return self

    @property
    def total_supply(self) -> Decimal | None:
        """Normalized supply, exact. Raw integer and decimals are kept alongside."""
        if self.total_supply_raw is None or self.decimals is None:
            return None
        return Decimal(self.total_supply_raw) / (Decimal(10) ** self.decimals)


class HolderShare(Immutable):
    address: EvmAddress
    balance_raw: int = Field(ge=0)
    share: Ratio
    is_burn_address: bool = Field(strict=True)
    # Advisory only. An explorer label never decides what an address is; this
    # records whether the source said the address has code, nothing more.
    is_contract: bool | None = None


class HolderSourceRow(Immutable):
    """One raw holder row exactly as a provider reported it, before any policy."""

    address: EvmAddress
    balance_raw: int = Field(ge=0)
    is_contract: bool | None = None


class HolderFactsSourceResult(Immutable):
    """The typed answer a holder provider gives. Not yet a safety fact.

    A provider supplies raw rows and provenance. It never supplies a
    concentration, because the denominator is on-chain total supply, which the
    deterministic collector owns and the provider is not trusted to state.
    """

    status: Availability = Availability.UNKNOWN
    failure: AtlasSourceFailure | None = None
    source: Identifier
    chain: Identifier | None = None
    token_address: EvmAddress | None = None
    rows: tuple[HolderSourceRow, ...] = Field(default=(), max_length=2000)
    completeness: HolderCompleteness = HolderCompleteness.UNKNOWN
    observation_basis: HolderObservationBasis | None = None
    # Present only when the provider names the block its holder state belongs to.
    snapshot_block: int | None = Field(default=None, ge=0)
    snapshot_timestamp: AwareDatetime | None = None
    holder_count: int | None = Field(default=None, ge=0)
    # Kept for reconciliation against the chain, never used as the denominator.
    provider_total_supply_raw: int | None = Field(default=None, ge=0)
    requests_made: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None:
                raise ValueError("An available holder result cannot carry a source failure")
            if self.chain is None or self.token_address is None:
                raise ValueError("An available holder result must identify chain and token")
            if self.completeness == HolderCompleteness.UNKNOWN:
                raise ValueError("An available holder result must prove its completeness")
            if self.observation_basis is None or self.snapshot_timestamp is None:
                raise ValueError("An available holder result must carry an observation anchor")
            if (
                self.observation_basis == HolderObservationBasis.SOURCE_BLOCK
                and self.snapshot_block is None
            ):
                raise ValueError("A block-anchored holder result must name its block")
        elif self.rows or self.snapshot_block is not None:
            raise ValueError("An unavailable holder result must not carry observations")
        return self


class HolderFacts(Immutable):
    """Holder distribution, only ever from a source that can actually provide it.

    Vanilla EVM RPC cannot enumerate holders. Nothing here is reconstructed from
    a partial log scan and presented as a complete holder set.
    """

    status: Availability = Availability.UNKNOWN
    failure: AtlasSourceFailure | None = None
    source: Identifier
    observed_at: AwareDatetime | None = None
    observation_basis: HolderObservationBasis | None = None
    completeness: HolderCompleteness = HolderCompleteness.UNKNOWN
    snapshot_block: int | None = Field(default=None, ge=0)
    # Pinned contract block minus holder snapshot block, when both are known.
    # Positive means the holder data is older than the block the contract facts
    # were read at. Negative is the ordinary case, because the pinned block
    # trails the chain head by the confirmation lag while an indexer tracks the
    # head — so this is a signed distance, never a one-directional "lag".
    holder_block_delta: int | None = None
    holder_count: int | None = Field(default=None, ge=0)
    # The denominator actually divided by: on-chain total supply, never the
    # provider's own figure.
    total_supply_raw: int | None = Field(default=None, ge=0)
    top_holders: tuple[HolderShare, ...] = Field(default=(), max_length=50)
    # Raw concentration over the full supply, before any policy adjustment.
    top1_share: Ratio | None = None
    top5_share: Ratio | None = None
    top10_share: Ratio | None = None
    # The same measure with proven burn addresses removed, kept separate so a
    # large position can never be hidden by an adjustment.
    top10_share_excluding_burn: Ratio | None = None
    burned_raw: int | None = Field(default=None, ge=0)
    burned_share: Ratio | None = None

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None:
                raise ValueError("Available holder facts cannot carry a source failure")
            if self.observed_at is None or self.top1_share is None:
                raise ValueError("Available holder facts require an observation time and shares")
            if self.completeness == HolderCompleteness.UNKNOWN:
                raise ValueError("Available holder facts require proven completeness")
            if self.observation_basis is None:
                raise ValueError("Available holder facts require an observation basis")
            if self.total_supply_raw is None:
                raise ValueError("Available holder facts require the denominator they used")
        elif self.top_holders or self.top1_share is not None:
            raise ValueError("Unavailable holder facts must not carry observations")
        return self


class OriginFacts(Immutable):
    """Contract creation provenance.

    A creator is only recorded when a source actually proves it. It is never
    inferred from the first holder, the current owner or the pool creator.
    """

    status: Availability = Availability.UNKNOWN
    failure: AtlasSourceFailure | None = None
    source: Identifier
    creator_address: EvmAddress | None = None
    creation_block: int | None = Field(default=None, ge=0)
    creation_tx_hash: Hash32 | None = None
    # A factory deployment means the creator is code, not a person. Both facts
    # are recorded; neither is interpreted as a developer wallet here.
    factory_address: EvmAddress | None = None
    creator_is_contract: bool | None = None
    verification: OriginVerification = OriginVerification.NOT_ATTEMPTED

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None:
                raise ValueError("Available origin facts cannot carry a source failure")
            if self.creator_address is None:
                raise ValueError("Available origin facts require a proven creator")
        elif self.creator_address is not None:
            raise ValueError("Unavailable origin facts must not name a creator")
        return self


class AtlasOnchainSnapshot(Immutable):
    """Everything ATLAS deterministically established, with provenance."""

    trade_case_id: UUID
    task_id: UUID
    market: MarketIdentity
    token_address: EvmAddress
    chain: ChainSnapshot
    contract: ContractFacts
    holders: HolderFacts
    origin: OriginFacts
    collected_at: AwareDatetime

    @field_validator("token_address")
    @classmethod
    def nonzero_token(cls, value: str) -> str:
        if int(value[2:], 16) == 0:
            raise ValueError("A token contract address cannot be the zero address")
        return value

    @model_validator(mode="after")
    def chain_matches_market(self) -> Self:
        if self.chain.chain != self.market.chain or self.chain.network != self.market.network:
            raise ValueError("Snapshot chain must match the TradeCase market identity")
        return self

    @property
    def oldest_source_observation(self) -> datetime:
        """The oldest moment any contributing source actually observed reality.

        Freshness is anchored here, never to ``collected_at``. Re-running the
        collector against an unchanged provider snapshot must not make old data
        look new, so the time we fetched something is deliberately not part of
        this answer.
        """
        observations = [self.chain.block_timestamp]
        if self.holders.observed_at is not None:
            observations.append(self.holders.observed_at)
        return min(observations)

    def availability(self, domain: AtlasDomain) -> Availability:
        return {
            AtlasDomain.CONTRACT: self.contract.status,
            AtlasDomain.HOLDERS: self.holders.status,
            AtlasDomain.ORIGIN: self.origin.status,
        }[domain]

    @property
    def addresses(self) -> frozenset[str]:
        """Every address ATLAS actually saw. A model may cite nothing else."""
        found = {self.token_address}
        for value in (
            self.contract.implementation_address,
            self.contract.admin_address,
            self.origin.creator_address,
        ):
            if value is not None:
                found.add(value)
        found.update(holder.address for holder in self.holders.top_holders)
        return frozenset(found)


class AtlasFinding(Immutable):
    """One advisory observation from the model. Never authoritative."""

    kind: Literal["VERIFIED_FACT", "INFERENCE"]
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
    statement: Annotated[str, Field(min_length=1, max_length=300)]
    referenced_addresses: tuple[EvmAddress, ...] = Field(default=(), max_length=8)


class AtlasAssessment(Immutable):
    """The model's bounded advisory output.

    It carries no verdict field at all. The safety verdict is computed from the
    snapshot and the policy, so there is nothing here for a model to set.
    """

    schema_version: Literal[1] = ATLAS_OUTPUT_SCHEMA_VERSION
    summary: SafeSummary
    findings: tuple[AtlasFinding, ...] = Field(default=(), max_length=10)
    acknowledged_data_gaps: tuple[AtlasReasonCode, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def gaps_are_data_quality_only(self) -> Self:
        if not set(self.acknowledged_data_gaps) <= DATA_QUALITY_REASONS:
            raise ValueError("A data gap must name a data-quality reason, not a risk blocker")
        return self


class AtlasSafetyDecision(Immutable):
    """The authoritative deterministic outcome. No model input reaches this."""

    verdict: AtlasVerdict
    policy_version: Identifier
    blockers: tuple[AtlasReasonCode, ...] = Field(default=(), max_length=12)
    data_gaps: tuple[AtlasReasonCode, ...] = Field(default=(), max_length=12)
    domain_status: dict[str, str]

    @model_validator(mode="after")
    def verdict_matches_reasons(self) -> Self:
        if not set(self.blockers) <= RISK_BLOCKER_REASONS:
            raise ValueError("Blockers must be risk reasons")
        if not set(self.data_gaps) <= DATA_QUALITY_REASONS:
            raise ValueError("Data gaps must be data-quality reasons")
        if self.verdict == AtlasVerdict.BLOCKED and not self.blockers:
            raise ValueError("A blocked verdict must name at least one blocker")
        if self.verdict == AtlasVerdict.INSUFFICIENT_DATA and not self.data_gaps:
            raise ValueError("Insufficient data must name at least one gap")
        if self.verdict == AtlasVerdict.CLEAR and (self.blockers or self.data_gaps):
            raise ValueError("A clear verdict cannot carry blockers or gaps")
        return self
