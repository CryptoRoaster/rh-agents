"""Issue #94: a PoolManager-wide pool index, so pool discovery covers its whole history.

v4-core lets a pool be initialized for a token address before the token exists,
so a census that looks for pools from the token's creation block can miss one
(review finding B1 on PR #99). The index records every Initialize the verified
PoolManager emitted, from its verified deployment block, up to a confirmed
block, incrementally and reorg-checked; the census takes the pools initialized
before the token from it and records that its discovery covers the PoolManager's
whole history. Nothing here reads a provider.
"""

from src.agents.atlas.v4.census import CensusBounds, ChainLog
from src.agents.atlas.v4.pool_index import PoolIndexHistory, V4PoolIndexer
from src.agents.atlas.v4.protocol import INITIALIZE_TOPIC
from src.markets.models import Availability
from src.runtime.models import ErrorCode
from tests.atlas.conftest import TOKEN, chain_snapshot
from tests.atlas.v4.chain import CREATED_BLOCK, HEAD_BLOCK, NATIVE, FakeV4Chain, Pool
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import HIDDEN_RANGE, MEME, census_for, deployment, revenue_like

DEPLOYED = HEAD_BLOCK - 350_000
CONFIRMATIONS = 20
OTHER = "0x" + "b2" * 20


def indexed_chain() -> tuple[FakeV4Chain, Pool, Pool]:
    chain, traded, hidden = revenue_like("official")
    chain.manager_deployed_block = DEPLOYED
    # Before the token existed: a pool of the token, and pools of other tokens.
    early = chain.add_pool(
        Pool(
            currency0=NATIVE,
            currency1=TOKEN,
            fee=500,
            tick_spacing=10,
            created_block=CREATED_BLOCK - 1_000,
            tick=300_000,
        )
    )
    chain.mint(early, "0x" + "ad" * 20, 9_999_999, *HIDDEN_RANGE, 10**18, block=CREATED_BLOCK + 50)
    chain.foreign_pools += [
        Pool(
            currency0=NATIVE, currency1=OTHER, fee=3000, tick_spacing=60, created_block=DEPLOYED + 5
        ),
        Pool(
            currency0=MEME, currency1=OTHER, fee=500, tick_spacing=10, created_block=HEAD_BLOCK - 10
        ),
    ]
    return chain, traded, early


def indexer(chain: FakeV4Chain, store: MemoryPoolIndexStore, *, max_requests: int = 200):
    return V4PoolIndexer(
        reads=chain,
        store=store,
        deployment=deployment(*chain.position_managers),
        confirmations=CONFIRMATIONS,
        max_requests=max_requests,
    )


async def test_the_index_covers_every_pool_from_the_verified_deployment(now) -> None:
    chain, traded, early = indexed_chain()
    store = MemoryPoolIndexStore()
    result = await indexer(chain, store).advance()
    assert result.status == "COMPLETE", result
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None
    assert coverage.deployment_block == DEPLOYED
    assert coverage.through_block == HEAD_BLOCK - CONFIRMATIONS
    ids = set(store.pools[(chain.network_id, chain.pool_manager)])
    # Every pool up to the confirmed block, whichever tokens it pairs, and none beyond.
    assert {traded.id, early.id, chain.foreign_pools[0].id} <= ids
    assert chain.foreign_pools[1].id not in ids


async def test_a_budget_stops_the_index_where_it_got_and_the_next_run_resumes(now) -> None:
    chain, _, _ = indexed_chain()
    store = MemoryPoolIndexStore()
    first = await indexer(chain, store, max_requests=34).advance()
    assert first.status == "PARTIAL"
    partial = await store.coverage(chain.network_id, chain.pool_manager)
    assert partial is not None and DEPLOYED <= partial.through_block < HEAD_BLOCK - CONFIRMATIONS
    second = await indexer(chain, store).advance()
    assert second.status == "COMPLETE"
    full = MemoryPoolIndexStore()
    await indexer(indexed_chain()[0], full).advance()
    assert store.pools == full.pools
    assert store.coverages == full.coverages


async def test_a_reorganised_tip_is_rewound_and_read_again(now) -> None:
    chain, _, _ = indexed_chain()
    store = MemoryPoolIndexStore()
    await indexer(chain, store).advance()
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None
    # The recorded tip is no longer canonical; a pool appeared just below it.
    chain.reorged[coverage.through_block] = "0x" + "ee" * 32
    late = Pool(
        currency0=NATIVE,
        currency1=OTHER,
        fee=100,
        tick_spacing=1,
        created_block=coverage.through_block - 3,
    )
    chain.foreign_pools.append(late)
    result = await indexer(chain, store).advance()
    assert result.status == "COMPLETE" and result.rewound
    assert late.id in store.pools[(chain.network_id, chain.pool_manager)]


async def test_an_initialize_whose_id_does_not_hash_from_its_key_stops_the_index(now) -> None:
    chain, _, _ = indexed_chain()

    def tamper(log: ChainLog) -> ChainLog:
        if log.topics[0] != INITIALIZE_TOPIC:
            return log
        return ChainLog(
            log.block_number, (log.topics[0], "0x" + "99" * 32, *log.topics[2:]), log.data
        )

    chain.tamper_log = tamper
    store = MemoryPoolIndexStore()
    result = await indexer(chain, store).advance()
    assert result.status == "FAILED"
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is None or coverage.through_block < DEPLOYED + 100_000
    assert not store.pools.get((chain.network_id, chain.pool_manager))


async def test_a_transport_failure_keeps_what_was_confirmed(now) -> None:
    chain, _, _ = indexed_chain()
    chain.fail_at = 34
    chain.fail_with = ErrorCode.RATE_LIMITED
    store = MemoryPoolIndexStore()
    result = await indexer(chain, store).advance()
    assert result.status == "FAILED"
    resumed = await indexer(chain, store).advance()
    assert resumed.status == "COMPLETE"


async def test_the_history_answers_only_when_it_covers_the_question(now) -> None:
    chain, _, early = indexed_chain()
    store = MemoryPoolIndexStore()
    history = PoolIndexHistory(store)
    assert await history.history(chain.network_id, chain.pool_manager, TOKEN, CREATED_BLOCK) is None
    await indexer(chain, store).advance()
    answer = await history.history(chain.network_id, chain.pool_manager, TOKEN, CREATED_BLOCK)
    assert answer is not None and answer.deployment_block == DEPLOYED
    assert [pool.pool_id for pool in answer.pools] == [early.id]
    # Not for a question beyond what the index confirmed.
    assert await history.history(chain.network_id, chain.pool_manager, TOKEN, HEAD_BLOCK) is None


async def census(now, chain: FakeV4Chain, store: MemoryPoolIndexStore | None):
    found = census_for(chain, bounds=CensusBounds())
    if store is not None:
        from dataclasses import replace

        found = replace(found, index=PoolIndexHistory(store))
    return await found.census(chain_snapshot(now, block=HEAD_BLOCK), TOKEN, CREATED_BLOCK)


async def test_the_census_finds_a_pool_initialized_before_the_token(now) -> None:
    chain, traded, early = indexed_chain()
    without = await census(now, chain, None)
    assert early.id not in {pool.pool_id for pool in without.pools}
    assert without.pool_discovery_basis == "TOKEN_CREATION"
    assert without.pool_discovery_from_block == CREATED_BLOCK

    store = MemoryPoolIndexStore()
    await indexer(indexed_chain()[0], store).advance()
    found = await census(now, indexed_chain()[0], store)
    assert found.status is Availability.AVAILABLE
    assert early.id in {pool.pool_id for pool in found.pools}
    assert early.id in {item.pool_id for item in found.positions}
    assert found.pool_discovery_basis == "MANAGER_HISTORY"
    assert found.pool_discovery_from_block == DEPLOYED
    assert found.reconciliation is not None and found.reconciliation.surplus_raw == 12_345 * 10**18


async def test_a_forged_index_entry_makes_the_census_incomplete(now) -> None:
    from src.agents.atlas.v4.pool_index import IndexedPool

    chain, _, _ = indexed_chain()
    store = MemoryPoolIndexStore()
    await indexer(chain, store).advance()
    key = (chain.network_id, chain.pool_manager)
    forged = IndexedPool(
        pool_id="0x" + "77" * 32,
        currency0=NATIVE,
        currency1=TOKEN,
        fee=500,
        tick_spacing=10,
        hooks=NATIVE,
        block_number=CREATED_BLOCK - 10,
    )
    store.pools[key][forged.pool_id] = forged
    found = await census(now, indexed_chain()[0], store)
    assert found.status is Availability.UNAVAILABLE
    assert found.gap is not None and found.gap.value == "V4_POOL_KEY_MISMATCH"


async def test_a_rewind_between_the_two_reads_is_not_answered_as_covered(now) -> None:
    # Review N1: coverage and pools are read separately; a rewind in between
    # must not yield a "covered" answer missing pools.
    chain, _, early = indexed_chain()
    store = MemoryPoolIndexStore()
    await indexer(chain, store).advance()
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None
    original = store.pools_with

    async def racing(chain_id, pool_manager, token, before_block):
        from dataclasses import replace

        back = replace(
            coverage, through_block=early.created_block - 1, through_hash="0x" + "11" * 32
        )
        await store.rewind(back, expected_through=coverage.through_block)
        return await original(chain_id, pool_manager, token, before_block)

    store.pools_with = racing  # type: ignore[method-assign]
    answer = await PoolIndexHistory(store).history(
        chain.network_id, chain.pool_manager, TOKEN, CREATED_BLOCK
    )
    assert answer is None


async def test_the_census_refuses_an_index_whose_tip_the_chain_no_longer_has(now) -> None:
    # Review N2: an index not yet rewound after a reorganisation is stale.
    chain, _, early = indexed_chain()
    store = MemoryPoolIndexStore()
    await indexer(chain, store).advance()
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None
    stale = indexed_chain()[0]
    stale.reorged[coverage.through_block] = "0x" + "ee" * 32
    found = await census(now, stale, store)
    assert found.pool_discovery_basis == "TOKEN_CREATION"
    assert early.id not in {pool.pool_id for pool in found.pools}


async def test_a_window_whose_last_block_changed_while_it_was_read_is_not_stored(now) -> None:
    # Review N2: logs of one branch must not be stored with another's hash.
    chain, _, _ = indexed_chain()
    store = MemoryPoolIndexStore()
    original = chain.logs
    first_end: list[int] = []

    async def reorganising(address, topics, start, end):
        result = await original(address, topics, start, end)
        if not first_end:
            first_end.append(end)
            chain.reorged[end] = "0x" + "dd" * 32
        return result

    chain.logs = reorganising  # type: ignore[method-assign]
    result = await indexer(chain, store).advance()
    assert result.status == "FAILED"
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None and coverage.through_block < first_end[0]


async def test_a_window_whose_parent_is_no_longer_the_confirmed_block_is_not_stored(now) -> None:
    # Re-review: a reorganisation between the tip check and a window.
    chain, _, _ = indexed_chain()
    store = MemoryPoolIndexStore()
    first = await indexer(chain, store, max_requests=34).advance()
    assert first.status == "PARTIAL"
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None
    original = chain.logs
    calls: list[int] = []

    async def reorganising(address, topics, start, end):
        if not calls:
            calls.append(start)
        return await original(address, topics, start, end)

    original_hash = chain.block_hash
    checked: list[int] = []

    async def moving_hash(number):
        # The tip check sees the confirmed block; any later read does not.
        if number == coverage.through_block:
            checked.append(number)
            if len(checked) > 1:
                return "0x" + "ce" * 32
        return await original_hash(number)

    chain.block_hash = moving_hash  # type: ignore[method-assign]
    chain.logs = reorganising  # type: ignore[method-assign]
    result = await indexer(chain, store).advance()
    assert result.status == "FAILED"
    assert await store.coverage(chain.network_id, chain.pool_manager) == coverage
