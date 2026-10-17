"""Issue #102: the V2 and V3 censuses against FIXTURE in-memory chains only.

Each scenario is declared through the protocol's own operations (`v2v3_chain`)
and judged end to end: census -> evidence -> `decide_v2` / `decide_v3` with a
FIXTURE registry. The production registry is empty, so the same evidence is
UNSUPPORTED in production (last tests). No RPC is reached.
"""

import pytest

from src.agents.atlas.lpcontrol.decision import (
    LP_CONTROL_V2_LP_TOKEN_CENSUS,
    LP_CONTROL_V3_POSITION_CENSUS,
    decide_v2,
    decide_v3,
)
from src.agents.atlas.lpcontrol.models import HolderState
from src.agents.atlas.lpcontrol.reads import calldata
from src.agents.atlas.lpcontrol.registry import LP_DEPLOYMENTS
from src.agents.atlas.lpcontrol.v2_census import DEFAULT_LIMITS, CensusLimits, census_v2
from src.agents.atlas.lpcontrol.v3_census import census_v3
from src.agents.atlas.v4.control import CustodyRefusal
from src.core.models import LpControlStatus
from tests.lpcontrol.v2v3_chain import (
    BURN_DEAD,
    CREATED_BLOCK,
    FIXTURE_LOCKER_CODE,
    FIXTURE_LOCKERS,
    HEAD,
    MINIMAL_PROXY_CODE,
    VAULT_CODE,
    FakeV2Chain,
    FakeV3Chain,
    v2_deployment,
    v2_registry,
    v3_deployment,
    v3_pool_code,
    v3_registry,
)
from tests.lpcontrol.v2v3_fixtures import (
    CREATOR,
    LOCKER,
    QUOTE,
    THIRD_PARTY,
    TOKEN,
    UNKNOWN_VAULT,
    V2_FACTORY,
    V2_MARKET,
    V2_PAIR,
    V3_FACTORY,
    V3_MARKET,
    V3_POOL,
)

LP = 10**18


async def v2_run(
    chain: FakeV2Chain, *, limits: CensusLimits = DEFAULT_LIMITS, created: int | None = None
):
    evidence = await census_v2(
        chain,
        deployment=v2_deployment(),
        pair=V2_PAIR,
        block=HEAD,
        creator=CREATOR,
        created_block=created,
        adapters=FIXTURE_LOCKERS,
        limits=limits,
    )
    found = decide_v2(evidence, market=V2_MARKET, registry=v2_registry())
    return evidence, (found.status, found.detail)


async def v3_run(
    chain: FakeV3Chain, *, limits: CensusLimits = DEFAULT_LIMITS, created: int | None = None
):
    evidence = await census_v3(
        chain,
        deployment=v3_deployment(),
        pool=V3_POOL,
        block=HEAD,
        creator=CREATOR,
        created_block=created,
        adapters=FIXTURE_LOCKERS,
        limits=limits,
    )
    found = decide_v3(evidence, market=V3_MARKET, registry=v3_registry())
    return evidence, (found.status, found.detail)


# ----------------------------------------------------------------------- V2


def burned_launch() -> FakeV2Chain:
    """The creator adds liquidity and sends every LP token to the dead address."""
    return FakeV2Chain().first_mint(CREATOR, 50 * LP).transfer(CREATOR, BURN_DEAD, 50 * LP)


async def test_v2_a_fully_burned_launch_passes() -> None:
    evidence, verdict = await v2_run(burned_launch())
    assert verdict == (LpControlStatus.PASS, LP_CONTROL_V2_LP_TOKEN_CENSUS)
    assert evidence.reconciled and evidence.scan_complete
    assert {row.custody.holder: row.balance_raw for row in evidence.holders} == {
        "0x" + "0" * 40: 1_000,  # MINIMUM_LIQUIDITY
        BURN_DEAD: 50 * LP,
    }
    assert {row.custody.state for row in evidence.holders} == {HolderState.BURNED}


async def test_v2_lp_sent_to_the_zero_address_counts_by_its_read_balance() -> None:
    chain = FakeV2Chain().first_mint(CREATOR, 50 * LP).transfer(CREATOR, "0x" + "0" * 40, 50 * LP)
    evidence, verdict = await v2_run(chain)
    assert verdict[0] is LpControlStatus.PASS
    assert evidence.total_supply_raw == 50 * LP + 1_000


async def test_v2_a_removal_burn_is_not_a_zero_address_balance() -> None:
    chain = burned_launch().mint(THIRD_PARTY, 5 * LP).burn(THIRD_PARTY, 5 * LP)
    evidence, verdict = await v2_run(chain)
    assert verdict[0] is LpControlStatus.PASS
    assert evidence.total_supply_raw == 50 * LP + 1_000


async def test_v2_lp_in_a_fixture_verified_locker_passes() -> None:
    chain = FakeV2Chain(codes={LOCKER: FIXTURE_LOCKER_CODE}).first_mint(CREATOR, 50 * LP)
    chain.transfer(CREATOR, LOCKER, 50 * LP)
    evidence, verdict = await v2_run(chain)
    assert verdict[0] is LpControlStatus.PASS
    states = {row.custody.holder: row.custody.state for row in evidence.holders}
    assert states[LOCKER] is HolderState.PERMANENTLY_LOCKED


async def test_v2_creator_held_lp_is_blocked() -> None:
    chain = FakeV2Chain().first_mint(CREATOR, 50 * LP)
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.BLOCKED, "V2_CREATOR_CAN_WITHDRAW")


async def test_v2_a_third_party_lp_holder_is_unknown() -> None:
    chain = burned_launch().mint(THIRD_PARTY, LP)
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_LIQUIDITY_WITHDRAWABLE")


async def test_v2_lp_left_in_the_pair_is_redeemable_by_anyone() -> None:
    chain = burned_launch().mint(THIRD_PARTY, LP).transfer(THIRD_PARTY, V2_PAIR, LP)
    evidence, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_LIQUIDITY_WITHDRAWABLE")
    states = {row.custody.holder: row.custody.state for row in evidence.holders}
    assert states[V2_PAIR] is HolderState.POOL_REDEEMABLE


@pytest.mark.parametrize(
    ("code", "refusal"),
    [
        (VAULT_CODE, CustodyRefusal.CODE_NOT_RECOGNISED),
        (MINIMAL_PROXY_CODE, CustodyRefusal.PROXY_INDIRECTION),
    ],
)
async def test_v2_an_unrecognised_contract_holder_is_unknown(code, refusal) -> None:
    chain = FakeV2Chain(codes={UNKNOWN_VAULT: code}).first_mint(CREATOR, 50 * LP)
    chain.transfer(CREATOR, UNKNOWN_VAULT, 50 * LP)
    evidence, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_LP_CUSTODY_UNRESOLVED")
    row = next(row for row in evidence.holders if row.custody.holder == UNKNOWN_VAULT)
    assert row.custody.refusal is refusal


async def test_v2_a_dropped_transfer_log_is_unknown() -> None:
    chain = burned_launch().mint(THIRD_PARTY, LP).transfer(THIRD_PARTY, BURN_DEAD, LP)
    pair_logs = [log for address, log in chain.logs_list if address == V2_PAIR]
    chain.dropped_logs.add((V2_PAIR, pair_logs[-1].log_index))
    _, verdict = await v2_run(chain)
    assert verdict[0] is LpControlStatus.UNKNOWN


async def test_v2_a_dropped_mint_log_cannot_hide_a_holder() -> None:
    # The holder's only incoming Transfer is missing: it is no candidate, and
    # the read balances no longer sum to totalSupply.
    chain = burned_launch().mint(THIRD_PARTY, LP)
    pair_logs = [log for address, log in chain.logs_list if address == V2_PAIR]
    chain.dropped_logs.add((V2_PAIR, pair_logs[-1].log_index))
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_HOLDER_SET_UNRECONCILED")


async def test_v2_a_fee_on_transfer_lp_token_is_unknown() -> None:
    chain = burned_launch()
    data = calldata("balanceOf(address)", "0" * 24 + BURN_DEAD[2:])
    chain.overrides[(V2_PAIR, data)] = "0x" + format(49 * LP, "064x")
    evidence, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_TRANSFER_RECONSTRUCTION_MISMATCH")
    assert not evidence.scan_complete


async def test_v2_a_supply_above_the_holder_set_is_unknown() -> None:
    chain = burned_launch()
    chain.supply += 1
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_HOLDER_SET_UNRECONCILED")


async def test_v2_a_pair_the_factory_does_not_name_is_unverified() -> None:
    chain = burned_launch()
    get_pair = calldata("getPair(address,address)", "0" * 24 + TOKEN[2:], "0" * 24 + QUOTE[2:])
    chain.overrides[(V2_FACTORY, get_pair)] = "0x" + "0" * 64
    evidence, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_FACTORY_BINDING_UNVERIFIED")
    assert evidence.holders == ()


async def test_v2_non_standard_pair_code_is_unverified() -> None:
    chain = burned_launch()
    chain.codes[V2_PAIR] = "0x60806040" + "99" * 40
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_FACTORY_BINDING_UNVERIFIED")


async def test_v2_another_chain_is_unknown() -> None:
    chain = burned_launch()
    chain.chain_id_value += 1
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_CHAIN_MISMATCH")


async def test_v2_a_failed_read_is_unknown() -> None:
    chain = burned_launch()
    chain.overrides[(V2_PAIR, calldata("totalSupply()"))] = TimeoutError()
    _, verdict = await v2_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_READ_FAILED")


async def test_v2_an_exhausted_budget_is_unknown_not_truncated() -> None:
    _, verdict = await v2_run(burned_launch(), limits=CensusLimits(max_requests=8))
    assert verdict == (LpControlStatus.UNKNOWN, "V2_READ_BUDGET_EXHAUSTED")


async def test_v2_too_many_holders_is_unknown() -> None:
    chain = burned_launch()
    for index in range(4):
        chain.mint("0x" + format(index + 1, "040x"), 1)
    _, verdict = await v2_run(chain, limits=CensusLimits(max_holders=3))
    assert verdict == (LpControlStatus.UNKNOWN, "V2_HOLDER_LIMIT_EXCEEDED")


async def test_v2_scans_are_chunked_to_the_log_span() -> None:
    chain = burned_launch()
    _, verdict = await v2_run(chain, limits=CensusLimits(max_log_span=5_000))
    assert verdict[0] is LpControlStatus.PASS
    # PairCreated over 49 901 blocks and Transfers over 49 001, 5 000 per request.
    assert chain.requests >= 10 + 10


async def test_v2_a_creation_hint_costs_one_request_and_a_wrong_one_is_a_gap() -> None:
    hinted = burned_launch()
    evidence, verdict = await v2_run(hinted, created=CREATED_BLOCK)
    assert verdict[0] is LpControlStatus.PASS
    assert evidence.pair_created_block == CREATED_BLOCK
    full = burned_launch()
    await v2_run(full, limits=CensusLimits(max_log_span=5_000))
    assert hinted.requests < full.requests
    _, verdict = await v2_run(burned_launch(), created=CREATED_BLOCK + 1)
    assert verdict == (LpControlStatus.UNKNOWN, "V2_PAIR_CREATION_UNKNOWN")


# ----------------------------------------------------------------------- V3


def locked_pool() -> FakeV3Chain:
    return FakeV3Chain(codes={LOCKER: FIXTURE_LOCKER_CODE}).nft(1, LOCKER, 5_000)


async def test_v3_every_nft_in_a_fixture_verified_locker_passes() -> None:
    evidence, verdict = await v3_run(locked_pool().nft(2, LOCKER, 7_000, lower=-60, upper=60))
    assert verdict == (LpControlStatus.PASS, LP_CONTROL_V3_POSITION_CENSUS)
    assert evidence.position_manager_reconciled and evidence.active_liquidity_reconciled
    assert evidence.active_liquidity == 12_000


async def test_v3_a_creation_hint_passes_and_a_wrong_one_is_a_gap() -> None:
    _, verdict = await v3_run(locked_pool(), created=CREATED_BLOCK)
    assert verdict[0] is LpControlStatus.PASS
    _, verdict = await v3_run(locked_pool(), created=CREATED_BLOCK - 1)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_POOL_CREATION_UNKNOWN")


async def test_v3_an_nft_held_by_the_dead_address_is_restricted() -> None:
    _, verdict = await v3_run(locked_pool().nft(2, BURN_DEAD, 100))
    assert verdict[0] is LpControlStatus.PASS


async def test_v3_a_burned_emptied_nft_is_skipped() -> None:
    chain = locked_pool().nft(2, CREATOR, 900).decrease_and_burn(2)
    _, verdict = await v3_run(chain)
    assert verdict[0] is LpControlStatus.PASS


async def test_v3_another_pools_nft_in_the_same_block_is_ignored() -> None:
    _, verdict = await v3_run(locked_pool().other_pool_nft(9, CREATOR))
    assert verdict[0] is LpControlStatus.PASS


async def test_v3_a_creator_owned_nft_is_blocked() -> None:
    _, verdict = await v3_run(locked_pool().nft(2, CREATOR, 10))
    assert verdict == (LpControlStatus.BLOCKED, "V3_CREATOR_CAN_WITHDRAW")


async def test_v3_a_creator_direct_position_is_blocked() -> None:
    _, verdict = await v3_run(locked_pool().direct(CREATOR, 10))
    assert verdict == (LpControlStatus.BLOCKED, "V3_CREATOR_CAN_WITHDRAW")


async def test_v3_a_third_party_nft_is_unknown() -> None:
    _, verdict = await v3_run(locked_pool().nft(2, THIRD_PARTY, 10))
    assert verdict == (LpControlStatus.UNKNOWN, "V3_LIQUIDITY_WITHDRAWABLE")


async def test_v3_an_out_of_range_creator_position_still_blocks() -> None:
    # Out of range today, withdrawable all the same.
    _, verdict = await v3_run(locked_pool().nft(2, CREATOR, 10, lower=600, upper=1200))
    assert verdict == (LpControlStatus.BLOCKED, "V3_CREATOR_CAN_WITHDRAW")


async def test_v3_an_nft_in_an_unrecognised_vault_is_unknown() -> None:
    chain = FakeV3Chain(codes={UNKNOWN_VAULT: VAULT_CODE}).nft(1, UNKNOWN_VAULT, 10)
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_LP_CUSTODY_UNRESOLVED")


async def test_v3_a_missing_increase_liquidity_log_is_unreconciled() -> None:
    chain = locked_pool().nft(2, CREATOR, 10)
    npm_logs = [log for address, log in chain.logs_list if address == chain.npm]
    chain.dropped_logs.add((chain.npm, npm_logs[-1].log_index))
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_POSITION_SET_UNRECONCILED")


async def test_v3_a_missing_mint_log_is_unreconciled() -> None:
    chain = locked_pool().direct(CREATOR, 10)
    pool_logs = [log for address, log in chain.logs_list if address == V3_POOL]
    chain.dropped_logs.add((V3_POOL, pool_logs[-1].log_index))
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_POSITION_SET_UNRECONCILED")


async def test_v3_active_liquidity_beyond_the_positions_is_unreconciled() -> None:
    chain = locked_pool()
    chain.liquidity_override = 5_001
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_POSITION_SET_UNRECONCILED")


async def test_v3_a_pool_whose_immutables_name_another_factory_is_unverified() -> None:
    chain = locked_pool()
    chain.codes[V3_POOL] = v3_pool_code("0x" + "99" * 20, TOKEN, QUOTE, 3000)
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_FACTORY_BINDING_UNVERIFIED")


async def test_v3_a_position_manager_of_another_factory_is_unverified() -> None:
    chain = locked_pool()
    chain.overrides[(chain.npm, calldata("factory()"))] = "0x" + "0" * 24 + "99" * 20
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_FACTORY_BINDING_UNVERIFIED")


async def test_v3_a_failed_owner_read_is_unknown() -> None:
    chain = locked_pool()
    chain.overrides[(chain.npm, calldata("ownerOf(uint256)", format(1, "064x")))] = OSError()
    _, verdict = await v3_run(chain)
    assert verdict == (LpControlStatus.UNKNOWN, "V3_READ_FAILED")


async def test_v3_an_exhausted_budget_is_unknown() -> None:
    _, verdict = await v3_run(locked_pool(), limits=CensusLimits(max_requests=10))
    assert verdict == (LpControlStatus.UNKNOWN, "V3_READ_BUDGET_EXHAUSTED")


# ------------------------------------------------------------- production


async def test_perfect_fixture_evidence_is_unsupported_in_production() -> None:
    v2 = await census_v2(
        burned_launch(),
        deployment=v2_deployment(),
        pair=V2_PAIR,
        block=HEAD,
        creator=CREATOR,
    )
    v3 = await census_v3(
        locked_pool(),
        deployment=v3_deployment(),
        pool=V3_POOL,
        block=HEAD,
        creator=CREATOR,
        adapters=FIXTURE_LOCKERS,
    )
    assert decide_v2(v2, market=V2_MARKET, registry=LP_DEPLOYMENTS).status is (
        LpControlStatus.UNSUPPORTED
    )
    assert decide_v3(v3, market=V3_MARKET, registry=LP_DEPLOYMENTS).status is (
        LpControlStatus.UNSUPPORTED
    )
    assert V3_FACTORY != V2_FACTORY
