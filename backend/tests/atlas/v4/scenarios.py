"""Declared V4 scenarios, assembled through the real ATLAS snapshot builder.

The REVENUE-like scenario models the mechanism of the historical rug — a traded
pool beside a never-traded second pool whose creator-owned hook and creator-
owned single-sided position hold ~57.4 % of supply — with synthetic addresses.
Nothing here or in the code under test keys on the historical token.
"""

from decimal import Decimal

from src.agents.atlas.context import AtlasSnapshotBuilder
from src.agents.atlas.models import (
    AtlasOnchainSnapshot,
    HolderCompleteness,
    HolderSourceRow,
    OriginFacts,
    OriginVerification,
)
from src.agents.atlas.v4.census import CensusBounds, V4PoolCensus
from src.agents.atlas.v4.math import Q96, sqrt_price_at_tick
from src.agents.atlas.v4.protocol import V4Deployment
from src.core.clock import FixedClock
from src.markets.models import Availability, MarketIdentity, PoolLocatorIdentity, PoolLocatorKind
from tests.atlas.conftest import (
    CREATOR,
    QUOTE,
    TOKEN,
    StubContracts,
    StubHolders,
    StubOrigins,
    chain_snapshot,
    contract_facts,
    holder_source_result,
    market_identity,
)
from tests.atlas.v4.chain import (
    CHAIN_ID,
    CREATED_BLOCK,
    HEAD_BLOCK,
    NATIVE,
    POOL_MANAGER,
    POSITION_MANAGER,
    FakeV4Chain,
    Pool,
)

SUPPLY = 1_000_000_000 * 10**18
UNIT = 10**18
# Bits 13 and 7: BEFORE_INITIALIZE and BEFORE_SWAP, as on the historical hook.
CREATOR_HOOK = "0x" + "5d" * 18 + "e080"
LOCKER = "0x" + "ef" * 20
# A higher address than the token, so the token sorts as currency0 against it.
MEME = "0x" + "fe" * 20
FULL_RANGE = (-887272, 887272)
# The second pool's position as it was on-chain: range and liquidity.
HIDDEN_RANGE = (-887272, 299351)
HIDDEN_LIQUIDITY = 181514997636243302190


def deployment(*managers: str) -> V4Deployment:
    return V4Deployment(
        chain="robinhood",
        chain_id=CHAIN_ID,
        pool_manager=POOL_MANAGER,
        position_managers=managers or (POSITION_MANAGER,),
    )


def census_for(chain: FakeV4Chain, *, bounds: CensusBounds | None = None) -> V4PoolCensus:
    return V4PoolCensus(
        reads=chain,
        chain="robinhood",
        bounds=bounds or CensusBounds(),
        deployments={("robinhood", CHAIN_ID): deployment(*chain.position_managers)},
    )


def liquidity_for(target: int, tick: int, lower: int) -> int:
    """Liquidity that holds ``target`` token1 units in range above ``lower``."""
    return target * Q96 // (sqrt_price_at_tick(tick) - sqrt_price_at_tick(lower))


def wallets(count: int = 12, each: int = 9_500_000 * UNIT) -> tuple[HolderSourceRow, ...]:
    """A flat raw distribution: ten of these make ~9.5 % of supply."""
    return tuple(
        HolderSourceRow(address="0x" + f"{index + 0x30:02x}" * 20, balance_raw=each - index)
        for index in range(count)
    )


def v4_market(pool: Pool) -> MarketIdentity:
    locator = PoolLocatorIdentity(
        kind=PoolLocatorKind.BYTES32_POOL_ID, value=pool.id, venue="uniswap-v4"
    )
    return MarketIdentity(
        provider="geckoterminal",
        chain="robinhood",
        network="mainnet",
        pair_id=locator.pair_id("robinhood", "mainnet"),
        base_asset_id=f"robinhood:mainnet:{TOKEN}",
        quote_asset_id=f"robinhood:mainnet:{QUOTE}",
        venue="uniswap-v4",
        pool_locator=locator,
        is_fixture=False,
    )


def traded_pool(**changes: object) -> Pool:
    values: dict[str, object] = {
        "currency0": NATIVE,
        "currency1": TOKEN,
        "fee": 2500,
        "tick_spacing": 25,
        "created_block": CREATED_BLOCK + 300,
        "tick": 190_000,
    }
    values.update(changes)
    return Pool(**values)  # type: ignore[arg-type]


def revenue_like() -> tuple[FakeV4Chain, Pool, Pool]:
    """The historical mechanism, on synthetic addresses."""
    chain = FakeV4Chain(token=TOKEN)
    chain.contracts.add(LOCKER)
    traded = chain.add_pool(traded_pool())
    hidden = chain.add_pool(
        Pool(
            currency0=NATIVE,
            currency1=TOKEN,
            fee=100,
            tick_spacing=1,
            hook=CREATOR_HOOK,
            created_block=CREATED_BLOCK + 339,
            # Above the position's range: the position is entirely token.
            tick=300_000,
        )
    )
    chain.hook_owners[CREATOR_HOOK] = CREATOR
    # The traded pool's launch liquidity, held by a locker contract's NFT.
    chain.mint(
        traded,
        LOCKER,
        3_498_758,
        -160_100,
        198_050,
        liquidity_for(150_000_000 * UNIT, traded.tick, -160_100),
    )
    # The creator's single-sided position in the never-traded pool.
    chain.mint(hidden, CREATOR, 3_498_775, *HIDDEN_RANGE, HIDDEN_LIQUIDITY, block=77_187_674)
    chain.extra_pool_balance = 12_345 * UNIT
    return chain, traded, hidden


def origin(*, verification: OriginVerification = OriginVerification.RECEIPT_CONFIRMED):
    return OriginFacts(
        status=Availability.AVAILABLE,
        source="test-origin",
        creator_address=CREATOR,
        creation_block=CREATED_BLOCK,
        creation_tx_hash="0x" + "11" * 32,
        verification=verification,
    )


def builder(
    now,
    chain: FakeV4Chain | None,
    *,
    rows: tuple[HolderSourceRow, ...] | None = None,
    completeness: HolderCompleteness = HolderCompleteness.TOP_N_ONLY,
    origin_facts: OriginFacts | None = None,
    bounds: CensusBounds | None = None,
    block: int = HEAD_BLOCK,
) -> AtlasSnapshotBuilder:
    return AtlasSnapshotBuilder(
        contracts=StubContracts(
            chain_snapshot(now, block=block), contract_facts(total_supply_raw=SUPPLY, block=block)
        ),
        holders=StubHolders(
            holder_source_result(
                now,
                rows=wallets() if rows is None else rows,
                completeness=completeness,
                snapshot_block=block,
                provider_total_supply_raw=SUPPLY,
            )
        ),
        origins=StubOrigins(origin() if origin_facts is None else origin_facts),
        pool_census=None if chain is None else census_for(chain, bounds=bounds),
        clock=FixedClock(now),
    )


async def build(now, chain: FakeV4Chain | None, market: MarketIdentity | None = None, **kw):
    from uuid import uuid4

    pools = [] if chain is None else chain.pools
    identity = market or (v4_market(pools[0]) if pools else market_identity())
    snapshot: AtlasOnchainSnapshot = await builder(now, chain, **kw).build(
        uuid4(), uuid4(), identity
    )
    return snapshot


def share(units: int) -> Decimal:
    return Decimal(units) / Decimal(SUPPLY)
