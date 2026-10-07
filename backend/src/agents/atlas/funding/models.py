"""CREATOR_FUNDING_GRAPH V1: direct native transfers from the token's origin creator.

A measurement, not a verdict. It records which distinct addresses the creator
named by the origin source paid native currency to, directly and successfully,
between the token's creation block and the pinned ATLAS block -- and how many
of those addresses appear among the token's observed economic holders.

What it deliberately is not: a funding graph in the economic sense. It follows
no second hop, no ERC-20 or stablecoin transfer, no internal call and no trace;
it applies no label, reputation or "probably the dev" heuristic; and it calls
nobody a buyer, a bot, an insider or a sybil. ``scope`` names exactly what was
measured, so the figures are never read as more than that.

A count is only exact when the source history was proven to reach back to the
creation block. Anything cut short by a bound is ``LOWER_BOUND``: never a zero
that means "none".

V2 adds ``prelaunch``, a separate measurement of the same creator's direct
native transfers *before* the creation block, in three lookback windows ending
at it. It never changes a V1 field: V1 still means the launch window only.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from src.agents.atlas.primitives import (
    AtlasSourceFailure,
    EvmAddress,
    Hash32,
    Identifier,
    Immutable,
    Ratio,
)
from src.markets.models import Availability

FUNDING_SCOPE = "DIRECT_NATIVE_FROM_ORIGIN_CREATOR"
# Edges written into durable evidence, smallest block first. The counts and the
# digest beside them always cover every edge that was read.
MAX_DURABLE_EDGES = 16

PRELAUNCH_SCOPE = "DIRECT_NATIVE_FROM_ORIGIN_CREATOR_PRELAUNCH"
PRELAUNCH_VERSION = 2
# The chain-side source of the creation time: the creation block's own header.
CREATION_TIME_SOURCE = "evm-rpc:eth_getBlockByNumber"
# The span of the rolling burst metric.
ROLLING_INTERVAL = timedelta(minutes=10)


class FundingCoverage(StrEnum):
    # Every transaction of the creator in the window was read.
    COMPLETE = "COMPLETE"
    # A bound ended the read before the creation block was reached: every
    # figure is what was seen, and the truth is that or more.
    LOWER_BOUND = "LOWER_BOUND"


class FundingGap(StrEnum):
    # No origin creator was established, so there is no root to measure from.
    ORIGIN_UNAVAILABLE = "FUNDING_ORIGIN_UNAVAILABLE"
    # The origin names no creation block, or one after the pinned block.
    WINDOW_UNAVAILABLE = "FUNDING_WINDOW_UNAVAILABLE"
    SOURCE_NOT_CONFIGURED = "FUNDING_SOURCE_NOT_CONFIGURED"
    SOURCE_UNAVAILABLE = "FUNDING_SOURCE_UNAVAILABLE"


class PrelaunchWindow(StrEnum):
    """A lookback ending at the creation block, named by its ISO 8601 duration."""

    PT1H = "PT1H"
    PT6H = "PT6H"
    PT24H = "PT24H"

    @property
    def lookback(self) -> timedelta:
        return LOOKBACKS[self]


LOOKBACKS = {
    PrelaunchWindow.PT1H: timedelta(hours=1),
    PrelaunchWindow.PT6H: timedelta(hours=6),
    PrelaunchWindow.PT24H: timedelta(hours=24),
}
PRELAUNCH_WINDOWS = (PrelaunchWindow.PT1H, PrelaunchWindow.PT6H, PrelaunchWindow.PT24H)
# The one read goes back this far, and no further.
LONGEST_LOOKBACK = max(window.lookback for window in PRELAUNCH_WINDOWS)


class PrelaunchGap(StrEnum):
    ORIGIN_UNAVAILABLE = "PRELAUNCH_ORIGIN_UNAVAILABLE"
    # The origin names a creation block after the pinned block.
    WINDOW_UNAVAILABLE = "PRELAUNCH_WINDOW_UNAVAILABLE"
    # The creation block's timestamp could not be read from the chain.
    CREATION_TIME_UNAVAILABLE = "PRELAUNCH_CREATION_TIME_UNAVAILABLE"
    SOURCE_UNAVAILABLE = "PRELAUNCH_SOURCE_UNAVAILABLE"


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("A timestamp must carry its timezone")
    return value.astimezone(UTC)


class HolderOverlapBasis(StrEnum):
    # A V4 token: the reconciled economic holders (pool control), never raw rows.
    ECONOMIC = "ECONOMIC"
    # Any other market: the normalized raw holder distribution.
    RAW = "RAW"
    # The basis the token requires was not established; no overlap is stated.
    UNKNOWN = "UNKNOWN"


class FundingTransaction(Immutable):
    """One normal transaction of the root address, as the source reported it."""

    tx_hash: Hash32
    block_number: int = Field(ge=0)
    sender: EvmAddress
    # Absent for a contract creation.
    recipient: EvmAddress | None = None
    native_value_raw: int = Field(ge=0)
    succeeded: bool
    # The provider's timestamp of the transaction's block, in UTC. Absent only
    # where a caller built the row without one; a V2 read always carries it.
    observed_at: AwareDatetime | None = None

    @field_validator("observed_at")
    @classmethod
    def in_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else utc(value)


class FundingSourceResult(Immutable):
    """A funding source's answer for one address and one block window."""

    status: Availability = Availability.UNKNOWN
    failure: AtlasSourceFailure | None = None
    source: Identifier
    chain: Identifier | None = None
    address: EvmAddress | None = None
    from_block: int | None = Field(default=None, ge=0)
    to_block: int | None = Field(default=None, ge=0)
    coverage: FundingCoverage | None = None
    # The launch window ``[from_block, to_block]``: V1, unchanged.
    transactions: tuple[FundingTransaction, ...] = Field(default=(), max_length=2_000)
    requests_made: int = Field(default=0, ge=0)
    # V2, from the same read: the rows before ``from_block`` no older than
    # ``history_until``, when the caller asked for history at all.
    history_until: AwareDatetime | None = None
    # A defect in the history's time data: the history is unusable, V1 is not.
    history_failure: AtlasSourceFailure | None = None
    prelaunch_transactions: tuple[FundingTransaction, ...] = Field(default=(), max_length=2_000)
    # Whether the provider's list ended, and the oldest valid row it reached.
    history_ended: bool = False
    oldest_observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None or self.coverage is None or self.address is None:
                raise ValueError("An available funding read names its address and coverage")
        elif self.transactions or self.prelaunch_transactions or self.coverage is not None:
            raise ValueError("An unavailable funding read carries no transactions")
        if self.history_failure is not None and (
            self.prelaunch_transactions or self.history_ended or self.oldest_observed_at
        ):
            raise ValueError("A failed history carries no history")
        return self


class FundingEdge(Immutable):
    """One counted edge: a successful, non-zero native transfer creator -> recipient."""

    funder: EvmAddress
    recipient: EvmAddress
    tx_hash: Hash32
    block_number: int = Field(ge=0)
    native_value_raw: int = Field(gt=0)


class PrelaunchWindowFacts(Immutable):
    """One lookback window ``[creation - lookback, creation block)``.

    ``coverage`` is this window's own: a short window can be exact while a
    longer one, cut by the page budget, is only a lower bound.
    """

    window: PrelaunchWindow
    lookback_seconds: int = Field(gt=0)
    cutoff_at: AwareDatetime
    coverage: FundingCoverage
    funding_tx_count: int = Field(ge=0)
    unique_funded_address_count: int = Field(ge=0)
    total_native_funding_raw: int = Field(ge=0)
    first_funding_block: int | None = Field(default=None, ge=0)
    last_funding_block: int | None = Field(default=None, ge=0)
    first_funding_at: AwareDatetime | None = None
    last_funding_at: AwareDatetime | None = None
    # sha256 over every counted edge of this window, in canonical order.
    edges_digest: Identifier
    unique_funding_value_count: int = Field(ge=0)
    # Edges beyond the first one to each recipient.
    repeated_funding_tx_count: int = Field(ge=0)
    max_funding_txs_per_recipient: int = Field(ge=0)
    # The exact native value paid to the most distinct recipients; ties go to
    # the smallest value. Recipients, never transfers, are what is counted.
    largest_identical_value_recipient_cluster_count: int = Field(ge=0)
    largest_identical_value_raw: int | None = Field(default=None, gt=0)
    largest_identical_value_recipient_fraction: Ratio | None = None
    max_unique_recipients_in_rolling_10m: int = Field(ge=0)

    @property
    def exact(self) -> bool:
        return self.coverage == FundingCoverage.COMPLETE


class PrelaunchFundingFacts(Immutable):
    """V2: the origin creator's direct native transfers before the creation block.

    The same edge definition as V1, over a different window, from the same
    provider read. Shadow only -- no policy reads it. Availability says the
    measurement was made; ``origin_verification`` says how far its root is
    trusted, and an unverified root is never treated as a confirmed one.
    """

    scope: Literal["DIRECT_NATIVE_FROM_ORIGIN_CREATOR_PRELAUNCH"] = (
        "DIRECT_NATIVE_FROM_ORIGIN_CREATOR_PRELAUNCH"
    )
    version: Literal[2] = 2
    status: Availability = Availability.UNKNOWN
    gap: PrelaunchGap | None = None
    failure: AtlasSourceFailure | None = None
    source: Identifier
    root_address: EvmAddress | None = None
    origin_source: Identifier | None = None
    origin_verification: Identifier | None = None
    creation_block: int | None = Field(default=None, ge=0)
    creation_timestamp: AwareDatetime | None = None
    creation_time_source: Identifier | None = None
    history_ended: bool | None = None
    oldest_observed_at: AwareDatetime | None = None
    # Rows before the creation block that the read kept (within the longest window).
    transactions_read: int = Field(default=0, ge=0)
    windows: tuple[PrelaunchWindowFacts, ...] = Field(default=(), max_length=3)
    # The first edges of the widest window only, never one sample per window.
    sample_edges: tuple[FundingEdge, ...] = Field(default=(), max_length=MAX_DURABLE_EDGES)
    # PT24H recipients among the observed holders, on the same basis as V1.
    holder_basis: HolderOverlapBasis = HolderOverlapBasis.UNKNOWN
    observed_holder_count: int | None = Field(default=None, ge=0)
    creator_funded_observed_holder_count: int | None = Field(default=None, ge=0)
    creator_funded_observed_holder_fraction: Ratio | None = None

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if (
                self.gap is not None
                or self.root_address is None
                or self.creation_timestamp is None
                or self.history_ended is None
                or tuple(item.window for item in self.windows) != PRELAUNCH_WINDOWS
            ):
                raise ValueError("An available prelaunch measurement carries every window")
        elif self.gap is None or self.windows or self.sample_edges:
            raise ValueError("An unavailable prelaunch measurement names its gap only")
        overlap = (
            self.observed_holder_count,
            self.creator_funded_observed_holder_count,
            self.creator_funded_observed_holder_fraction,
        )
        if self.holder_basis == HolderOverlapBasis.UNKNOWN and any(
            value is not None for value in overlap
        ):
            raise ValueError("An unknown holder basis states no overlap")
        return self

    def window(self, name: PrelaunchWindow) -> PrelaunchWindowFacts | None:
        return next((item for item in self.windows if item.window == name), None)


class FundingGraphFacts(Immutable):
    measurement: Literal["CREATOR_FUNDING_GRAPH"] = "CREATOR_FUNDING_GRAPH"
    scope: Literal["DIRECT_NATIVE_FROM_ORIGIN_CREATOR"] = "DIRECT_NATIVE_FROM_ORIGIN_CREATOR"
    status: Availability = Availability.UNKNOWN
    gap: FundingGap | None = None
    failure: AtlasSourceFailure | None = None
    source: Identifier
    # The root and where it comes from: the origin source, nothing else.
    root_address: EvmAddress | None = None
    origin_source: Identifier | None = None
    origin_verification: Identifier | None = None
    factory_address: EvmAddress | None = None
    creation_block: int | None = Field(default=None, ge=0)
    snapshot_block: int | None = Field(default=None, ge=0)
    coverage: FundingCoverage | None = None
    requests_made: int = Field(default=0, ge=0)
    transactions_read: int = Field(default=0, ge=0)
    direct_funding_tx_count: int | None = Field(default=None, ge=0)
    unique_direct_funded_address_count: int | None = Field(default=None, ge=0)
    total_direct_native_funding_raw: int | None = Field(default=None, ge=0)
    first_funding_block: int | None = Field(default=None, ge=0)
    last_funding_block: int | None = Field(default=None, ge=0)
    # sha256 over every counted edge in canonical order; the sample below is
    # only the first few of them.
    edges_digest: Identifier | None = None
    sample_edges: tuple[FundingEdge, ...] = Field(default=(), max_length=MAX_DURABLE_EDGES)
    # The overlap with observed holders. Fractions are within the observed
    # holder basis -- a bounded prefix, not the whole holder universe -- except
    # the supply fraction, which is over the on-chain total supply.
    holder_basis: HolderOverlapBasis = HolderOverlapBasis.UNKNOWN
    observed_holder_count: int | None = Field(default=None, ge=0)
    creator_funded_observed_holder_count: int | None = Field(default=None, ge=0)
    creator_funded_observed_holder_fraction: Ratio | None = None
    creator_funded_observed_supply_fraction: Ratio | None = None
    creator_funded_observed_top10_count: int | None = Field(default=None, ge=0)
    creator_funded_observed_holders: tuple[EvmAddress, ...] = Field(default=(), max_length=50)
    # V2. Absent where it was not measured; V1 fields above never carry it.
    prelaunch: PrelaunchFundingFacts | None = None

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        counts = (
            self.direct_funding_tx_count,
            self.unique_direct_funded_address_count,
            self.total_direct_native_funding_raw,
        )
        if self.status == Availability.AVAILABLE:
            if self.gap is not None or self.coverage is None or self.root_address is None:
                raise ValueError("An available funding graph names its root and coverage")
            if any(value is None for value in counts) or self.edges_digest is None:
                raise ValueError("An available funding graph carries its counts")
        else:
            if self.gap is None:
                raise ValueError("An unavailable funding graph must say why")
            if any(value is not None for value in counts) or self.sample_edges:
                raise ValueError("An unavailable funding graph carries no counts")
        overlap = (
            self.observed_holder_count,
            self.creator_funded_observed_holder_count,
            self.creator_funded_observed_holder_fraction,
            self.creator_funded_observed_top10_count,
        )
        if self.holder_basis == HolderOverlapBasis.UNKNOWN and any(
            value is not None for value in overlap
        ):
            raise ValueError("An unknown holder basis states no overlap")
        return self

    @property
    def exact(self) -> bool:
        """Whether the counts are the whole truth rather than what was seen."""
        return self.coverage == FundingCoverage.COMPLETE
