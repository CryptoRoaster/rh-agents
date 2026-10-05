"""Adaptive log windows: a long history fits the unchanged budget, a narrow provider still works.

The census starts every filtered log stream at the largest window the RPC
client allows and halves the same unanswered window only for failures a
smaller window can fix. Every attempt, failed or not, is one budgeted read;
answered windows tile the range exactly; nothing is ever read as "no logs".
"""

import pytest

from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.v4.census import MIN_LOG_SPAN, CensusBounds
from src.agents.atlas.v4.control import PositionControlState
from src.agents.atlas.v4.models import PoolControlGap
from src.agents.atlas.v4.protocol import INITIALIZE_TOPIC, MODIFY_LIQUIDITY_TOPIC
from src.markets.models import Availability
from src.runtime.models import ErrorCode
from tests.atlas.conftest import TOKEN, chain_snapshot
from tests.atlas.v4.chain import CREATED_BLOCK, HEAD_BLOCK, NATIVE, FakeV4Chain, Pool
from tests.atlas.v4.scenarios import (
    MEME,
    census_for,
    liquidity_for,
    revenue_like,
    traded_pool,
)

# The live counter-check: a Robinhood V4 token 2 663 540 blocks old.
LIVE_AGE = 2_663_540
OLD_CREATED = 78_106_150
OLD_HEAD = OLD_CREATED + LIVE_AGE


async def run(now, chain: FakeV4Chain, *, from_block=CREATED_BLOCK, block=HEAD_BLOCK, bounds=None):
    return await census_for(chain, bounds=bounds).census(
        chain_snapshot(now, block=block), TOKEN, from_block
    )


def old_token(age: int = LIVE_AGE) -> tuple[FakeV4Chain, Pool]:
    """One pool, one position, a sparse history of ``age`` blocks (default: 2.66 million)."""
    chain = FakeV4Chain(token=TOKEN, created_block=OLD_CREATED, head=OLD_CREATED + age)
    pool = chain.add_pool(traded_pool(created_block=OLD_CREATED + 13))
    owner = "0x" + "09" * 20
    chain.mint(
        pool,
        owner,
        3_536_651,
        -160_100,
        198_050,
        liquidity_for(167_000_000 * 10**18, pool.tick, -160_100),
        block=OLD_CREATED + 14,
    )
    return chain, pool


def windows_of(chain: FakeV4Chain, topics: tuple[str | None, ...]) -> list[tuple[int, int]]:
    return [(start, end) for asked, start, end in chain.answered_windows if asked == topics]


def assert_tiles(windows: list[tuple[int, int]], start: int, end: int) -> None:
    """Answered windows cover ``[start, end]`` exactly: no gap, no overlap."""
    assert windows[0][0] == start and windows[-1][1] == end
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:], strict=False):
        assert next_start == previous_end + 1


# -------------------------------------------------------------- live finding


async def test_a_token_two_point_six_million_blocks_old_is_censused(now) -> None:
    chain, pool = old_token()
    census = await run(now, chain, from_block=OLD_CREATED, block=OLD_HEAD)
    assert census.status is Availability.AVAILABLE
    assert census.gap is None
    assert [item.pool_id for item in census.pools] == [pool.id]
    (position,) = census.positions
    assert position.control.control_state is PositionControlState.DIRECT_CONTROL
    # 27 windows per stream, three streams, plus a handful of reads.
    assert census.requests_made < 120
    assert census.requests_made == chain.requests


async def test_the_old_default_could_never_have_tried(now) -> None:
    """With 10 000-block windows the same history needed 534 reads before any position."""
    chain, _ = old_token()
    census = await run(
        now, chain, from_block=OLD_CREATED, block=OLD_HEAD, bounds=CensusBounds(chunk_blocks=10_000)
    )
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED
    assert census.requests_made == 6  # refused before a single log read
    assert not chain.log_requests


# ------------------------------------------------------------ narrow provider


async def test_a_provider_capped_at_25k_blocks_is_learned_and_covered(now) -> None:
    age = 500_000
    chain, pool = old_token(age)
    chain.max_log_span = 25_000
    census = await run(now, chain, from_block=OLD_CREATED, block=OLD_CREATED + age)
    assert census.status is Availability.AVAILABLE
    assert len(census.positions) == 1

    currency0 = (INITIALIZE_TOPIC, None, "0x" + "0" * 24 + TOKEN[2:])
    attempts = [(s, e) for _, topics, s, e in chain.log_requests if topics == currency0]
    # 100k fails, 50k fails, 25k answers -- and every later window is 25k.
    assert [e - s + 1 for s, e in attempts[:3]] == [100_000, 50_000, 25_000]
    assert all(e - s + 1 <= 25_000 for s, e in attempts[3:])
    failed = [item for item in chain.log_requests if item[3] - item[2] + 1 > 25_000]
    assert len(failed) == 6  # two refused probes in each of the three streams

    for topics, start in (
        (currency0, OLD_CREATED),
        ((INITIALIZE_TOPIC, None, None, "0x" + "0" * 24 + TOKEN[2:]), OLD_CREATED),
        ((MODIFY_LIQUIDITY_TOPIC, pool.id), pool.created_block),
    ):
        assert_tiles(windows_of(chain, topics), start, OLD_CREATED + age)
    # Every refused probe was a budgeted read like any other.
    assert census.requests_made == chain.requests
    assert census.requests_made <= 240


async def test_the_same_25k_provider_cannot_cover_2_6_million_blocks_in_budget(now) -> None:
    """Three streams of 107 windows each: more than 240 reads, so it fails closed."""
    chain, _ = old_token()
    chain.max_log_span = 25_000
    census = await run(now, chain, from_block=OLD_CREATED, block=OLD_HEAD)
    assert census.status is Availability.UNAVAILABLE
    assert census.failure is AtlasSourceFailure.INCOMPLETE_RESULT
    assert census.requests_made == chain.requests == 240


@pytest.mark.parametrize("code", [ErrorCode.RPC_ERROR, ErrorCode.CONTRACT, ErrorCode.TIMEOUT])
async def test_window_failures_a_smaller_window_can_fix_are_narrowed(now, code) -> None:
    chain, _, hidden = revenue_like()
    chain.max_log_span = 4_000
    chain.span_failure = code
    census = await run(now, chain)
    assert census.status is Availability.AVAILABLE
    assert hidden.id in {pool.pool_id for pool in census.pools}
    assert census.requests_made == chain.requests


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.RATE_LIMITED,
        ErrorCode.AUTHENTICATION,
        ErrorCode.CONNECTIVITY,
        ErrorCode.UNAVAILABLE,
        ErrorCode.CLIENT,
        ErrorCode.CONFIGURATION,
    ],
)
async def test_failures_that_are_not_about_the_window_are_never_split(now, code) -> None:
    chain, _, _ = revenue_like()
    chain.max_log_span = 4_000
    chain.span_failure = code
    census = await run(now, chain)
    assert census.status is Availability.UNAVAILABLE
    assert census.pools == ()
    # One refused log read, and not a single retry after it.
    assert len(chain.log_requests) == 1


async def test_a_chain_mismatch_on_a_window_is_a_hard_refusal(now) -> None:
    from src.agents.atlas.v4.census import PoolControlChainRefused

    chain, _, _ = revenue_like()
    chain.max_log_span = 4_000
    chain.span_failure = ErrorCode.CHAIN_ID_MISMATCH
    with pytest.raises(PoolControlChainRefused):
        await run(now, chain)
    assert len(chain.log_requests) == 1


async def test_a_block_unanswerable_even_alone_fails_closed(now) -> None:
    chain, _, _ = revenue_like()
    chain.unreadable_block = CREATED_BLOCK + 500
    census = await run(now, chain)
    assert census.status is Availability.UNAVAILABLE
    assert census.failure is not None
    assert census.pools == () and census.positions == ()
    # Narrowed to a single block before giving up -- and never skipped.
    narrowest = min(end - start + 1 for _, _, start, end in chain.log_requests)
    assert narrowest == MIN_LOG_SPAN
    assert all(
        not start <= chain.unreadable_block <= end for _, start, end in chain.answered_windows
    )


# ------------------------------------------------------------------ budget


async def test_the_budget_is_never_exceeded_and_nothing_is_read_past_it(now) -> None:
    chain, _ = old_token()
    chain.max_log_span = 2_000
    census = await run(now, chain, from_block=OLD_CREATED, block=OLD_HEAD)
    assert census.status is Availability.UNAVAILABLE
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED
    assert census.failure is AtlasSourceFailure.INCOMPLETE_RESULT
    assert census.requests_made == 240
    assert chain.requests == 240


async def test_every_read_has_its_budget_slot(now) -> None:
    chain, _, _ = revenue_like()
    chain.max_log_span = 3_000
    census = await run(now, chain, bounds=CensusBounds(max_requests=60))
    assert census.requests_made <= 60
    assert chain.requests == census.requests_made


async def test_a_history_no_budget_could_cover_is_refused_before_any_log_read(now) -> None:
    """Even at 100 000-block windows, 2 x 120 windows already exceed the budget."""
    created = 10_000_000
    head = created + 12_000_000
    chain = FakeV4Chain(token=TOKEN, created_block=created, head=head)
    census = await run(now, chain, from_block=created, block=head)
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED
    assert not chain.log_requests


async def test_a_history_the_budget_can_cover_is_attempted(now) -> None:
    """Just inside the bound: the lower-bound check never refuses a possible census."""
    created = 10_000_000
    head = created + 11_600_000 - 1  # 116 windows per orientation: 232 + 6 reads
    chain = FakeV4Chain(token=TOKEN, created_block=created, head=head)
    census = await run(now, chain, from_block=created, block=head)
    assert census.status is Availability.AVAILABLE  # no pools: discovery was all it needed
    assert census.requests_made == 239


# ------------------------------------------------------------- semantics


async def test_a_never_traded_pool_is_still_found(now) -> None:
    chain, _, hidden = revenue_like()
    census = await run(now, chain)
    hidden_facts = next(pool for pool in census.pools if pool.pool_id == hidden.id)
    assert hidden_facts.hook_facts is not None


@pytest.mark.parametrize("token_first", [True, False])
async def test_the_token_is_found_as_either_currency(now, token_first) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pair = (TOKEN, MEME) if token_first else (NATIVE, TOKEN)
    pool = chain.add_pool(Pool(currency0=pair[0], currency1=pair[1], fee=3000, tick_spacing=60))
    census = await run(now, chain)
    assert [item.pool_id for item in census.pools] == [pool.id]


async def test_each_boundary_block_is_read_once(now) -> None:
    chain, _, _ = revenue_like()
    chain.max_log_span = 1_000
    await run(now, chain)
    for topics in {topics for topics, _, _ in chain.answered_windows}:
        windows = windows_of(chain, topics)
        blocks = sum(end - start + 1 for start, end in windows)
        assert blocks == windows[-1][1] - windows[0][0] + 1


async def test_position_bounds_still_hold_across_windows(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(traded_pool())
    for index in range(5):
        chain.mint(
            pool,
            "0x" + f"{index + 0x60:02x}" * 20,
            index + 1,
            -160_100,
            198_050,
            10**20,
            block=pool.created_block + 1 + index * 3_000,
        )
    chain.max_log_span = 2_500
    events = await run(now, chain, bounds=CensusBounds(max_position_events=4))
    assert events.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE
    positions = await run(now, chain, bounds=CensusBounds(max_positions=4))
    assert positions.gap is PoolControlGap.POSITION_FACTS_INCOMPLETE


async def test_the_pool_bound_still_holds_across_windows(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    for index in range(4):
        chain.add_pool(
            traded_pool(fee=100 * (index + 1), created_block=CREATED_BLOCK + index * 4_000)
        )
    chain.max_log_span = 3_000
    census = await run(now, chain, bounds=CensusBounds(max_pools=3))
    assert census.gap is PoolControlGap.CENSUS_BOUNDS_EXCEEDED
