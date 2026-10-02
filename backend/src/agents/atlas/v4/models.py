"""POOL_CONTROL: who economically controls token supply held in Uniswap V4.

Raw holder facts say which addresses hold a token. In V4 every pool's reserves
sit in one PoolManager contract, so supply that a single party can withdraw at
will — its liquidity position — shows up as one technical holder, or not at
all when a provider filters that contract out. This fact puts that supply back
with the party that controls it, without touching the raw holder record.

Nothing here is inferred from names, labels, symbols or explorer metadata.
Every pool, hook, position and amount is read from the chain at the pinned
block, and anything that could not be established is recorded as unknown with
a stable reason rather than left out.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from src.agents.atlas.primitives import (
    AtlasSourceFailure,
    EvmAddress,
    Hash32,
    Identifier,
    Immutable,
    Ratio,
)
from src.markets.models import Availability

# Bounds on what one snapshot may carry. Exceeding one is never a silent cut:
# the census reports itself incomplete instead.
MAX_POOLS = 16
MAX_POSITIONS = 64
# The pseudo-holder an unexplained PoolManager remainder is ranked as.
UNATTRIBUTED_POOL_BALANCE = "UNATTRIBUTED_POOL_BALANCE"


class PoolControlGap(StrEnum):
    """Why the pool-control fact is not usable. Detailed, for the audit record."""

    DEPLOYMENT_NOT_CONFIGURED = "V4_DEPLOYMENT_NOT_CONFIGURED"
    DEPLOYMENT_UNVERIFIED = "V4_DEPLOYMENT_UNVERIFIED"
    CREATION_BLOCK_UNKNOWN = "V4_CREATION_BLOCK_UNKNOWN"
    CENSUS_UNAVAILABLE = "V4_POOL_CENSUS_UNAVAILABLE"
    CENSUS_BOUNDS_EXCEEDED = "V4_POOL_CENSUS_BOUNDS_EXCEEDED"
    POOL_KEY_MISMATCH = "V4_POOL_KEY_MISMATCH"
    POSITION_FACTS_INCOMPLETE = "V4_POSITION_FACTS_INCOMPLETE"
    POSITION_OWNER_UNKNOWN = "V4_POSITION_OWNER_UNKNOWN"
    POOL_BALANCE_UNATTRIBUTED = "V4_POOL_BALANCE_UNATTRIBUTED"
    HOLDER_BASIS_UNAVAILABLE = "V4_HOLDER_BASIS_UNAVAILABLE"
    # The market's own V4 pool is not among the pools the census found.
    MARKET_POOL_NOT_FOUND = "V4_MARKET_POOL_NOT_FOUND"


class HookOwnerStatus(StrEnum):
    """What a hook's `owner()` established. A failed read is never "no owner"."""

    OWNER_READ = "OWNER_READ"
    OWNER_UNKNOWN = "OWNER_UNKNOWN"


class PositionKind(StrEnum):
    # Held by a verified PositionManager on behalf of an ERC-721 owner.
    POSITION_MANAGER = "POSITION_MANAGER"
    # Held in the PoolManager under some other sender's owner key.
    DIRECT = "DIRECT"


class PositionOwnerStatus(StrEnum):
    ATTRIBUTED = "ATTRIBUTED"
    OWNER_UNKNOWN = "OWNER_UNKNOWN"


class ConcentrationBasis(StrEnum):
    # Every unit of pool-held supply was assigned to its owner.
    EXACT = "EXACT"
    # Some quantity could only be bounded, and was ranked at its bound, so the
    # figure can overstate concentration but never understate it.
    UPPER_BOUND = "UPPER_BOUND"


class V4HookFacts(Immutable):
    address: EvmAddress
    code_present: bool | None = None
    # From the address bits, which is exactly what the PoolManager acts on.
    permissions: tuple[str, ...] = Field(default=(), max_length=14)
    owner: EvmAddress | None = None
    owner_status: HookOwnerStatus = HookOwnerStatus.OWNER_UNKNOWN
    # None unless both the owner and a receipt-verified creator are known.
    owner_is_creator: bool | None = None

    @model_validator(mode="after")
    def owner_matches_status(self) -> Self:
        if (self.owner is not None) != (self.owner_status == HookOwnerStatus.OWNER_READ):
            raise ValueError("A hook owner is present exactly when it was read")
        return self

    @property
    def before_swap(self) -> bool:
        return "BEFORE_SWAP" in self.permissions


class V4PoolFacts(Immutable):
    pool_manager: EvmAddress
    pool_id: Hash32
    currency0: EvmAddress
    currency1: EvmAddress
    fee: int = Field(ge=0, lt=1 << 24)
    tick_spacing: int
    hook: EvmAddress
    created_block: int = Field(ge=0)
    # The creation block's own chain time, read from that block.
    created_at: AwareDatetime | None = None
    initialized: bool
    sqrt_price_x96: int | None = Field(default=None, ge=0)
    tick: int | None = None
    hook_facts: V4HookFacts | None = None

    @model_validator(mode="after")
    def ordered_currencies(self) -> Self:
        if int(self.currency0, 16) >= int(self.currency1, 16):
            raise ValueError("A PoolKey orders currency0 strictly below currency1")
        if (self.hook_facts is None) != (int(self.hook, 16) == 0):
            raise ValueError("Hook facts are present exactly when the pool has a hook")
        return self


class V4PositionFacts(Immutable):
    kind: PositionKind
    pool_id: Hash32
    # The PoolManager owner key: the PositionManager, or the direct sender.
    owner_key: EvmAddress
    salt: Hash32
    position_manager: EvmAddress | None = None
    token_id: int | None = Field(default=None, ge=0)
    tick_lower: int
    tick_upper: int
    liquidity: int = Field(gt=0)
    owner: EvmAddress | None = None
    owner_status: PositionOwnerStatus
    controlled_token_raw: int = Field(ge=0)
    owner_is_creator: bool | None = None

    @model_validator(mode="after")
    def owner_matches_status(self) -> Self:
        if (self.owner is not None) != (self.owner_status == PositionOwnerStatus.ATTRIBUTED):
            raise ValueError("A position owner is present exactly when it was attributed")
        if (self.kind == PositionKind.POSITION_MANAGER) != (
            self.position_manager is not None and self.token_id is not None
        ):
            raise ValueError("Only a PositionManager position carries a manager and token id")
        return self


class V4Census(Immutable):
    """What the chain said, before any economic interpretation."""

    status: Availability = Availability.UNKNOWN
    gap: PoolControlGap | None = None
    failure: AtlasSourceFailure | None = None
    source: Identifier
    chain_id: int | None = Field(default=None, gt=0)
    pool_manager: EvmAddress | None = None
    position_managers: tuple[EvmAddress, ...] = Field(default=(), max_length=4)
    scan_from_block: int | None = Field(default=None, ge=0)
    scan_to_block: int | None = Field(default=None, ge=0)
    requests_made: int = Field(default=0, ge=0)
    pools: tuple[V4PoolFacts, ...] = Field(default=(), max_length=MAX_POOLS)
    positions: tuple[V4PositionFacts, ...] = Field(default=(), max_length=MAX_POSITIONS)
    pool_manager_balance_raw: int | None = Field(default=None, ge=0)
    # How many pools were proven, even when the census later failed. A census
    # that found V4 pools and then broke off must not make them look absent.
    pools_found: int = Field(default=0, ge=0)

    @property
    def has_pools(self) -> bool:
        return bool(self.pools) or self.pools_found > 0

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.gap is not None or self.failure is not None:
                raise ValueError("An available census cannot carry a gap")
            if self.pool_manager is None or self.pool_manager_balance_raw is None:
                raise ValueError("An available census names its PoolManager and its balance")
        elif self.gap is None:
            raise ValueError("An unavailable census must say why")
        return self


class EconomicHolder(Immutable):
    # An address, or the UNATTRIBUTED_POOL_BALANCE pseudo-holder.
    holder: Identifier
    balance_raw: int = Field(ge=0)
    share: Ratio
    is_burn_address: bool = False


class PoolControlFacts(Immutable):
    """The census plus the economic distribution derived from it.

    ``required`` says whether this token's holder concentration must be read
    through this fact: its market is a V4 pool, or the census found V4 pools.
    When required and not AVAILABLE, the economic figures are unknown and the
    raw holder figures may not stand in for them.
    """

    measurement: Literal["V4_POOL_CONTROL"] = "V4_POOL_CONTROL"
    status: Availability = Availability.UNKNOWN
    gap: PoolControlGap | None = None
    required: bool
    census: V4Census
    total_supply_raw: int | None = Field(default=None, ge=0)
    # Supply the PoolManager held, split into what was traced to a position
    # owner and what was not. attributed + unattributed == pool held.
    attributed_raw: int | None = Field(default=None, ge=0)
    unattributed_raw: int | None = Field(default=None, ge=0)
    creator_controlled_raw: int | None = Field(default=None, ge=0)
    pool_held_supply_fraction: Ratio | None = None
    attributable_pool_supply_fraction: Ratio | None = None
    creator_controlled_pool_supply_fraction: Ratio | None = None
    unattributed_pool_supply_fraction: Ratio | None = None
    basis: ConcentrationBasis | None = None
    economic_top1_share: Ratio | None = None
    economic_top5_share: Ratio | None = None
    economic_top10_share: Ratio | None = None
    economic_top_holders: tuple[EconomicHolder, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def availability_matches_content(self) -> Self:
        if self.status == Availability.AVAILABLE:
            if self.gap is not None or self.economic_top10_share is None or self.basis is None:
                raise ValueError("Available pool control carries its economic figures")
            if self.census.status != Availability.AVAILABLE:
                raise ValueError("Available pool control requires an available census")
        else:
            if self.gap is None:
                raise ValueError("Unavailable pool control must say why")
            if self.economic_top10_share is not None or self.economic_top_holders:
                raise ValueError("Unavailable pool control carries no economic figures")
        return self

    @property
    def pools_total(self) -> int:
        return len(self.census.pools)

    @property
    def pools_with_hooks(self) -> int:
        return sum(1 for pool in self.census.pools if pool.hook_facts is not None)

    @property
    def creator_controlled_hooks(self) -> int:
        return sum(
            1
            for pool in self.census.pools
            if pool.hook_facts is not None and pool.hook_facts.owner_is_creator is True
        )

    @property
    def positions_total(self) -> int:
        return len(self.census.positions)

    @property
    def positions_attributed(self) -> int:
        return sum(
            1
            for item in self.census.positions
            if item.owner_status == PositionOwnerStatus.ATTRIBUTED
        )

    @property
    def positions_unattributed(self) -> int:
        return self.positions_total - self.positions_attributed
