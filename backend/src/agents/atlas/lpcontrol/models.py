"""V2/V3 LP-control evidence: who can withdraw a 20-byte pool's liquidity (issue #102).

A V2-style pair *is* its LP token: liquidity is withdrawn by burning LP tokens,
so the question is who holds the complete `totalSupply()` at the pinned block.
A V3-style pool's liquidity lives in positions keyed by (owner, tickLower,
tickUpper); most are owned by a NonfungiblePositionManager (NPM), whose ERC-721
owners are the ones who can withdraw, and a few are direct pool positions owned
by whoever minted them.

Each holder -- an LP-token balance holder or a position-NFT owner or a direct
position owner -- is resolved into one `HolderState`, and every state maps to
exactly one withdrawal class, mirroring the V4 census after issue #97:

* RESTRICTED -- nobody can withdraw: a burn address, or a contract a verified
  locker adapter proved has no path for the principal ever to leave;
* WITHDRAWABLE -- an established controller can withdraw now: an account, a
  released lock's controller, or LP tokens left in the pair (anyone can burn);
* UNRESOLVED -- anything else: a timelock not yet released, an unrecognised
  contract, or a holder whose custody was not read.

These models record facts. Whether they suffice is `decision`'s question, and
it re-checks every reconciliation rather than trusting a flag.
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from src.agents.atlas.primitives import EvmAddress, Hash32, Identifier, Immutable
from src.agents.atlas.v4.control import CustodyRefusal

ZERO_ADDRESS = "0x" + "0" * 40
# The conventional dead address; nobody holds its key.
BURN_DEAD = "0x000000000000000000000000000000000000dead"
BURN_ADDRESSES = frozenset({ZERO_ADDRESS, BURN_DEAD})


class Withdrawal(StrEnum):
    RESTRICTED = "RESTRICTED"
    WITHDRAWABLE = "WITHDRAWABLE"
    UNRESOLVED = "UNRESOLVED"


class HolderState(StrEnum):
    # A burn address holds it: nobody can ever move it. Includes the
    # MINIMUM_LIQUIDITY a V2 pair mints to the zero address.
    BURNED = "BURNED"
    # An account a private key controls; it can withdraw at will.
    DIRECT_CONTROL = "DIRECT_CONTROL"
    # A verified locker with no release path for the principal.
    PERMANENTLY_LOCKED = "PERMANENTLY_LOCKED"
    # A verified locker that releases to a fixed controller at a fixed block,
    # not yet reached at the pinned block.
    TIMELOCKED = "TIMELOCKED"
    # The same at or after that block: its controller can withdraw now.
    RELEASABLE = "RELEASABLE"
    # LP tokens held by the pair itself: anyone can call `burn()` and take them.
    POOL_REDEEMABLE = "POOL_REDEEMABLE"
    # A contract no adapter verified, or a recognised one whose binding failed.
    UNKNOWN_CONTRACT_CUSTODY = "UNKNOWN_CONTRACT_CUSTODY"


WITHDRAWAL_OF_STATE: dict[HolderState, Withdrawal] = {
    HolderState.BURNED: Withdrawal.RESTRICTED,
    HolderState.PERMANENTLY_LOCKED: Withdrawal.RESTRICTED,
    HolderState.DIRECT_CONTROL: Withdrawal.WITHDRAWABLE,
    HolderState.RELEASABLE: Withdrawal.WITHDRAWABLE,
    HolderState.POOL_REDEEMABLE: Withdrawal.WITHDRAWABLE,
    HolderState.TIMELOCKED: Withdrawal.UNRESOLVED,
    HolderState.UNKNOWN_CONTRACT_CUSTODY: Withdrawal.UNRESOLVED,
}

CONTROLLED_STATES = frozenset({HolderState.DIRECT_CONTROL, HolderState.RELEASABLE})
LOCKER_STATES = frozenset(
    {HolderState.PERMANENTLY_LOCKED, HolderState.TIMELOCKED, HolderState.RELEASABLE}
)


class LpProofKind(StrEnum):
    BURN_ADDRESS = "BURN_ADDRESS"
    EXTERNALLY_OWNED_ACCOUNT = "EXTERNALLY_OWNED_ACCOUNT"
    VERIFIED_LOCKER_CODE = "VERIFIED_LOCKER_CODE"
    # Protocol semantics: LP tokens in the pair are redeemable by anyone.
    POOL_SEMANTICS = "POOL_SEMANTICS"
    NONE = "NONE"


class CustodyFacts(Immutable):
    """What one holder means for who can withdraw what it holds."""

    holder: EvmAddress
    state: HolderState
    # Who can withdraw: the account itself or a released lock's controller.
    controller: EvmAddress | None = None
    unlock_block: int | None = Field(default=None, ge=0)
    proof_kind: LpProofKind
    proof_contract: Identifier | None = None
    proof_version: Identifier | None = None
    holder_code_hash: Hash32 | None = None
    refusal: CustodyRefusal | None = None
    # Whether the controller is the receipt-verified creator; None unless both
    # are known.
    controller_is_creator: bool | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        state = self.state
        if (self.controller is not None) != (state in CONTROLLED_STATES):
            raise ValueError("A controller is named exactly for controlled holders")
        if self.controller is None and self.controller_is_creator is not None:
            raise ValueError("Only a controller can be the creator")
        if state == HolderState.BURNED and not (
            self.holder in BURN_ADDRESSES and self.proof_kind == LpProofKind.BURN_ADDRESS
        ):
            raise ValueError("Only a burn address is burned")
        if state == HolderState.DIRECT_CONTROL and not (
            self.controller == self.holder
            and self.proof_kind == LpProofKind.EXTERNALLY_OWNED_ACCOUNT
        ):
            raise ValueError("Direct control is an account's own holding")
        if state in LOCKER_STATES and not (
            self.proof_kind == LpProofKind.VERIFIED_LOCKER_CODE
            and self.proof_contract is not None
            and self.proof_version is not None
            and self.holder_code_hash is not None
        ):
            raise ValueError("A lock or a release rests on verified locker code")
        if (self.unlock_block is not None) != (
            state in (HolderState.TIMELOCKED, HolderState.RELEASABLE)
        ):
            raise ValueError("Only a timelocked or releasable holder has an unlock block")
        if state == HolderState.POOL_REDEEMABLE and self.proof_kind != LpProofKind.POOL_SEMANTICS:
            raise ValueError("Pool redemption rests on the pool's own semantics")
        if (state == HolderState.UNKNOWN_CONTRACT_CUSTODY) != (self.refusal is not None):
            raise ValueError("Exactly unknown custody says why it stayed unknown")
        return self

    @property
    def withdrawal(self) -> Withdrawal:
        return WITHDRAWAL_OF_STATE[self.state]


# ----------------------------------------------------------------------- V2


class V2PairBinding(Immutable):
    """The pair's binding to a registered factory, as read at the pinned block.

    A field left None was not read; a read value that disagrees is kept as read.
    """

    factory: EvmAddress
    pair: EvmAddress
    token0: EvmAddress | None = None
    token1: EvmAddress | None = None
    # `factory.getPair(token0, token1)`.
    factory_get_pair: EvmAddress | None = None
    # `pair.factory()`.
    pair_factory: EvmAddress | None = None
    # keccak256 of the pair's runtime code.
    pair_code_hash: Hash32 | None = None
    # Whether that hash is the registered pair template's.
    code_matches: bool = False


class V2HolderBalance(Immutable):
    balance_raw: int = Field(ge=0)
    custody: CustodyFacts


class V2LpEvidence(Immutable):
    """A census of one V2-style pair's LP supply at one pinned block."""

    chain: Identifier
    chain_id: int = Field(gt=0)
    block: int = Field(ge=0)
    block_timestamp: int | None = Field(default=None, ge=0)
    binding: V2PairBinding
    pair_created_block: int | None = Field(default=None, ge=0)
    # The Transfer scan covered creation to the pinned block without a gap.
    scan_complete: bool = False
    total_supply_raw: int | None = Field(default=None, ge=0)
    # Every candidate holder with its `balanceOf` at the pinned block.
    holders: tuple[V2HolderBalance, ...] = ()
    # Why the census stopped short, when it did.
    gap: Identifier | None = None

    @model_validator(mode="after")
    def unique_holders(self) -> Self:
        seen = [item.custody.holder for item in self.holders]
        if len(seen) != len(set(seen)):
            raise ValueError("Every holder is listed once")
        return self

    @property
    def reconciled(self) -> bool:
        return self.total_supply_raw is not None and self.total_supply_raw == sum(
            item.balance_raw for item in self.holders
        )


# ----------------------------------------------------------------------- V3


class V3PoolBinding(Immutable):
    factory: EvmAddress
    pool: EvmAddress
    position_manager: EvmAddress
    token0: EvmAddress | None = None
    token1: EvmAddress | None = None
    fee: int | None = Field(default=None, ge=0)
    # `factory.getPool(token0, token1, fee)`.
    factory_get_pool: EvmAddress | None = None
    # `pool.factory()`.
    pool_factory: EvmAddress | None = None
    # The pool's runtime code matched the registered template, its immutable
    # factory/token0/token1/fee equal to the values above.
    pool_code_matches: bool = False
    # `positionManager.factory()`.
    position_manager_factory: EvmAddress | None = None


class V3PositionKind(StrEnum):
    # Owned in the pool by the registered NPM; withdrawn by the NFT's owner.
    POSITION_MANAGER = "POSITION_MANAGER"
    # Owned in the pool by whoever minted it directly.
    DIRECT = "DIRECT"


class V3PositionEvidence(Immutable):
    kind: V3PositionKind
    # The pool-level position owner: the NPM, or the direct minter.
    owner: EvmAddress
    tick_lower: int = Field(ge=-887272, le=887272)
    tick_upper: int = Field(ge=-887272, le=887272)
    liquidity: int = Field(ge=0, lt=1 << 128)
    # The NPM token id, exactly for a position-manager position.
    token_id: int | None = Field(default=None, ge=0)
    # Who can withdraw it: the NFT owner's custody, or the direct owner's.
    # None when it was not read -- always unresolved.
    custody: CustodyFacts | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.tick_lower >= self.tick_upper:
            raise ValueError("A position's lower tick lies below its upper tick")
        if (self.token_id is not None) != (self.kind == V3PositionKind.POSITION_MANAGER):
            raise ValueError("A token id names exactly a position-manager position")
        if (
            self.kind == V3PositionKind.DIRECT
            and self.custody is not None
            and self.custody.holder != self.owner
        ):
            raise ValueError("A direct position's custody is its own owner's")
        return self

    @property
    def withdrawal(self) -> Withdrawal:
        return Withdrawal.UNRESOLVED if self.custody is None else self.custody.withdrawal


class V3LpEvidence(Immutable):
    """A census of one V3-style pool's positions at one pinned block."""

    chain: Identifier
    chain_id: int = Field(gt=0)
    block: int = Field(ge=0)
    block_timestamp: int | None = Field(default=None, ge=0)
    binding: V3PoolBinding
    pool_created_block: int | None = Field(default=None, ge=0)
    # The pool's Mint scan covered creation to the pinned block without a gap.
    scan_complete: bool = False
    # For every NPM-owned position key, the NPM tokens' liquidity summed
    # exactly to the pool's own `positions(key).liquidity`.
    position_manager_reconciled: bool = False
    current_tick: int | None = None
    # `pool.liquidity()` at the pinned block.
    active_liquidity: int | None = Field(default=None, ge=0)
    # The positions in range at `current_tick` summed exactly to it.
    active_liquidity_reconciled: bool = False
    positions: tuple[V3PositionEvidence, ...] = ()
    gap: Identifier | None = None

    @model_validator(mode="after")
    def unique_positions(self) -> Self:
        keys = [
            (item.owner, item.tick_lower, item.tick_upper, item.token_id) for item in self.positions
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Every position is listed once")
        return self

    def active_sum(self) -> int | None:
        if self.current_tick is None:
            return None
        return sum(
            item.liquidity
            for item in self.positions
            if item.tick_lower <= self.current_tick < item.tick_upper
        )
