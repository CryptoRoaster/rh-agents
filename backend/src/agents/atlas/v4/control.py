"""POSITION_CONTROL: who can actually move the principal of a V4 position NFT.

The census reads a PositionManager position's ERC-721 owner with `ownerOf`.
That is a fact about custody, not about control: a contract that holds the NFT
may hand it to anyone, or to nobody, depending on what its code allows. So the
raw owner is kept as it is and a second, separate fact says what it means:

* ``DIRECT_CONTROL`` -- the owner is an account a private key controls; it can
  withdraw or transfer the position at will.
* ``PERMANENTLY_LOCKED`` -- the owner is a custody contract whose deployed code
  was matched to a supported, pinned version that has no path by which the
  principal ever leaves: no NFT transfer, no approval, no liquidity decrease, no
  arbitrary call, no upgrade.
* ``TIMELOCKED`` -- the owner is a verified custody contract that releases the
  position to a fixed controller once a fixed block is reached, and that block
  has not been reached at the pinned block.
* ``RELEASABLE`` -- the same, at or after that block: the controller can take
  the position now, so it is that controller's supply.
* ``UNKNOWN_CONTRACT_CUSTODY`` -- the owner is a contract no supported adapter
  verified. Its code may or may not let someone withdraw, so nothing is assumed.

Nothing here is inferred from a name, symbol, label, website, the age of a
holding or the absence of transfers. A lock is a property of verified code and
verified bindings, or it is not established.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from src.agents.atlas.primitives import EvmAddress, Hash32, Identifier, Immutable


class PositionControlState(StrEnum):
    DIRECT_CONTROL = "DIRECT_CONTROL"
    PERMANENTLY_LOCKED = "PERMANENTLY_LOCKED"
    TIMELOCKED = "TIMELOCKED"
    RELEASABLE = "RELEASABLE"
    UNKNOWN_CONTRACT_CUSTODY = "UNKNOWN_CONTRACT_CUSTODY"


# The states whose supply belongs to a named controller.
CONTROLLED_STATES = frozenset(
    {PositionControlState.DIRECT_CONTROL, PositionControlState.RELEASABLE}
)
# The states whose supply cannot be placed: nobody may count it as anyone's, and
# nobody may count it as locked.
UNRESOLVED_STATES = frozenset(
    {PositionControlState.TIMELOCKED, PositionControlState.UNKNOWN_CONTRACT_CUSTODY}
)


class PositionOwnerKind(StrEnum):
    # No code, or only an EIP-7702 delegation: a private key decides.
    EXTERNALLY_OWNED = "EXTERNALLY_OWNED"
    CONTRACT = "CONTRACT"


class ControlProofKind(StrEnum):
    # The owner has no contract code at the pinned block.
    EXTERNALLY_OWNED_ACCOUNT = "EXTERNALLY_OWNED_ACCOUNT"
    # A custody adapter matched the owner's code and bindings to a pinned
    # official deployment version.
    VERIFIED_CUSTODY_CODE = "VERIFIED_CUSTODY_CODE"
    # Nothing proved anything about the owner's code.
    NONE = "NONE"


class ControlCompleteness(StrEnum):
    # Every fact the state rests on was read and checked at the pinned block.
    VERIFIED = "VERIFIED"
    # The owner is a contract no adapter recognised. A fact, not a failure.
    UNRECOGNISED = "UNRECOGNISED"
    # An adapter recognised the code but a binding did not hold or a read
    # failed, so the claimed custody could not be confirmed.
    REFUTED = "REFUTED"


class CustodyRefusal(StrEnum):
    """Why a contract owner stayed unknown. For the audit record only."""

    CODE_NOT_RECOGNISED = "CODE_NOT_RECOGNISED"
    CHAIN_NOT_SUPPORTED = "CHAIN_NOT_SUPPORTED"
    POSITION_MANAGER_MISMATCH = "POSITION_MANAGER_MISMATCH"
    DEPLOYMENT_MISMATCH = "DEPLOYMENT_MISMATCH"
    PROXY_INDIRECTION = "PROXY_INDIRECTION"
    # An immutable value is not what the contract's own semantics require,
    # such as an operator word that is not an address.
    IMMUTABLE_INVALID = "IMMUTABLE_INVALID"
    # The timelock is counted in a block clock that the pinned block number
    # does not measure, so whether it has passed cannot be said.
    BLOCK_CLOCK_UNSUPPORTED = "BLOCK_CLOCK_UNSUPPORTED"


class CustodyChainRefused(Exception):
    """A custody registry pins this chain name to another chain id. A hard stop."""


class PositionControlFacts(Immutable):
    """What the raw NFT owner of one PositionManager position means for control."""

    position_manager: EvmAddress
    token_id: int = Field(ge=0)
    # `ownerOf(tokenId)` at the pinned block, exactly as read.
    position_owner: EvmAddress
    owner_kind: PositionOwnerKind
    control_state: PositionControlState
    # Who can take the principal: the owner itself, or a verified release
    # controller. Absent wherever nobody can, or nobody is established.
    controller: EvmAddress | None = None
    # The block from which a timelocked position becomes releasable.
    unlock_block: int | None = Field(default=None, ge=0)
    # Never derived from a block number: a future block has no proven time.
    unlock_timestamp: None = None
    proof_kind: ControlProofKind
    # The adapter and the pinned contract version that matched, if any.
    proof_contract: Identifier | None = None
    proof_version: Identifier | None = None
    # keccak256 of the owner's runtime code at the pinned block, when read.
    owner_code_hash: Hash32 | None = None
    completeness: ControlCompleteness
    refusal: CustodyRefusal | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        state = self.control_state
        if (self.controller is not None) != (state in CONTROLLED_STATES):
            raise ValueError("A controller is named exactly for controlled positions")
        if state == PositionControlState.DIRECT_CONTROL and not (
            self.owner_kind == PositionOwnerKind.EXTERNALLY_OWNED
            and self.controller == self.position_owner
            and self.proof_kind == ControlProofKind.EXTERNALLY_OWNED_ACCOUNT
        ):
            raise ValueError("Direct control is an account's own position")
        if self.owner_kind == PositionOwnerKind.EXTERNALLY_OWNED and (
            state != PositionControlState.DIRECT_CONTROL
        ):
            raise ValueError("An account's position is always under its direct control")
        verified = state in (
            PositionControlState.PERMANENTLY_LOCKED,
            PositionControlState.TIMELOCKED,
            PositionControlState.RELEASABLE,
        )
        if verified and not (
            self.proof_kind == ControlProofKind.VERIFIED_CUSTODY_CODE
            and self.completeness == ControlCompleteness.VERIFIED
            and self.proof_contract is not None
            and self.proof_version is not None
            and self.owner_code_hash is not None
        ):
            raise ValueError("A lock or a release rests on verified custody code")
        if (self.unlock_block is not None) != (
            state in (PositionControlState.TIMELOCKED, PositionControlState.RELEASABLE)
        ):
            raise ValueError("Only a timelocked or releasable position has an unlock block")
        if state == PositionControlState.UNKNOWN_CONTRACT_CUSTODY:
            if self.completeness == ControlCompleteness.VERIFIED or self.refusal is None:
                raise ValueError("Unknown custody says why it stayed unknown")
        elif self.refusal is not None:
            raise ValueError("Only unknown custody carries a refusal")
        return self


# ----------------------------------------------------------------- adapters


class CustodyReadPort(Protocol):
    """The reads a custody adapter may make, all at the pinned block.

    Every call is charged to the census's own request budget and wall-clock
    bound; an adapter never reaches the chain any other way.
    """

    async def code(self, address: str) -> str: ...

    async def storage(self, address: str, slot: str) -> str: ...

    async def call(self, address: str, selector: str) -> str: ...


@dataclass(frozen=True)
class CustodyQuery:
    """One contract-owned position, as the census verified it."""

    chain: str
    chain_id: int
    block: int
    pool_manager: str
    position_manager: str
    token_id: int
    pool_id: str
    owner: str
    owner_code: str
    owner_code_hash: str


@dataclass(frozen=True)
class CustodyVerdict:
    """An adapter's answer about a contract it recognised.

    ``state`` is PERMANENTLY_LOCKED, TIMELOCKED or RELEASABLE when every binding
    held, and UNKNOWN_CONTRACT_CUSTODY with a refusal when the code matched but
    a binding did not -- a recognised look-alike is not a lock.
    """

    state: PositionControlState
    proof_contract: str
    proof_version: str
    controller: str | None = None
    unlock_block: int | None = None
    refusal: CustodyRefusal | None = None


class PositionCustodyAdapter(Protocol):
    """One verified custody protocol. Answers None for code it does not know."""

    @property
    def name(self) -> str: ...

    async def verify(
        self, reads: CustodyReadPort, query: CustodyQuery
    ) -> CustodyVerdict | None: ...
