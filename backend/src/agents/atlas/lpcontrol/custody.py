"""From one LP holder or position owner to its custody, through verified adapters only.

The V4 custody pattern (`agents/atlas/v4/custody/`), applied to V2 LP-token
balances and V3 position NFTs: a burn address is burned; an account is its own
controller; LP tokens in the pair itself are redeemable by anyone; a contract
is offered to each verified locker adapter in turn, the first that recognises
its exact code (a pinned `CodeTemplate`) and every binding decides, and a
contract no adapter recognises is unknown custody -- whatever it is called.

No locker adapter is shipped: none has been built from a pinned source and tied
to an on-chain deployment yet (UNCX, Team.Finance, PinkLock and alike are the
candidates; docs/lp-control-v2-v3.md). `DEFAULT_LOCKERS` is therefore empty and
every contract holder stays UNKNOWN_CONTRACT_CUSTODY.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.agents.atlas.lpcontrol.models import (
    BURN_ADDRESSES,
    BURN_DEAD,
    ZERO_ADDRESS,
    CustodyFacts,
    HolderState,
    LpProofKind,
)
from src.agents.atlas.v4.control import CustodyReadPort, CustodyRefusal
from src.agents.atlas.v4.custody.resolver import (
    BEACON_SLOT,
    IMPLEMENTATION_SLOT,
    MINIMAL_PROXY_PREFIX,
    is_externally_owned,
)


@dataclass(frozen=True)
class LockerQuery:
    """One contract holder, as the census read it at the pinned block."""

    chain: str
    chain_id: int
    block: int
    # The V2 pair whose LP tokens it holds, or the V3 position manager / pool.
    asset: str
    # The NPM token id it owns; None for an LP-token balance or direct position.
    token_id: int | None
    holder: str
    holder_code: str
    holder_code_hash: str


@dataclass(frozen=True)
class LockerVerdict:
    """An adapter's answer about a contract whose exact code it recognised.

    PERMANENTLY_LOCKED, TIMELOCKED or RELEASABLE when every binding held;
    UNKNOWN_CONTRACT_CUSTODY with a refusal when the code matched but a binding
    did not -- a recognised look-alike is not a lock.
    """

    state: HolderState
    proof_contract: str
    proof_version: str
    controller: str | None = None
    unlock_block: int | None = None
    refusal: CustodyRefusal | None = None


class LpLockerAdapter(Protocol):
    """One verified locker protocol. Answers None for code it does not know."""

    @property
    def name(self) -> str: ...

    async def verify(self, reads: CustodyReadPort, query: LockerQuery) -> LockerVerdict | None: ...


DEFAULT_LOCKERS: tuple[LpLockerAdapter, ...] = ()


async def _refusal(reads: CustodyReadPort, holder: str, code: str) -> CustodyRefusal:
    if code.startswith(MINIMAL_PROXY_PREFIX):
        return CustodyRefusal.PROXY_INDIRECTION
    for slot in (IMPLEMENTATION_SLOT, BEACON_SLOT):
        if int(await reads.storage(holder, slot), 16):
            return CustodyRefusal.PROXY_INDIRECTION
    return CustodyRefusal.CODE_NOT_RECOGNISED


def _is_creator(controller: str | None, creator: str | None) -> bool | None:
    if controller is None or creator is None:
        return None
    return controller == creator


async def classify_holder(
    reads: CustodyReadPort,
    query: LockerQuery,
    *,
    pool: str,
    creator: str | None,
    adapters: Sequence[LpLockerAdapter] = DEFAULT_LOCKERS,
) -> CustodyFacts:
    """What ``query.holder`` holding LP supply or a position means for withdrawal.

    ``creator`` is the receipt-verified creator, or None when it is unknown.
    """
    holder = query.holder
    if holder in BURN_ADDRESSES:
        return CustodyFacts(
            holder=holder, state=HolderState.BURNED, proof_kind=LpProofKind.BURN_ADDRESS
        )
    if holder == pool:
        return CustodyFacts(
            holder=holder,
            state=HolderState.POOL_REDEEMABLE,
            proof_kind=LpProofKind.POOL_SEMANTICS,
        )
    if is_externally_owned(query.holder_code):
        return CustodyFacts(
            holder=holder,
            state=HolderState.DIRECT_CONTROL,
            controller=holder,
            proof_kind=LpProofKind.EXTERNALLY_OWNED_ACCOUNT,
            controller_is_creator=_is_creator(holder, creator),
        )
    verdict: LockerVerdict | None = None
    for adapter in adapters:
        verdict = await adapter.verify(reads, query)
        if verdict is not None:
            break
    if verdict is None:
        return CustodyFacts(
            holder=holder,
            state=HolderState.UNKNOWN_CONTRACT_CUSTODY,
            proof_kind=LpProofKind.NONE,
            holder_code_hash=query.holder_code_hash,
            refusal=await _refusal(reads, holder, query.holder_code),
        )
    if verdict.state == HolderState.UNKNOWN_CONTRACT_CUSTODY:
        return CustodyFacts(
            holder=holder,
            state=verdict.state,
            proof_kind=LpProofKind.NONE,
            proof_contract=verdict.proof_contract,
            proof_version=verdict.proof_version,
            holder_code_hash=query.holder_code_hash,
            refusal=verdict.refusal or CustodyRefusal.DEPLOYMENT_MISMATCH,
        )
    if verdict.state not in (
        HolderState.PERMANENTLY_LOCKED,
        HolderState.TIMELOCKED,
        HolderState.RELEASABLE,
    ):
        # An adapter may only speak for locker states.
        return CustodyFacts(
            holder=holder,
            state=HolderState.UNKNOWN_CONTRACT_CUSTODY,
            proof_kind=LpProofKind.NONE,
            holder_code_hash=query.holder_code_hash,
            refusal=CustodyRefusal.CODE_NOT_RECOGNISED,
        )
    return CustodyFacts(
        holder=holder,
        state=verdict.state,
        controller=verdict.controller,
        unlock_block=verdict.unlock_block,
        proof_kind=LpProofKind.VERIFIED_LOCKER_CODE,
        proof_contract=verdict.proof_contract,
        proof_version=verdict.proof_version,
        holder_code_hash=query.holder_code_hash,
        controller_is_creator=_is_creator(verdict.controller, creator),
    )


__all__ = [
    "BURN_ADDRESSES",
    "BURN_DEAD",
    "DEFAULT_LOCKERS",
    "ZERO_ADDRESS",
    "LockerQuery",
    "LockerVerdict",
    "LpLockerAdapter",
    "classify_holder",
]
