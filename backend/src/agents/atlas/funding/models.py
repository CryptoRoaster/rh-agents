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
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

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
    transactions: tuple[FundingTransaction, ...] = Field(default=(), max_length=2_000)
    requests_made: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.failure is not None or self.coverage is None or self.address is None:
                raise ValueError("An available funding read names its address and coverage")
        elif self.transactions or self.coverage is not None:
            raise ValueError("An unavailable funding read carries no transactions")
        return self


class FundingEdge(Immutable):
    """One counted edge: a successful, non-zero native transfer creator -> recipient."""

    funder: EvmAddress
    recipient: EvmAddress
    tx_hash: Hash32
    block_number: int = Field(ge=0)
    native_value_raw: int = Field(gt=0)


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
