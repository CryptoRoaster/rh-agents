"""Issue #96: the census reconciles the PoolManager balance against its liabilities.

Principal, fees owed, protocol fees and ERC-6909 claims are read at the census
block; whatever the PoolManager holds beyond them is owed to nobody. The
reconciliation is a separate, optional stage: when it cannot be completed --
budget, an unreadable or inconsistent liability, a deficit -- it says so, and
the census and its economic figures stay exactly as they were.
"""

from src.agents.atlas.v4.census import CensusBounds, ChainLog
from src.agents.atlas.v4.math import fee_growth_inside, fees_owed
from src.agents.atlas.v4.protocol import CLAIM_TRANSFER_TOPIC
from src.markets.models import Availability
from src.runtime.models import ErrorCode
from tests.atlas.conftest import TOKEN, chain_snapshot
from tests.atlas.v4.chain import CREATED_BLOCK, HEAD_BLOCK, NATIVE, POSITION_MANAGER, token_salt
from tests.atlas.v4.scenarios import HIDDEN_RANGE, UNIT, census_for, revenue_like

HOLDER = "0x" + "c1" * 20
MAX = (1 << 256) - 1


async def run(now, chain, *, bounds=None):
    return await census_for(chain, bounds=bounds).census(
        chain_snapshot(now, block=HEAD_BLOCK), TOKEN, CREATED_BLOCK
    )


def with_fees():
    """Both pools traded: in range, below range, and an accumulator that wrapped."""
    chain, traded, hidden = revenue_like()
    side = chain.side(traded)
    pair = lambda value: (value, 0) if side == 0 else (0, value)  # noqa: E731
    chain.fee_growth_global[traded.id] = pair(5 << 125)
    chain.fee_growth_outside[(traded.id, -160_100)] = pair(1 << 125)
    chain.fee_growth_outside[(traded.id, 198_050)] = pair(1 << 124)
    traded_key = (traded.id, POSITION_MANAGER, -160_100, 198_050, token_salt(3_498_758))
    chain.fee_growth_inside_last[traded_key] = pair(1 << 120)
    # The hidden pool's price is above the position: inside = upper - lower,
    # and the last value lies above it, so the subtraction wraps.
    hidden_side = chain.side(hidden)
    hpair = lambda value: (value, 0) if hidden_side == 0 else (0, value)  # noqa: E731
    chain.fee_growth_outside[(hidden.id, HIDDEN_RANGE[0])] = hpair(3 << 100)
    chain.fee_growth_outside[(hidden.id, HIDDEN_RANGE[1])] = hpair(7 << 100)
    hidden_key = (hidden.id, POSITION_MANAGER, *HIDDEN_RANGE, token_salt(3_498_775))
    chain.fee_growth_inside_last[hidden_key] = hpair(MAX - (1 << 90) + 1)
    chain.protocol_fees = 11 * UNIT
    chain.claim_transfers += [
        (NATIVE, HOLDER, 4 * UNIT, CREATED_BLOCK + 400),
        (HOLDER, NATIVE, UNIT, CREATED_BLOCK + 500),
    ]
    return chain, traded, hidden


async def test_every_liability_is_read_and_the_rest_is_owed_to_nobody(now) -> None:
    chain, _, _ = with_fees()
    census = await run(now, chain)

    assert census.status is Availability.AVAILABLE
    owed = chain.owed(HEAD_BLOCK)
    assert all(value > 0 for value in owed.values())
    by_key = {
        (item.pool_id, item.owner_key, item.tick_lower, item.tick_upper, item.salt): item
        for item in census.positions
    }
    assert {key: by_key[key].uncollected_token_raw for key in owed} == owed
    found = census.reconciliation
    assert found is not None and found.status == "COMPLETE" and found.gap is None
    assert found.principal_raw == sum(chain.controlled(HEAD_BLOCK).values())
    assert found.uncollected_fees_raw == sum(owed.values())
    assert found.protocol_fees_raw == 11 * UNIT
    assert found.claims_raw == 3 * UNIT
    assert found.surplus_raw == 12_345 * UNIT
    assert census.pool_manager_balance_raw == (
        found.principal_raw
        + found.uncollected_fees_raw
        + found.protocol_fees_raw
        + found.claims_raw
        + found.surplus_raw
    )


async def test_the_claim_scan_covers_the_census_range_at_its_block(now) -> None:
    chain, _, _ = with_fees()
    await run(now, chain)
    windows = [
        (start, end)
        for _, topics, start, end in chain.log_requests
        if topics[0] == CLAIM_TRANSFER_TOPIC
    ]
    assert windows and windows[0][0] == CREATED_BLOCK and windows[-1][1] == HEAD_BLOCK


async def test_a_budget_that_ends_inside_the_reconciliation_leaves_the_census(now) -> None:
    chain, _, _ = with_fees()
    full = await run(now, chain)
    assert full.reconciliation is not None
    spent = full.reconciliation.requests_made
    assert spent > 0
    short = await run(now, with_fees()[0], bounds=CensusBounds(max_requests=full.requests_made - 1))
    assert short.status is Availability.AVAILABLE
    # Nothing partial is kept: no position carries a fee figure.
    assert all(item.uncollected_token_raw is None for item in short.positions)
    assert short.reconciliation is not None
    assert (short.reconciliation.status, short.reconciliation.gap) == (
        "INCOMPLETE",
        "V4_RECONCILIATION_BOUNDS_EXCEEDED",
    )
    assert short.reconciliation.surplus_raw is None


async def test_an_unreadable_protocol_fee_is_incomplete(now) -> None:
    chain, _, _ = with_fees()
    chain.protocol_fees = None
    census = await run(now, chain)
    assert census.status is Availability.AVAILABLE
    assert census.reconciliation is not None
    assert (census.reconciliation.status, census.reconciliation.gap) == (
        "INCOMPLETE",
        "V4_RECONCILIATION_UNAVAILABLE",
    )


async def test_more_claims_burned_than_minted_is_incomplete(now) -> None:
    chain, _, _ = with_fees()
    chain.claim_transfers.append((HOLDER, NATIVE, 9 * UNIT, CREATED_BLOCK + 600))
    census = await run(now, chain)
    assert census.reconciliation is not None
    assert (census.reconciliation.status, census.reconciliation.gap) == (
        "INCOMPLETE",
        "V4_CLAIMS_INCONSISTENT",
    )


async def test_a_malformed_claim_log_is_incomplete(now) -> None:
    chain, _, _ = with_fees()

    def tamper(log: ChainLog) -> ChainLog:
        if log.topics[0] != CLAIM_TRANSFER_TOPIC:
            return log
        return ChainLog(block_number=log.block_number, topics=log.topics, data="0x00")

    chain.tamper_log = tamper
    census = await run(now, chain)
    assert census.status is Availability.AVAILABLE
    assert census.reconciliation is not None
    assert census.reconciliation.gap == "V4_RECONCILIATION_UNAVAILABLE"


async def test_a_balance_below_the_liabilities_is_a_deficit(now) -> None:
    chain, _, _ = with_fees()
    chain.extra_pool_balance = -1
    census = await run(now, chain)
    assert census.reconciliation is not None
    assert (census.reconciliation.status, census.reconciliation.gap) == (
        "INCOMPLETE",
        "V4_POOL_BALANCE_DEFICIT",
    )
    assert census.reconciliation.surplus_raw is None


async def test_a_transport_failure_inside_the_reconciliation_leaves_the_census(now) -> None:
    chain, _, _ = with_fees()
    full = await run(now, chain)
    assert full.reconciliation is not None
    chain = with_fees()[0]
    # The last read of the census proper is its balance; fail the one after.
    chain.fail_at = full.requests_made - full.reconciliation.requests_made + 1
    chain.fail_with = ErrorCode.RATE_LIMITED
    census = await run(now, chain)
    assert census.status is Availability.AVAILABLE
    assert census.reconciliation is not None
    assert census.reconciliation.gap == "V4_RECONCILIATION_UNAVAILABLE"


def test_fee_growth_inside_follows_the_protocol_branches() -> None:
    assert fee_growth_inside(-1, 0, 10, 100, 30, 20) == 10  # below: lower - upper
    assert fee_growth_inside(10, 0, 10, 100, 30, 20) == (20 - 30) & MAX  # at upper: above
    assert fee_growth_inside(5, 0, 10, 100, 30, 20) == 50  # inside: global - lower - upper
    assert fee_growth_inside(0, 0, 10, 100, 30, 20) == 50  # at lower: inside
    assert fee_growth_inside(5, 0, 10, 10, 30, 20) == (10 - 50) & MAX  # wraps


def test_fees_owed_wrap_and_floor_like_the_protocol() -> None:
    q128 = 1 << 128
    assert fees_owed(3 * q128, q128, 5) == 10
    assert fees_owed(q128 + 1, q128, q128 - 1) == 0  # floored
    # (5 - (2**256 - 5)) mod 2**256 == 10
    assert fees_owed(5, MAX - 4, q128 - 1) == 10 * (q128 - 1) >> 128
