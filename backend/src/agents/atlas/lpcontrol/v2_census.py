"""The V2 LP-token census against the read port: the pair's complete holder set.

At one pinned block:

1. the chain id is the registered deployment's;
2. the pair is bound to the registered factory: `pair.factory()`, its tokens,
   `factory.getPair(token0, token1) == pair`, and its runtime code hashes to
   the registered pair template;
3. the pair's creation block comes from the factory's own `PairCreated` log;
4. every `Transfer` of the LP token from creation to the pinned block is read,
   in bounded chunks, and every address that ever received LP is a candidate,
   plus the zero address (a V2 pair mints MINIMUM_LIQUIDITY to it, and a
   transfer to it is indistinguishable by event from a burn, so its balance is
   read, never reconstructed);
5. `totalSupply()` and every candidate's `balanceOf` are read; each nonzero
   candidate's balance must equal its reconstruction from the Transfers (a
   fee-on-transfer or rebasing LP token does not), and the read balances must
   sum *exactly* to `totalSupply()` -- since no balance is negative, that sum
   proves nobody outside the set holds any LP;
6. every holder with a balance is classified (`custody.classify_holder`).

Anything short of that is a named gap; the decision turns every gap into
UNKNOWN. Nothing here reads a provider's holder list.
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
    ZERO_ADDRESS,
    V2HolderBalance,
    V2LpEvidence,
    V2PairBinding,
)
from src.agents.atlas.lpcontrol.reads import (
    AbiDecodeError,
    BudgetedReads,
    LpChainReads,
    ReadBudgetExhausted,
    address_topic,
    address_word,
    as_address,
    calldata,
    topic,
    word_at,
    words_of,
)
from src.agents.atlas.lpcontrol.registry import V2Deployment
from src.agents.atlas.v4.custody.template import code_keccak

TRANSFER_TOPIC = topic("Transfer(address,address,uint256)")
PAIR_CREATED_TOPIC = topic("PairCreated(address,address,address,uint256)")


@dataclass(frozen=True)
class CensusLimits:
    max_requests: int = 600
    max_log_span: int = 50_000
    max_holders: int = 250
    max_positions: int = 250


DEFAULT_LIMITS = CensusLimits()


class CensusGap(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def creation_range(start: int, block: int, hint: int | None) -> tuple[int, int]:
    """Where to look for the creation log: the hinted block alone, or everything.

    A pool is created once, so the factory's creation log *at* a hinted block
    proves the creation block in one request (discovery already saw it). A
    hint outside the deployment's range is no hint; a wrong one finds nothing,
    which is a gap.
    """
    if hint is not None and start <= hint <= block:
        return hint, hint
    return start, block


async def census_v2(
    reads: LpChainReads,
    *,
    deployment: V2Deployment,
    pair: str,
    block: int,
    creator: str | None,
    created_block: int | None = None,
    adapters: Sequence[LpLockerAdapter] = DEFAULT_LOCKERS,
    limits: CensusLimits = DEFAULT_LIMITS,
) -> V2LpEvidence:
    budget = BudgetedReads(reads, limits.max_requests, limits.max_log_span)
    facts: dict[str, Any] = {
        "chain": deployment.chain,
        "chain_id": deployment.chain_id,
        "block": block,
        "binding": V2PairBinding(factory=deployment.factory, pair=pair),
    }
    try:
        await _census(
            budget, created_block, deployment, pair, block, creator, adapters, limits, facts
        )
    except CensusGap as gap:
        facts["gap"] = gap.code
    except ReadBudgetExhausted:
        facts["gap"] = "V2_READ_BUDGET_EXHAUSTED"
    except AbiDecodeError:
        facts["gap"] = "V2_READ_INVALID"
    except Exception:  # every failed or reverted read is a gap, never an answer
        facts["gap"] = "V2_READ_FAILED"
    if "gap" in facts:
        facts["scan_complete"] = False
    return V2LpEvidence.model_validate(facts)


async def _census(
    reads: BudgetedReads,
    created_hint: int | None,
    deployment: V2Deployment,
    pair: str,
    block: int,
    creator: str | None,
    adapters: Sequence[LpLockerAdapter],
    limits: CensusLimits,
    facts: dict[str, Any],
) -> None:
    if await reads.chain_id() != deployment.chain_id:
        raise CensusGap("V2_CHAIN_MISMATCH")
    factory = deployment.factory
    token0 = as_address(word_at(await reads.call(pair, calldata("token0()"), block), 0))
    token1 = as_address(word_at(await reads.call(pair, calldata("token1()"), block), 0))
    pair_factory = as_address(word_at(await reads.call(pair, calldata("factory()"), block), 0))
    get_pair = as_address(
        word_at(
            await reads.call(
                factory,
                calldata("getPair(address,address)", address_word(token0), address_word(token1)),
                block,
            ),
            0,
        )
    )
    code_hash = code_keccak(await reads.code(pair, block))
    binding = V2PairBinding(
        factory=factory,
        pair=pair,
        token0=token0,
        token1=token1,
        factory_get_pair=get_pair,
        pair_factory=pair_factory,
        pair_code_hash=code_hash,
        code_matches=code_hash == deployment.pair_code_hash,
    )
    facts["binding"] = binding
    if not (get_pair == pair and pair_factory == factory and binding.code_matches):
        raise CensusGap("V2_FACTORY_BINDING_UNVERIFIED")

    created = [
        item.block_number
        for item in await reads.scan(
            factory,
            (PAIR_CREATED_TOPIC, address_topic(token0), address_topic(token1)),
            *creation_range(deployment.start_block, block, created_hint),
        )
        if as_address(word_at(item.data, 0)) == pair
    ]
    if len(created) != 1:
        raise CensusGap("V2_PAIR_CREATION_UNKNOWN")
    facts["pair_created_block"] = created[0]

    reconstructed: dict[str, int] = {}
    for item in await reads.scan(pair, (TRANSFER_TOPIC,), created[0], block):
        if len(item.topics) != 3:
            raise CensusGap("V2_TRANSFER_LOG_INVALID")
        sender = as_address(words_of(item.topics[1])[0])
        receiver = as_address(words_of(item.topics[2])[0])
        value = word_at(item.data, 0)
        if sender != ZERO_ADDRESS:
            reconstructed[sender] = reconstructed.get(sender, 0) - value
        if receiver != ZERO_ADDRESS:
            reconstructed[receiver] = reconstructed.get(receiver, 0) + value
    candidates = sorted({ZERO_ADDRESS, *reconstructed})
    if len(candidates) > limits.max_holders:
        raise CensusGap("V2_HOLDER_LIMIT_EXCEEDED")
    if any(value < 0 for value in reconstructed.values()):
        raise CensusGap("V2_TRANSFER_RECONSTRUCTION_MISMATCH")

    facts["total_supply_raw"] = word_at(await reads.call(pair, calldata("totalSupply()"), block), 0)
    balances: dict[str, int] = {}
    for holder in candidates:
        balances[holder] = word_at(
            await reads.call(pair, calldata("balanceOf(address)", address_word(holder)), block), 0
        )
        if holder != ZERO_ADDRESS and balances[holder] != reconstructed[holder]:
            raise CensusGap("V2_TRANSFER_RECONSTRUCTION_MISMATCH")
    facts["scan_complete"] = True

    rows: list[V2HolderBalance] = []
    at_block = reads.at(block)
    for holder, balance in balances.items():
        if balance == 0:
            continue
        code = (
            "0x" if holder in BURN_ADDRESSES or holder == pair else await reads.code(holder, block)
        )
        custody = await classify_holder(
            at_block,
            LockerQuery(
                chain=deployment.chain,
                chain_id=deployment.chain_id,
                block=block,
                asset=pair,
                token_id=None,
                holder=holder,
                holder_code=code,
                holder_code_hash=code_keccak(code),
            ),
            pool=pair,
            creator=creator,
            adapters=adapters,
        )
        rows.append(V2HolderBalance(balance_raw=balance, custody=custody))
    facts["holders"] = tuple(rows)
    facts["block_timestamp"] = await reads.block_timestamp(block)
