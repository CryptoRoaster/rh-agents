"""Issue #94: a surplus is judged on how completely pools were discovered.

Discovery from the token's creation block can miss a pool initialized before
the token existed, so a PoolManager surplus is unexplained. With the pool
index covering the PoolManager's whole history, such a pool is found -- its
positions are positions, not surplus -- and what remains beyond every
liability is owed to nobody; the economic figure still has to stop ranking it
(issue #103), so it does not pass yet. Real ATLAS pipeline throughout.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.pool_index import PoolIndexHistory, V4PoolIndexer
from src.core.models import LpControlStatus
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.chain import HEAD_BLOCK, NATIVE, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import HIDDEN_RANGE, build, deployment, official_launch
from tests.lpcontrol.test_lp_control import recorded

HIDDEN_OWNER = "0x" + "ad" * 20


def launch_chain(*, early: bool, surplus: int) -> tuple[FakeV4Chain, Pool]:
    chain, pool, _ = official_launch()
    chain.extra_pool_balance = surplus
    chain.manager_deployed_block = HEAD_BLOCK - 350_000
    if early:
        hidden = chain.add_pool(
            Pool(
                currency0=NATIVE,
                currency1=TOKEN,
                fee=500,
                tick_spacing=10,
                created_block=chain.created_block - 1_000,
                tick=300_000,
            )
        )
        chain.mint(
            hidden, HIDDEN_OWNER, 9_999_999, *HIDDEN_RANGE, 10**18, block=chain.created_block + 50
        )
    return chain, pool


async def judged(now, chain: FakeV4Chain, pool: Pool, *, indexed: bool):
    store = MemoryPoolIndexStore()
    if indexed:
        result = await V4PoolIndexer(
            reads=chain, store=store, deployment=deployment(), confirmations=20, max_requests=200
        ).advance()
        assert result.status == "COMPLETE"
    rows = (HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),)
    _, _, buyers = official_launch()
    rows = (*rows, *buyers[1:])
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
        index=PoolIndexHistory(store) if indexed else None,
    )
    payload: OnchainPayload = atlas_payload(snapshot, evaluate_snapshot(snapshot, now))
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    found = lp_control_view(snapshot=recorded(now, pool_id=pool.id), onchain=payload)
    return payload.intelligence.pool_control, found


async def test_without_the_index_an_early_pool_hides_in_an_unexplained_surplus(now) -> None:
    chain, pool = launch_chain(early=True, surplus=0)
    control, found = await judged(now, chain, pool, indexed=False)
    assert control.pool_discovery_basis == "TOKEN_CREATION"
    assert int(control.surplus_raw or "0") > 0
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_POOL_SURPLUS_UNEXPLAINED")


async def test_with_the_index_the_early_pool_is_found_and_nothing_hides(now) -> None:
    chain, pool = launch_chain(early=True, surplus=0)
    control, found = await judged(now, chain, pool, indexed=True)
    assert control.pool_discovery_basis == "MANAGER_HISTORY"
    assert control.pool_discovery_from_block == HEAD_BLOCK - 350_000
    assert len(control.pools) == 2
    assert control.surplus_raw == "0" and control.unattributed_raw == "0"
    # The hidden owner's position in another pool is a position now; the
    # market pool itself is only the locked launch.
    assert found[0] is LpControlStatus.PASS, found.detail


async def test_a_surplus_over_complete_discovery_is_nobodys(now) -> None:
    # Since issue #103 the economic figure credits it to nobody, and it passes.
    chain, pool = launch_chain(early=False, surplus=13)
    control, found = await judged(now, chain, pool, indexed=True)
    assert control.pool_discovery_basis == "MANAGER_HISTORY"
    assert control.surplus_raw == "13"
    assert control.remainder_unowned_raw == "13"
    assert found[0] is LpControlStatus.PASS, found.detail


async def test_legacy_evidence_without_a_discovery_basis_is_unexplained(now) -> None:
    chain, pool = launch_chain(early=False, surplus=13)
    control, _ = await judged(now, chain, pool, indexed=True)
    # Evidence recorded before #94 carries neither a discovery basis nor a credit.
    payload_control = control.model_copy(
        update={
            "pool_discovery_basis": None,
            "pool_discovery_from_block": None,
            "remainder_credit_status": None,
            "remainder_credited_raw": None,
            "remainder_unowned_raw": None,
            "remainder_uncredited_raw": None,
        }
    )
    assert "pool_discovery_basis" not in payload_control.model_dump(mode="json")
    from src.orchestration.riskrequest.service import unresolved_custody

    assert unresolved_custody(payload_control) == "V4_POOL_SURPLUS_UNEXPLAINED"
