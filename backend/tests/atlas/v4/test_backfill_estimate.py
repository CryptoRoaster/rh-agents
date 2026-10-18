"""Issue #94: the pool index backfill at Robinhood scale, against the fixture chain.

A dry run of exactly the code the live backfill would run, over a PoolManager
history of the size the chain has (deployed early, head at ~77.2 million
blocks), in bounded calls that each resume where the last one stopped. It pins
the read counts the live budget is planned with: four reads per window plus
the one-time location of the deployment block, and how a provider that only
answers 25 000-block windows multiplies them. No RPC is involved.
"""

import math

import pytest

from src.agents.atlas.v4.pool_index import V4PoolIndexer
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.chain import HEAD_BLOCK, NATIVE, FakeV4Chain, Pool
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import deployment

DEPLOYED = 1_000_000
CONFIRMATIONS = 12
PER_CALL = 800


def history() -> FakeV4Chain:
    chain = FakeV4Chain(token=TOKEN)
    chain.manager_deployed_block = DEPLOYED
    # A pool every ~5 million blocks, of other tokens and of the token, the
    # last one well before the token existed.
    chain.foreign_pools += [
        Pool(
            currency0=NATIVE,
            currency1="0x" + f"{index + 1:040x}",
            fee=3000,
            tick_spacing=60,
            created_block=DEPLOYED + index * 5_000_000 + 17,
        )
        for index in range(15)
    ]
    return chain


async def backfill(chain: FakeV4Chain, store: MemoryPoolIndexStore) -> tuple[int, int]:
    """Bounded calls until complete. Returns (calls, total reads)."""
    calls = reads = 0
    while True:
        calls += 1
        result = await V4PoolIndexer(
            reads=chain,
            store=store,
            deployment=deployment(),
            confirmations=CONFIRMATIONS,
            max_requests=PER_CALL,
        ).advance()
        reads += result.requests
        if result.status == "COMPLETE":
            return calls, reads
        assert result.status == "PARTIAL", result
        assert calls < 50


@pytest.mark.parametrize(("cap", "per_window"), [(None, 100_000), (25_000, 25_000)])
async def test_the_backfill_reads_what_the_budget_plans(now, cap, per_window) -> None:
    chain = history()
    chain.max_log_span = cap
    store = MemoryPoolIndexStore()
    calls, reads = await backfill(chain, store)

    safe = HEAD_BLOCK - CONFIRMATIONS
    windows = math.ceil((safe - DEPLOYED + 1) / per_window)
    # Per window: last-block hash before and after the logs, the logs, and the
    # parent hash. Once: deployment search (~log2 head + 1), its hash, chain id
    # and head per call, the tip check on every resumed call, and with a capped
    # provider two refused probes on the first window of every call.
    search = math.ceil(math.log2(safe)) + 2
    per_call = 2 + 1  # chain id, head, tip check (absent on the first call)
    probes = 0 if cap is None else 2 * calls
    expected = 4 * windows + search + per_call * calls - 1 + probes
    # A call that runs out inside a window loses the reads already spent on it
    # (at most the narrowing probes and three more); nothing else is wasted.
    assert expected - 2 <= reads <= expected + calls * (3 + (0 if cap is None else 2)), (
        reads,
        expected,
    )
    coverage = await store.coverage(chain.network_id, chain.pool_manager)
    assert coverage is not None and coverage.deployment_block == DEPLOYED
    assert coverage.through_block == safe
    assert len(store.pools[(chain.network_id, chain.pool_manager)]) == 15
    print(f"cap={cap} windows={windows} calls={calls} reads={reads}")
