"""The V4 pool census against a deterministic fixture chain.

Pools are found from Initialize events alone, positions are rebuilt from
ModifyLiquidity events and checked against PoolManager storage, and every
failure, bound or inconsistency is an unavailable census with a stable reason
— never a short list reported as complete. No RPC is involved.
"""

import pytest

from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.v4.census import CensusBounds, ChainLog, PoolControlChainRefused
from src.agents.atlas.v4.models import (
    HookOwnerStatus,
    PoolControlGap,
    PositionKind,
    PositionOwnerStatus,
)
from src.markets.models import Availability
from src.runtime.models import ErrorCode
from tests.atlas.conftest import CREATOR, TOKEN, chain_snapshot
from tests.atlas.v4.chain import (
    CREATED_BLOCK,
    HEAD_BLOCK,
    NATIVE,
    POSITION_MANAGER,
    SECOND_POSITION_MANAGER,
    FakeV4Chain,
    Pool,
    token_salt,
)
from tests.atlas.v4.scenarios import (
    CREATOR_HOOK,
    FULL_RANGE,
    LOCKER,
    MEME,
    UNIT,
    census_for,
    revenue_like,
    traded_pool,
)


async def run(now, chain: FakeV4Chain, *, from_block=CREATED_BLOCK, bounds=None, block=HEAD_BLOCK):
    return await census_for(chain, bounds=bounds).census(
        chain_snapshot(now, block=block), TOKEN, from_block
    )


async def test_both_pools_are_found_including_the_one_never_traded(now) -> None:
    chain, traded, hidden = revenue_like()
    census = await run(now, chain)

    assert census.status is Availability.AVAILABLE
    assert [pool.pool_id for pool in census.pools] == [traded.id, hidden.id]
    found = {pool.pool_id: pool for pool in census.pools}
    assert found[hidden.id].fee == 100 and found[traded.id].fee == 2500
    assert found[hidden.id].created_block == hidden.created_block
    assert found[hidden.id].created_at is not None
    assert found[hidden.id].initialized is True
    hook = found[hidden.id].hook_facts
    assert hook is not None and hook.before_swap
    assert hook.owner == CREATOR and hook.owner_status is HookOwnerStatus.OWNER_READ
    assert found[traded.id].hook_facts is None
    assert census.pool_manager_balance_raw == chain.pool_balance(HEAD_BLOCK)
    assert census.scan_from_block == CREATED_BLOCK and census.scan_to_block == HEAD_BLOCK


async def test_positions_carry_owner_range_and_exact_amount(now) -> None:
    chain, _, hidden = revenue_like()
    census = await run(now, chain)

    creator = next(item for item in census.positions if item.pool_id == hidden.id)
    assert creator.kind is PositionKind.POSITION_MANAGER
    assert creator.position_manager == POSITION_MANAGER
    assert creator.token_id == 3_498_775
    assert creator.owner == CREATOR
    assert (creator.tick_lower, creator.tick_upper) == (-887272, 299351)
    assert creator.controlled_token_raw // UNIT == 573_999_999


async def test_the_scan_is_chunked_and_bounded(now) -> None:
    chain, _, _ = revenue_like()
    bounds = CensusBounds(chunk_blocks=5_000)
    census = await run(now, chain, bounds=bounds)
    assert census.status is Availability.AVAILABLE
    initialize = [item for item in chain.log_requests if len(item[1]) >= 3 and item[1][1] is None]
    assert all(end - start + 1 <= 5_000 for _, _, start, end in chain.log_requests)
    assert min(start for _, _, start, _ in initialize) == CREATED_BLOCK
    assert max(end for _, _, _, end in initialize) == HEAD_BLOCK
    assert census.requests_made == chain.requests


async def test_history_longer_than_the_request_budget_is_incomplete_not_truncated(now) -> None:
    chain, _, _ = revenue_like()
    census = await run(now, chain, bounds=CensusBounds(chunk_blocks=100, max_requests=40))
    assert census.status is Availability.UNAVAILABLE
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED
    assert census.failure is AtlasSourceFailure.INCOMPLETE_RESULT
    assert census.pools == ()


async def test_more_pools_than_the_bound_is_incomplete(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    for fee in range(1, 5):
        chain.add_pool(traded_pool(fee=fee))
    census = await run(now, chain, bounds=CensusBounds(max_pools=3))
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED


async def test_more_positions_than_the_bound_is_incomplete(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool())
    for token_id in range(1, 5):
        chain.mint(pool, "0x" + f"{token_id:02x}" * 20, token_id, *FULL_RANGE, 10**20)
    census = await run(now, chain, bounds=CensusBounds(max_positions=3))
    assert census.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE
    assert census.failure is AtlasSourceFailure.INCOMPLETE_RESULT


@pytest.mark.parametrize("fail_at", [1, 3, 6, 9, 14, 20, 26, 27])
async def test_an_rpc_timeout_anywhere_is_unknown_never_no_more_pools(now, fail_at) -> None:
    chain, _, _ = revenue_like()
    chain.fail_at = fail_at
    census = await run(now, chain)
    assert census.status is Availability.UNAVAILABLE
    assert census.failure is AtlasSourceFailure.TIMEOUT
    assert census.gap in {
        PoolControlGap.CENSUS_UNAVAILABLE,
        PoolControlGap.POSITION_FACTS_INCOMPLETE,
    }
    assert census.pools == () and census.positions == ()


async def test_a_timeout_while_reading_positions_names_the_position_phase(now) -> None:
    chain, _, _ = revenue_like()
    chain.fail_at = 20  # past the pool reads, inside the position reads
    census = await run(now, chain)
    assert census.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE


async def test_a_wall_clock_bound_fails_closed(now) -> None:
    import asyncio

    chain, _, _ = revenue_like()

    async def slow(*args):
        await asyncio.sleep(1)
        return 4663

    chain.chain_id = slow  # type: ignore[method-assign]
    census = await run(now, chain, bounds=CensusBounds(timeout_seconds=0.01))
    assert census.gap is PoolControlGap.CENSUS_UNAVAILABLE
    assert census.failure is AtlasSourceFailure.TIMEOUT


async def test_a_pool_id_that_does_not_hash_from_its_key_is_refused(now) -> None:
    chain, _, _ = revenue_like()

    def forge(log: ChainLog) -> ChainLog:
        return ChainLog(
            log.block_number, (log.topics[0], "0x" + "99" * 32, *log.topics[2:]), log.data
        )

    chain.tamper_log = forge
    census = await run(now, chain)
    assert census.gap is PoolControlGap.POOL_KEY_MISMATCH
    assert census.failure is AtlasSourceFailure.INVALID_RESPONSE


async def test_a_wrong_chain_is_a_hard_refusal(now) -> None:
    chain, _, _ = revenue_like()
    chain.network_id = 56
    with pytest.raises(PoolControlChainRefused):
        await run(now, chain)
    with pytest.raises(PoolControlChainRefused):
        await census_for(revenue_like()[0]).census(
            chain_snapshot(now, chain="bsc", chain_id=56), TOKEN, CREATED_BLOCK
        )


async def test_an_rpc_chain_mismatch_is_a_hard_refusal(now) -> None:
    chain, _, _ = revenue_like()
    chain.fail_at = 4
    chain.fail_with = ErrorCode.CHAIN_ID_MISMATCH
    with pytest.raises(PoolControlChainRefused):
        await run(now, chain)


async def test_an_unknown_creation_block_is_never_guessed(now) -> None:
    chain, _, _ = revenue_like()
    census = await run(now, chain, from_block=None)
    assert census.gap is PoolControlGap.CREATION_BLOCK_UNKNOWN
    assert chain.requests == 0


@pytest.mark.parametrize("claimed", [CREATED_BLOCK - 50, CREATED_BLOCK + 50])
async def test_a_creation_block_the_chain_contradicts_is_unknown(now, claimed) -> None:
    chain, _, _ = revenue_like()
    census = await run(now, chain, from_block=claimed)
    assert census.gap is PoolControlGap.CREATION_BLOCK_UNKNOWN


async def test_an_unconfigured_chain_is_not_configured(now) -> None:
    from src.agents.atlas.v4.census import V4PoolCensus

    census = await V4PoolCensus(reads=revenue_like()[0], chain="robinhood", deployments={}).census(
        chain_snapshot(now), TOKEN, CREATED_BLOCK
    )
    assert census.gap is PoolControlGap.DEPLOYMENT_NOT_CONFIGURED
    assert census.failure is AtlasSourceFailure.NOT_CONFIGURED


async def test_a_position_manager_bound_elsewhere_is_never_trusted(now) -> None:
    chain, _, _ = revenue_like()
    chain.manager_binding[POSITION_MANAGER] = "0x" + "66" * 20
    census = await run(now, chain)
    assert census.gap is PoolControlGap.DEPLOYMENT_UNVERIFIED


async def test_a_reconstruction_the_pool_manager_disagrees_with_is_refused(now) -> None:
    chain, _, _ = revenue_like()
    chain.tamper_liquidity = lambda liquidity: liquidity - 1
    census = await run(now, chain)
    assert census.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE
    assert census.failure is AtlasSourceFailure.INVALID_RESPONSE


async def test_a_hook_without_owner_is_owner_unknown_not_no_owner(now) -> None:
    chain, _, hidden = revenue_like()
    del chain.hook_owners[CREATOR_HOOK]
    census = await run(now, chain)
    hook = next(pool for pool in census.pools if pool.pool_id == hidden.id).hook_facts
    assert hook is not None
    assert hook.owner is None and hook.owner_status is HookOwnerStatus.OWNER_UNKNOWN


async def test_a_hook_address_without_code_is_recorded(now) -> None:
    chain, _, hidden = revenue_like()
    chain.contracts.discard(CREATOR_HOOK)
    census = await run(now, chain)
    hook = next(pool for pool in census.pools if pool.pool_id == hidden.id).hook_facts
    assert hook is not None and hook.code_present is False
    assert hook.owner_status is HookOwnerStatus.OWNER_UNKNOWN


async def test_a_transferred_nft_counts_for_its_current_owner(now) -> None:
    chain, _, hidden = revenue_like()
    buyer = "0x" + "b9" * 20
    chain.nft_owners[(POSITION_MANAGER, 3_498_775)] = buyer
    census = await run(now, chain)
    position = next(item for item in census.positions if item.pool_id == hidden.id)
    assert position.owner == buyer


async def test_a_fully_removed_position_leaves_nothing_behind(now) -> None:
    chain, _, hidden = revenue_like()
    chain.modify(
        hidden,
        POSITION_MANAGER,
        -887272,
        299351,
        -181514997636243302190,
        token_salt(3_498_775),
        block=77_200_000,
    )
    census = await run(now, chain)
    assert all(item.pool_id != hidden.id for item in census.positions)
    assert census.pool_manager_balance_raw == chain.pool_balance(HEAD_BLOCK)
    assert census.pool_manager_balance_raw < 200_000_000 * UNIT


async def test_a_partly_removed_position_counts_what_remains(now) -> None:
    chain, _, hidden = revenue_like()
    chain.modify(
        hidden,
        POSITION_MANAGER,
        -887272,
        299351,
        -181514997636243302190 // 2,
        token_salt(3_498_775),
        block=77_190_000,
    )
    census = await run(now, chain)
    position = next(item for item in census.positions if item.pool_id == hidden.id)
    assert position.liquidity == 181514997636243302190 - 181514997636243302190 // 2
    assert 286_999_000 * UNIT < position.controlled_token_raw < 287_001_000 * UNIT


async def test_a_meme_meme_pair_is_found_with_the_token_as_currency0(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool(currency0=TOKEN, currency1=MEME, tick=-5_000))
    chain.mint(pool, LOCKER, 1, *FULL_RANGE, 10**24)
    census = await run(now, chain)
    assert [item.pool_id for item in census.pools] == [pool.id]
    position = census.positions[0]
    expected = chain.controlled(HEAD_BLOCK)
    assert position.controlled_token_raw == next(iter(expected.values())) > 0


async def test_native_and_token_in_either_orientation(now) -> None:
    """Native is the zero address and sorts first; a higher quote sorts last."""
    chain = FakeV4Chain(token=TOKEN)
    native = chain.add_pool(traded_pool())
    quoted = chain.add_pool(traded_pool(currency0=TOKEN, currency1=MEME, fee=3000, tick=0))
    chain.mint(native, LOCKER, 1, -1000, 1000, 10**22)
    chain.mint(quoted, LOCKER, 2, -1000, 1000, 10**22)
    census = await run(now, chain)
    assert {item.pool_id for item in census.pools} == {native.id, quoted.id}
    by_pool = {item.pool_id: item for item in census.positions}
    # Token1 of the native pool at tick 190000: entirely the token, above range.
    assert by_pool[native.id].controlled_token_raw > 0
    assert by_pool[quoted.id].controlled_token_raw > 0
    pools = {item.pool_id: item for item in census.pools}
    assert (pools[native.id].currency0, pools[native.id].currency1) == (NATIVE, TOKEN)
    assert (pools[quoted.id].currency0, pools[quoted.id].currency1) == (TOKEN, MEME)


async def test_several_position_managers_each_verified(now) -> None:
    chain = FakeV4Chain(token=TOKEN, position_managers=(POSITION_MANAGER, SECOND_POSITION_MANAGER))
    pool = chain.add_pool(traded_pool())
    chain.mint(pool, LOCKER, 7, *FULL_RANGE, 10**21)
    chain.mint(pool, CREATOR, 7, *FULL_RANGE, 10**21, manager=SECOND_POSITION_MANAGER)
    census = await run(now, chain)
    owners = {(item.position_manager, item.owner) for item in census.positions}
    assert owners == {(POSITION_MANAGER, LOCKER), (SECOND_POSITION_MANAGER, CREATOR)}


async def test_a_direct_position_of_an_account_is_attributed(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool())
    account = "0x" + "4a" * 20
    chain.delegated.add(account)
    chain.modify(pool, account, *FULL_RANGE, 10**21, "0x" + "00" * 32)
    census = await run(now, chain)
    position = census.positions[0]
    assert position.kind is PositionKind.DIRECT
    assert position.owner == account and position.owner_key == account
    assert position.owner_status is PositionOwnerStatus.ATTRIBUTED


async def test_a_direct_position_of_a_contract_has_an_unknown_owner(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool())
    vault = "0x" + "5b" * 20
    chain.contracts.add(vault)
    chain.modify(pool, vault, *FULL_RANGE, 10**21, "0x" + "00" * 32)
    census = await run(now, chain)
    position = census.positions[0]
    assert position.owner is None and position.owner_key == vault
    assert position.owner_status is PositionOwnerStatus.OWNER_UNKNOWN


async def test_an_unreadable_nft_owner_is_unknown(now) -> None:
    chain, traded, _ = revenue_like()
    del chain.nft_owners[(POSITION_MANAGER, 3_498_758)]
    census = await run(now, chain)
    position = next(item for item in census.positions if item.pool_id == traded.id)
    assert position.owner_status is PositionOwnerStatus.OWNER_UNKNOWN


async def test_a_token_id_the_manager_binds_elsewhere_is_refused(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool())
    # A manager-sender change whose token id the manager never issued for it.
    chain.modify(pool, POSITION_MANAGER, *FULL_RANGE, 10**21, token_salt(9))
    chain.changes[-1] = chain.changes[-1].__class__(
        pool, POSITION_MANAGER, *FULL_RANGE, 10**21, token_salt(9), pool.created_block + 2
    )
    chain.nft_owners[(POSITION_MANAGER, 9)] = LOCKER
    real_info = chain._position_info
    chain._position_info = lambda manager, token_id: "0x" + "00" * 192  # type: ignore[method-assign]
    census = await run(now, chain)
    chain._position_info = real_info  # type: ignore[method-assign]
    assert census.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE


async def test_an_uninitialized_pool_is_recorded_as_such(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    chain.add_pool(Pool(NATIVE, TOKEN, 500, 10, initialized=False))
    census = await run(now, chain)
    assert census.pools[0].initialized is False and census.pools[0].sqrt_price_x96 is None
