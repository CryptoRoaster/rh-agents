"""FIXTURE ONLY: fake V2/V3 LP-control evidence for the pure decision tests.

Every address, hash and deployment here is invented. None of it was read from a
chain, and none of it may ever be copied into `LP_DEPLOYMENTS`: a production
registry entry needs on-chain verification (issue #102, docs/lp-control-v2-v3.md).
"""

from src.agents.atlas.lpcontrol.custody import BURN_DEAD, ZERO_ADDRESS
from src.agents.atlas.lpcontrol.decision import MarketPool
from src.agents.atlas.lpcontrol.models import (
    CustodyFacts,
    HolderState,
    LpProofKind,
    V2HolderBalance,
    V2LpEvidence,
    V2PairBinding,
    V3LpEvidence,
    V3PoolBinding,
    V3PositionEvidence,
    V3PositionKind,
)
from src.agents.atlas.lpcontrol.registry import (
    LpDeploymentRegistry,
    V2Deployment,
    V3Deployment,
)
from src.agents.atlas.v4.control import CustodyRefusal
from src.agents.atlas.v4.custody.template import CodeTemplate

FIXTURE_CHAIN = "fixture-chain"
FIXTURE_CHAIN_ID = 990_001
FIXTURE_BLOCK = 1_000_000
FIXTURE_TIMESTAMP = 1_760_000_000

TOKEN = "0x" + "a1" * 20
QUOTE = "0x" + "a2" * 20
V2_FACTORY = "0x" + "f2" * 20
V2_PAIR = "0x" + "b2" * 20
V2_PAIR_CODE_HASH = "0x" + "c2" * 32
V3_FACTORY = "0x" + "f3" * 20
V3_POOL = "0x" + "b3" * 20
NPM = "0x" + "e3" * 20
CREATOR = "0x" + "cc" * 20
THIRD_PARTY = "0x" + "3f" * 20
LOCKER = "0x" + "10" * 20
UNKNOWN_VAULT = "0x" + "77" * 20
CODE_HASH = "0x" + "ab" * 32

FIXTURE_V3_TEMPLATE = CodeTemplate(
    contract="FixturePool",
    version="fixture",
    source_repository="fixture",
    source_commit="fixture",
    build="fixture",
    length=64,
    masked_keccak="0x" + "00" * 32,
    immutables={},
)

FIXTURE_REGISTRY = LpDeploymentRegistry(
    v2=(
        V2Deployment(
            chain=FIXTURE_CHAIN,
            chain_id=FIXTURE_CHAIN_ID,
            venue="fixture-v2",
            factory=V2_FACTORY,
            pair_code_hash=V2_PAIR_CODE_HASH,
            start_block=1,
        ),
    ),
    v3=(
        V3Deployment(
            chain=FIXTURE_CHAIN,
            chain_id=FIXTURE_CHAIN_ID,
            venue="fixture-v3",
            factory=V3_FACTORY,
            position_manager=NPM,
            pool_template=FIXTURE_V3_TEMPLATE,
            start_block=1,
        ),
    ),
)

V2_MARKET = MarketPool(chain=FIXTURE_CHAIN, pool=V2_PAIR, token=TOKEN)
V3_MARKET = MarketPool(chain=FIXTURE_CHAIN, pool=V3_POOL, token=TOKEN)


# ------------------------------------------------------------------ custody


def burned(holder: str = ZERO_ADDRESS) -> CustodyFacts:
    return CustodyFacts(
        holder=holder, state=HolderState.BURNED, proof_kind=LpProofKind.BURN_ADDRESS
    )


def locked(holder: str = LOCKER) -> CustodyFacts:
    return CustodyFacts(
        holder=holder,
        state=HolderState.PERMANENTLY_LOCKED,
        proof_kind=LpProofKind.VERIFIED_LOCKER_CODE,
        proof_contract="FixtureLocker",
        proof_version="fixture",
        holder_code_hash=CODE_HASH,
    )


def timelocked(holder: str = LOCKER, *, unlock: int = FIXTURE_BLOCK + 10) -> CustodyFacts:
    return CustodyFacts(
        holder=holder,
        state=HolderState.TIMELOCKED,
        unlock_block=unlock,
        proof_kind=LpProofKind.VERIFIED_LOCKER_CODE,
        proof_contract="FixtureLocker",
        proof_version="fixture",
        holder_code_hash=CODE_HASH,
    )


def releasable(controller: str, *, holder: str = LOCKER) -> CustodyFacts:
    return CustodyFacts(
        holder=holder,
        state=HolderState.RELEASABLE,
        controller=controller,
        unlock_block=FIXTURE_BLOCK - 10,
        proof_kind=LpProofKind.VERIFIED_LOCKER_CODE,
        proof_contract="FixtureLocker",
        proof_version="fixture",
        holder_code_hash=CODE_HASH,
        controller_is_creator=controller == CREATOR,
    )


def account(holder: str, *, creator: bool | None = None) -> CustodyFacts:
    return CustodyFacts(
        holder=holder,
        state=HolderState.DIRECT_CONTROL,
        controller=holder,
        proof_kind=LpProofKind.EXTERNALLY_OWNED_ACCOUNT,
        controller_is_creator=(holder == CREATOR) if creator is None else creator,
    )


def unknown_contract(holder: str = UNKNOWN_VAULT) -> CustodyFacts:
    return CustodyFacts(
        holder=holder,
        state=HolderState.UNKNOWN_CONTRACT_CUSTODY,
        proof_kind=LpProofKind.NONE,
        holder_code_hash=CODE_HASH,
        refusal=CustodyRefusal.CODE_NOT_RECOGNISED,
    )


def pool_redeemable(holder: str = V2_PAIR) -> CustodyFacts:
    return CustodyFacts(
        holder=holder, state=HolderState.POOL_REDEEMABLE, proof_kind=LpProofKind.POOL_SEMANTICS
    )


# ----------------------------------------------------------------------- V2


def v2_binding(**changes: object) -> V2PairBinding:
    values: dict[str, object] = {
        "factory": V2_FACTORY,
        "pair": V2_PAIR,
        "token0": TOKEN,
        "token1": QUOTE,
        "factory_get_pair": V2_PAIR,
        "pair_factory": V2_FACTORY,
        "pair_code_hash": V2_PAIR_CODE_HASH,
        "code_matches": True,
    }
    values.update(changes)
    return V2PairBinding.model_validate(values)


def v2_evidence(
    holders: tuple[tuple[CustodyFacts, int], ...] | None = None, **changes: object
) -> V2LpEvidence:
    """A complete, reconciled V2 census whose LP supply is all burned or locked."""
    if holders is None:
        holders = ((burned(ZERO_ADDRESS), 1_000), (burned(BURN_DEAD), 999_000))
    rows = tuple(V2HolderBalance(balance_raw=amount, custody=facts) for facts, amount in holders)
    values: dict[str, object] = {
        "chain": FIXTURE_CHAIN,
        "chain_id": FIXTURE_CHAIN_ID,
        "block": FIXTURE_BLOCK,
        "block_timestamp": FIXTURE_TIMESTAMP,
        "binding": v2_binding(),
        "pair_created_block": 10,
        "scan_complete": True,
        "total_supply_raw": sum(row.balance_raw for row in rows),
        "holders": rows,
    }
    values.update(changes)
    return V2LpEvidence.model_validate(values)


# ----------------------------------------------------------------------- V3


def v3_binding(**changes: object) -> V3PoolBinding:
    values: dict[str, object] = {
        "factory": V3_FACTORY,
        "pool": V3_POOL,
        "position_manager": NPM,
        "token0": TOKEN,
        "token1": QUOTE,
        "fee": 3000,
        "factory_get_pool": V3_POOL,
        "pool_factory": V3_FACTORY,
        "pool_code_matches": True,
        "position_manager_factory": V3_FACTORY,
    }
    values.update(changes)
    return V3PoolBinding.model_validate(values)


def npm_position(token_id: int, liquidity: int, custody: CustodyFacts | None) -> V3PositionEvidence:
    return V3PositionEvidence(
        kind=V3PositionKind.POSITION_MANAGER,
        owner=NPM,
        tick_lower=-600,
        tick_upper=600,
        liquidity=liquidity,
        token_id=token_id,
        custody=custody,
    )


def direct_position(owner: str, liquidity: int, custody: CustodyFacts | None) -> V3PositionEvidence:
    return V3PositionEvidence(
        kind=V3PositionKind.DIRECT,
        owner=owner,
        tick_lower=-600,
        tick_upper=600,
        liquidity=liquidity,
        custody=custody,
    )


def v3_evidence(
    positions: tuple[V3PositionEvidence, ...] | None = None, **changes: object
) -> V3LpEvidence:
    """A complete, reconciled V3 census whose every position NFT is locked."""
    if positions is None:
        positions = (npm_position(1, 5_000, locked()),)
    active = sum(item.liquidity for item in positions)
    values: dict[str, object] = {
        "chain": FIXTURE_CHAIN,
        "chain_id": FIXTURE_CHAIN_ID,
        "block": FIXTURE_BLOCK,
        "block_timestamp": FIXTURE_TIMESTAMP,
        "binding": v3_binding(),
        "pool_created_block": 10,
        "scan_complete": True,
        "position_manager_reconciled": True,
        "current_tick": 0,
        "active_liquidity": active,
        "active_liquidity_reconciled": True,
        "positions": positions,
    }
    values.update(changes)
    return V3LpEvidence.model_validate(values)
