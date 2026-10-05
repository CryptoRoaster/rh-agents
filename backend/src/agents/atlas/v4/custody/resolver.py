"""From a raw NFT owner to position control, through verified adapters only.

The census calls this once per distinct owner of a PositionManager position.
An account is its own controller. A contract is offered to each adapter in
turn; the first one that recognises its exact code decides, and a contract no
adapter recognises is unknown custody -- whatever it is called and however
long it has held the position.
"""

from collections.abc import Sequence

from src.agents.atlas.v4.control import (
    ControlCompleteness,
    ControlProofKind,
    CustodyQuery,
    CustodyReadPort,
    CustodyRefusal,
    CustodyVerdict,
    PositionControlFacts,
    PositionControlState,
    PositionCustodyAdapter,
    PositionOwnerKind,
)
from src.agents.atlas.v4.custody.template import code_keccak
from src.agents.atlas.v4.custody.uniswap_launcher import (
    FeeSplitterAdapter,
    TimelockedRecipientAdapter,
)

# EIP-7702 delegation designator: an externally owned account with delegated code.
DELEGATION_PREFIX = "0xef0100"
# EIP-1167 minimal proxy: a clone that delegates every call to a fixed address.
MINIMAL_PROXY_PREFIX = "0x363d3d373d3d3d363d73"
# EIP-1967 implementation and beacon slots.
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"

DEFAULT_ADAPTERS: tuple[PositionCustodyAdapter, ...] = (
    FeeSplitterAdapter(),
    TimelockedRecipientAdapter(),
)


def is_externally_owned(code: str) -> bool:
    """No code, or only an EIP-7702 delegation: an account a private key controls."""
    return code == "0x" or (code.startswith(DELEGATION_PREFIX) and len(code) == 2 + 46)


async def _refusal(reads: CustodyReadPort, owner: str, code: str) -> CustodyRefusal:
    """Why an unrecognised contract stayed unknown: a proxy, or just unknown code."""
    if code.startswith(MINIMAL_PROXY_PREFIX):
        return CustodyRefusal.PROXY_INDIRECTION
    for slot in (IMPLEMENTATION_SLOT, BEACON_SLOT):
        if int(await reads.storage(owner, slot), 16):
            return CustodyRefusal.PROXY_INDIRECTION
    return CustodyRefusal.CODE_NOT_RECOGNISED


async def resolve_owner(
    reads: CustodyReadPort,
    query: CustodyQuery,
    adapters: Sequence[PositionCustodyAdapter] = DEFAULT_ADAPTERS,
) -> PositionControlFacts:
    """What ``query.owner`` holding this position means for its control."""
    common = {
        "position_manager": query.position_manager,
        "token_id": query.token_id,
        "position_owner": query.owner,
    }
    if is_externally_owned(query.owner_code):
        return PositionControlFacts(
            **common,  # type: ignore[arg-type]
            owner_kind=PositionOwnerKind.EXTERNALLY_OWNED,
            control_state=PositionControlState.DIRECT_CONTROL,
            controller=query.owner,
            proof_kind=ControlProofKind.EXTERNALLY_OWNED_ACCOUNT,
            completeness=ControlCompleteness.VERIFIED,
        )
    verdict: CustodyVerdict | None = None
    for adapter in adapters:
        verdict = await adapter.verify(reads, query)
        if verdict is not None:
            break
    if verdict is None:
        return PositionControlFacts(
            **common,  # type: ignore[arg-type]
            owner_kind=PositionOwnerKind.CONTRACT,
            control_state=PositionControlState.UNKNOWN_CONTRACT_CUSTODY,
            proof_kind=ControlProofKind.NONE,
            owner_code_hash=query.owner_code_hash,
            completeness=ControlCompleteness.UNRECOGNISED,
            refusal=await _refusal(reads, query.owner, query.owner_code),
        )
    if verdict.state == PositionControlState.UNKNOWN_CONTRACT_CUSTODY:
        # The code was recognised; a binding it must carry did not hold.
        return PositionControlFacts(
            **common,  # type: ignore[arg-type]
            owner_kind=PositionOwnerKind.CONTRACT,
            control_state=verdict.state,
            proof_kind=ControlProofKind.NONE,
            proof_contract=verdict.proof_contract,
            proof_version=verdict.proof_version,
            owner_code_hash=query.owner_code_hash,
            completeness=ControlCompleteness.REFUTED,
            refusal=verdict.refusal,
        )
    return PositionControlFacts(
        **common,  # type: ignore[arg-type]
        owner_kind=PositionOwnerKind.CONTRACT,
        control_state=verdict.state,
        controller=verdict.controller,
        unlock_block=verdict.unlock_block,
        proof_kind=ControlProofKind.VERIFIED_CUSTODY_CODE,
        proof_contract=verdict.proof_contract,
        proof_version=verdict.proof_version,
        owner_code_hash=query.owner_code_hash,
        completeness=ControlCompleteness.VERIFIED,
    )


__all__ = ["DEFAULT_ADAPTERS", "code_keccak", "is_externally_owned", "resolve_owner"]
