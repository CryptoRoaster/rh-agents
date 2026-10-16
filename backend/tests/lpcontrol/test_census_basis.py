"""Issue #92, final review finding 2: a V4 PASS rests on a verified census basis.

The census is evidence only for the chain, PoolManager and block it was read
at. Its PoolManager must be the verified V4 deployment of the market's chain,
its chain must be the ATLAS evidence's chain, and its block must be the block
that evidence was pinned to. A census that is foreign, older, from another
chain or without a known deployment is UNKNOWN -- however well its pool id
matches.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import LpControlStatus, RiskOutcome
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.v4.chain import HEAD_BLOCK, POOL_MANAGER
from tests.atlas.v4.scenarios import build, official_launch
from tests.lpcontrol.test_lp_control import sentinel

FOREIGN_MANAGER = "0x" + "11" * 20
OTHER_CHAIN_ID = 8453


async def launch(now, *, block: int = HEAD_BLOCK) -> tuple[OnchainPayload, str]:
    """A complete, verified official launch, pinned at ``block``."""
    chain, pool, rows = official_launch()
    chain.extra_pool_balance = 0
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *rows[1:],
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
        block=block,
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now)), pool.id


def with_census(payload: OnchainPayload, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None
    assert payload.intelligence.pool_control is not None
    control = payload.intelligence.pool_control.model_copy(update=changes)
    return with_intelligence(payload, pool_control=control)


def with_intelligence(payload: OnchainPayload, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None
    return payload.model_copy(
        update={"intelligence": payload.intelligence.model_copy(update=changes)}
    )


def refused(now, intent, context, payload: OnchainPayload, pool_id: str, detail: str) -> None:
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert (market.lp_control.status, market.lp_control.detail) == (
        LpControlStatus.UNKNOWN,
        detail,
    )
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


async def test_a_census_of_a_foreign_pool_manager_is_unknown(now, intent, context) -> None:
    # A
    payload, pool_id = await launch(now)
    foreign = with_census(payload, pool_manager=FOREIGN_MANAGER)
    refused(now, intent, context, foreign, pool_id, "V4_CENSUS_POOL_MANAGER_UNVERIFIED")


async def test_a_census_block_other_than_the_atlas_block_is_unknown(now, intent, context) -> None:
    # B
    payload, pool_id = await launch(now)
    assert payload.intelligence is not None
    block = payload.intelligence.block_number
    for other in (block - 1, block + 1):
        shifted = with_census(payload, snapshot_block=other)
        refused(now, intent, context, shifted, pool_id, "V4_CENSUS_BLOCK_MISMATCH")


async def test_a_census_from_an_older_snapshot_is_unknown(now, intent, context) -> None:
    # C: a real census, read 500 blocks earlier, beside today's ATLAS evidence.
    older, pool_id = await launch(now, block=HEAD_BLOCK - 500)
    current, current_pool = await launch(now)
    assert pool_id == current_pool
    assert older.intelligence is not None and older.intelligence.pool_control is not None
    assert older.intelligence.pool_control.snapshot_block == HEAD_BLOCK - 500
    stale = with_intelligence(current, pool_control=older.intelligence.pool_control)
    refused(now, intent, context, stale, pool_id, "V4_CENSUS_BLOCK_MISMATCH")


async def test_a_census_of_another_chain_is_unknown(now, intent, context) -> None:
    # D: the pool id matches, the census basis names another chain.
    payload, pool_id = await launch(now)
    elsewhere = with_census(payload, chain_id=OTHER_CHAIN_ID)
    refused(now, intent, context, elsewhere, pool_id, "V4_CENSUS_CHAIN_MISMATCH")
    unnamed = with_census(payload, chain_id=None)
    refused(now, intent, context, unnamed, pool_id, "V4_CENSUS_CHAIN_MISMATCH")


async def test_a_census_without_a_known_deployment_is_unknown(now, intent, context) -> None:
    # E: consistent with itself, but no verified V4 deployment exists for it.
    payload, pool_id = await launch(now)
    unlisted = with_intelligence(
        with_census(payload, chain_id=OTHER_CHAIN_ID), chain_id=OTHER_CHAIN_ID
    )
    refused(now, intent, context, unlisted, pool_id, "V4_DEPLOYMENT_UNKNOWN")


async def test_a_census_bound_to_its_manager_and_block_passes(now, intent, context) -> None:
    # F
    payload, pool_id = await launch(now)
    assert payload.intelligence is not None
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS
    assert market.lp_control.census_block == payload.intelligence.block_number
    assert market.lp_control.pool_id == pool_id
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes


async def test_an_incomplete_or_foreign_position_manager_basis_is_unknown(
    now, intent, context
) -> None:
    payload, pool_id = await launch(now)
    refused(
        now,
        intent,
        context,
        with_census(payload, position_managers=(FOREIGN_MANAGER,)),
        pool_id,
        "V4_CENSUS_POSITION_MANAGER_UNVERIFIED",
    )
    for missing in ({"pool_manager": None}, {"snapshot_block": None}):
        refused(
            now,
            intent,
            context,
            with_census(payload, **missing),
            pool_id,
            "V4_POOL_CENSUS_BASIS_INCOMPLETE",
        )


def test_the_market_view_is_the_only_producer_of_a_sentinel_snapshot() -> None:
    """LP control binds to the traded pool where it is derived: in the market view,
    from the market's own pool locator. SENTINEL's snapshot carries no pool of its
    own to compare against, so a second producer could pair any evidence with any
    market. There is none; this keeps it that way.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src"
    producers = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text())
        names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "src.core.models"
            for alias in node.names
            if alias.name == "MarketSnapshot"
        }
        if path.name == "models.py" and path.parent.name == "core":
            names.add("MarketSnapshot")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else None
                if called in names:
                    producers.append(str(path.relative_to(source)))
                if isinstance(func, ast.Attribute) and func.attr == "model_copy":
                    keywords = ast.dump(node)
                    if "lp_control" in keywords:
                        producers.append(f"{path.relative_to(source)}:model_copy")
    assert producers == ["orchestration/riskrequest/service.py"]
