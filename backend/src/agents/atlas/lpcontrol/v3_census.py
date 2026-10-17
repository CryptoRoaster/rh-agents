"""The V3 position census against the read port: every position and its owner.

At one pinned block:

1. the chain id is the registered deployment's;
2. the pool is bound to the registered factory: `pool.factory()`, its tokens
   and fee, `factory.getPool(token0, token1, fee) == pool`, its runtime code
   matching the registered pool template with the factory/token0/token1/fee
   immutables equal to those reads, and the position manager's `factory()`;
3. the pool's creation block comes from the factory's own `PoolCreated` log;
4. every pool `Mint` from creation to the pinned block names a position key
   (owner, tickLower, tickUpper); each key's liquidity is read from the pool's
   own `positions(key)`;
5. for the keys the position manager owns, its token ids come from its
   `IncreaseLiquidity` logs in exactly the blocks where it minted into this
   pool; `positions(tokenId)` binds each to this pool and range (a reverted
   read is a burned token, which held nothing), and per key the tokens'
   liquidity must sum *exactly* to the pool's;
6. `slot0().tick` and `liquidity()` are read, and the keys in range must sum
   *exactly* to the pool's active liquidity;
7. each NFT with liquidity is resolved through `ownerOf`, each direct
   position's owner directly, and both are classified (`custody.classify_holder`).

Anything short of that is a named gap; the decision turns it into UNKNOWN.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.agents.atlas.lpcontrol.custody import (
    DEFAULT_LOCKERS,
    LockerQuery,
    LpLockerAdapter,
    classify_holder,
)
from src.agents.atlas.lpcontrol.models import (
    BURN_ADDRESSES,
    V3LpEvidence,
    V3PoolBinding,
    V3PositionEvidence,
    V3PositionKind,
)
from src.agents.atlas.lpcontrol.reads import (
    AbiDecodeError,
    BudgetedReads,
    CallReverted,
    LpChainReads,
    ReadBudgetExhausted,
    address_topic,
    address_word,
    as_address,
    as_int24,
    as_uint,
    calldata,
    topic,
    word,
    word_at,
    words_of,
)
from src.agents.atlas.lpcontrol.registry import V3Deployment
from src.agents.atlas.lpcontrol.v2_census import (
    DEFAULT_LIMITS,
    CensusGap,
    CensusLimits,
    creation_range,
)
from src.agents.atlas.v4.custody.template import code_keccak, match_template
from src.agents.atlas.v4.keccak import keccak256

POOL_CREATED_TOPIC = topic("PoolCreated(address,address,uint24,int24,address)")
MINT_TOPIC = topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
INCREASE_LIQUIDITY_TOPIC = topic("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
# The pool template's immutables that must equal the binding reads.
BOUND_IMMUTABLES = ("factory", "token0", "token1", "fee")


def position_key(owner: str, tick_lower: int, tick_upper: int) -> str:
    """`PositionKey.compute`: keccak256(abi.encodePacked(owner, int24, int24))."""
    packed = (
        bytes.fromhex(owner[2:])
        + (tick_lower % (1 << 24)).to_bytes(3, "big")
        + (tick_upper % (1 << 24)).to_bytes(3, "big")
    )
    return keccak256(packed).hex()


@dataclass(frozen=True)
class _Token:
    token_id: int
    tick_lower: int
    tick_upper: int
    liquidity: int


async def census_v3(
    reads: LpChainReads,
    *,
    deployment: V3Deployment,
    pool: str,
    block: int,
    creator: str | None,
    created_block: int | None = None,
    adapters: Sequence[LpLockerAdapter] = DEFAULT_LOCKERS,
    limits: CensusLimits = DEFAULT_LIMITS,
) -> V3LpEvidence:
    budget = BudgetedReads(reads, limits.max_requests, limits.max_log_span)
    facts: dict[str, Any] = {
        "chain": deployment.chain,
        "chain_id": deployment.chain_id,
        "block": block,
        "binding": V3PoolBinding(
            factory=deployment.factory,
            pool=pool,
            position_manager=deployment.position_manager,
        ),
    }
    try:
        await _census(
            budget, created_block, deployment, pool, block, creator, adapters, limits, facts
        )
    except CensusGap as gap:
        facts["gap"] = gap.code
    except ReadBudgetExhausted:
        facts["gap"] = "V3_READ_BUDGET_EXHAUSTED"
    except AbiDecodeError:
        facts["gap"] = "V3_READ_INVALID"
    except Exception:  # every failed read is a gap, never an answer
        facts["gap"] = "V3_READ_FAILED"
    if "gap" in facts:
        facts["scan_complete"] = False
    return V3LpEvidence.model_validate(facts)


async def _address(reads: BudgetedReads, target: str, data: str, block: int) -> str:
    return as_address(word_at(await reads.call(target, data, block), 0))


async def _census(
    reads: BudgetedReads,
    created_hint: int | None,
    deployment: V3Deployment,
    pool: str,
    block: int,
    creator: str | None,
    adapters: Sequence[LpLockerAdapter],
    limits: CensusLimits,
    facts: dict[str, Any],
) -> None:
    if await reads.chain_id() != deployment.chain_id:
        raise CensusGap("V3_CHAIN_MISMATCH")
    factory, npm = deployment.factory, deployment.position_manager
    token0 = await _address(reads, pool, calldata("token0()"), block)
    token1 = await _address(reads, pool, calldata("token1()"), block)
    fee = as_uint(word_at(await reads.call(pool, calldata("fee()"), block), 0), 24)
    pool_factory = await _address(reads, pool, calldata("factory()"), block)
    get_pool = await _address(
        reads,
        factory,
        calldata(
            "getPool(address,address,uint24)", address_word(token0), address_word(token1), word(fee)
        ),
        block,
    )
    immutables = match_template(await reads.code(pool, block), deployment.pool_template)
    expected = {"factory": int(factory, 16), "token0": int(token0, 16), "token1": int(token1, 16)}
    expected["fee"] = fee
    code_matches = immutables is not None and all(
        immutables.get(name) == expected[name] for name in BOUND_IMMUTABLES if name in immutables
    )
    npm_factory = await _address(reads, npm, calldata("factory()"), block)
    binding = V3PoolBinding(
        factory=factory,
        pool=pool,
        position_manager=npm,
        token0=token0,
        token1=token1,
        fee=fee,
        factory_get_pool=get_pool,
        pool_factory=pool_factory,
        pool_code_matches=code_matches,
        position_manager_factory=npm_factory,
    )
    facts["binding"] = binding
    if not (
        get_pool == pool and pool_factory == factory and code_matches and npm_factory == factory
    ):
        raise CensusGap("V3_FACTORY_BINDING_UNVERIFIED")

    created = [
        item.block_number
        for item in await reads.scan(
            factory,
            (POOL_CREATED_TOPIC, address_topic(token0), address_topic(token1), "0x" + word(fee)),
            *creation_range(deployment.start_block, block, created_hint),
        )
        if as_address(word_at(item.data, 1)) == pool
    ]
    if len(created) != 1:
        raise CensusGap("V3_POOL_CREATION_UNKNOWN")
    facts["pool_created_block"] = created[0]

    keys: set[tuple[str, int, int]] = set()
    npm_blocks: set[int] = set()
    for item in await reads.scan(pool, (MINT_TOPIC,), created[0], block):
        if len(item.topics) != 4:
            raise CensusGap("V3_MINT_LOG_INVALID")
        owner = as_address(words_of(item.topics[1])[0])
        keys.add(
            (owner, as_int24(words_of(item.topics[2])[0]), as_int24(words_of(item.topics[3])[0]))
        )
        if owner == npm:
            npm_blocks.add(item.block_number)
    if len(keys) > limits.max_positions:
        raise CensusGap("V3_POSITION_LIMIT_EXCEEDED")
    key_liquidity: dict[tuple[str, int, int], int] = {}
    for key in sorted(keys):
        result = await reads.call(pool, calldata("positions(bytes32)", position_key(*key)), block)
        key_liquidity[key] = as_uint(word_at(result, 0), 128)

    token_ids: set[int] = set()
    for minted in sorted(npm_blocks):
        for item in await reads.scan(npm, (INCREASE_LIQUIDITY_TOPIC,), minted, minted):
            token_ids.add(words_of(item.topics[1])[0])
    if len(token_ids) > limits.max_positions:
        raise CensusGap("V3_POSITION_LIMIT_EXCEEDED")
    tokens: list[_Token] = []
    for token_id in sorted(token_ids):
        try:
            result = await reads.call(npm, calldata("positions(uint256)", word(token_id)), block)
        except CallReverted:
            continue  # burned: a position NFT is burned only once emptied
        values = words_of(result)
        if len(values) < 8:
            raise AbiDecodeError("positions(uint256) too short")
        if (as_address(values[2]), as_address(values[3]), as_uint(values[4], 24)) != (
            token0,
            token1,
            fee,
        ):
            continue  # another pool's token minted in the same block
        tokens.append(
            _Token(token_id, as_int24(values[5]), as_int24(values[6]), as_uint(values[7], 128))
        )
    npm_sums: dict[tuple[int, int], int] = {}
    for token in tokens:
        span = (token.tick_lower, token.tick_upper)
        npm_sums[span] = npm_sums.get(span, 0) + token.liquidity
    npm_keys = {
        (lower, upper): value
        for (owner, lower, upper), value in key_liquidity.items()
        if owner == npm
    }
    reconciled = all(npm_sums.get(span, 0) == value for span, value in npm_keys.items()) and all(
        span in npm_keys for span in npm_sums
    )
    facts["position_manager_reconciled"] = reconciled
    facts["scan_complete"] = True

    tick = as_int24(word_at(await reads.call(pool, calldata("slot0()"), block), 1))
    active = as_uint(word_at(await reads.call(pool, calldata("liquidity()"), block), 0), 128)
    facts["current_tick"] = tick
    facts["active_liquidity"] = active
    facts["active_liquidity_reconciled"] = active == sum(
        value for (_, lower, upper), value in key_liquidity.items() if lower <= tick < upper
    )

    at_block = reads.at(block)

    async def custody_of(holder: str, asset: str, token_id: int | None) -> Any:
        code = "0x" if holder in BURN_ADDRESSES else await reads.code(holder, block)
        return await classify_holder(
            at_block,
            LockerQuery(
                chain=deployment.chain,
                chain_id=deployment.chain_id,
                block=block,
                asset=asset,
                token_id=token_id,
                holder=holder,
                holder_code=code,
                holder_code_hash=code_keccak(code),
            ),
            pool=pool,
            creator=creator,
            adapters=adapters,
        )

    positions: list[V3PositionEvidence] = []
    for token in tokens:
        custody = None
        if token.liquidity:
            owner = await _address(
                reads, npm, calldata("ownerOf(uint256)", word(token.token_id)), block
            )
            custody = await custody_of(owner, npm, token.token_id)
        positions.append(
            V3PositionEvidence(
                kind=V3PositionKind.POSITION_MANAGER,
                owner=npm,
                tick_lower=token.tick_lower,
                tick_upper=token.tick_upper,
                liquidity=token.liquidity,
                token_id=token.token_id,
                custody=custody,
            )
        )
    for (owner, lower, upper), value in sorted(key_liquidity.items()):
        if owner == npm:
            continue
        positions.append(
            V3PositionEvidence(
                kind=V3PositionKind.DIRECT,
                owner=owner,
                tick_lower=lower,
                tick_upper=upper,
                liquidity=value,
                custody=await custody_of(owner, pool, None) if value else None,
            )
        )
    facts["positions"] = tuple(positions)
    facts["block_timestamp"] = await reads.block_timestamp(block)
