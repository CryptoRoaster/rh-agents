"""Economic concentration, ATLAS policy, durable evidence and the digest.

Raw holder facts are never modified; the economic view is derived beside them
and fails closed for a V4 token whenever it cannot be established.
"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from src.agents.atlas.context import (
    AtlasContextUnavailable,
    atlas_snapshot_digest,
    snapshot_document,
)
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.models import (
    AtlasReasonCode,
    AtlasVerdict,
    HolderSourceRow,
    OriginVerification,
)
from src.agents.atlas.policy import ATLAS_POLICY_V2, evaluate_snapshot
from src.agents.atlas.v4.economic import MAX_UNATTRIBUTED_POOL_FRACTION
from src.agents.atlas.v4.models import (
    UNATTRIBUTED_POOL_BALANCE,
    ConcentrationBasis,
    PoolControlGap,
)
from src.markets.models import Availability
from src.orchestration.workflow.models import OnchainIntelligence, OnchainPayload
from tests.atlas.conftest import CREATOR, TOKEN, market_identity
from tests.atlas.v4.chain import CREATED_BLOCK, NATIVE, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.scenarios import (
    CREATOR_HOOK,
    FULL_RANGE,
    LOCKER,
    SUPPLY,
    UNIT,
    build,
    liquidity_for,
    origin,
    revenue_like,
    traded_pool,
    wallets,
)

LIMITED = replace(
    ATLAS_POLICY_V2, version="atlas-policy-test", max_top10_concentration=Decimal("0.35")
)


def spread_pool(chain: FakeV4Chain, owners: int = 8, each: int = 15_000_000 * UNIT) -> Pool:
    """One traded pool whose liquidity is spread over many independent owners."""
    pool = chain.add_pool(traded_pool())
    for index in range(owners):
        chain.mint(
            pool,
            "0x" + f"{index + 0x60:02x}" * 20,
            index + 1,
            -160_100,
            198_050,
            liquidity_for(each, pool.tick, -160_100),
        )
    return pool


# ------------------------------------------------------------------ REVENUE


async def test_revenue_like_supply_reappears_with_its_controller(now) -> None:
    # The launch NFT in the official FeeSplitter: everything else is resolved,
    # so the economic figure is established -- and the creator still tops it.
    chain, _, hidden = revenue_like("official")
    snapshot = await build(now, chain)
    control = snapshot.pool_control

    assert control is not None and control.status is Availability.AVAILABLE
    assert control.required
    # The raw view is untouched and stays low.
    assert Decimal("0.09") <= snapshot.holders.top10_share <= Decimal("0.10")
    # The economic view finds the controller.
    assert control.economic_top10_share > Decimal("0.60")
    assert control.economic_top10_share > Decimal("0.35")
    assert control.economic_top_holders[0].holder == CREATOR
    assert control.creator_controlled_pool_supply_fraction == Decimal("0.574")
    assert control.creator_controlled_hooks == 1
    assert control.pools_total == 2 and control.pools_with_hooks == 1
    assert control.positions_total == 2 and control.positions_unattributed == 0
    hidden_position = next(item for item in control.census.positions if item.pool_id == hidden.id)
    assert hidden_position.owner_is_creator is True


async def test_atlas_policy_never_passes_it_on_a_limit(now) -> None:
    chain, _, _ = revenue_like("official")
    snapshot = await build(now, chain)
    # The provisional policy sets no limit of its own: the domain is
    # established, and SENTINEL's existing limit judges the figure.
    assert evaluate_snapshot(snapshot, now).verdict is AtlasVerdict.CLEAR
    limited = evaluate_snapshot(snapshot, now, LIMITED)
    assert limited.verdict is AtlasVerdict.BLOCKED
    assert AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED in limited.blockers
    # The raw figure alone would have passed the same limit.
    assert snapshot.holders.top10_share < Decimal("0.35")


# -------------------------------------------------------------- no double count


async def test_a_reported_pool_manager_row_is_not_counted_twice(now) -> None:
    chain, _, _ = revenue_like("official")
    held = chain.pool_balance(chain.head)
    rows = (HolderSourceRow(address=POOL_MANAGER, balance_raw=held), *wallets())
    with_row = (await build(now, chain, rows=rows)).pool_control
    chain.requests = 0
    without_row = (await build(now, chain)).pool_control

    assert with_row is not None and without_row is not None
    assert all(item.holder != POOL_MANAGER for item in with_row.economic_top_holders)
    assert with_row.economic_top10_share == without_row.economic_top10_share
    total = sum(item.balance_raw for item in with_row.economic_top_holders)
    assert total <= SUPPLY


async def test_a_small_remainder_is_ranked_as_one_unattributed_holder(now) -> None:
    chain, _, _ = revenue_like("official")
    control = (await build(now, chain)).pool_control
    assert control.unattributed_raw == chain.extra_pool_balance
    assert control.basis is ConcentrationBasis.UPPER_BOUND
    assert (
        control.attributed_raw + control.unattributed_raw == control.census.pool_manager_balance_raw
    )
    ranked = {item.holder: item.balance_raw for item in control.economic_top_holders}
    # Too small to rank in this top ten, and never folded into anybody else.
    assert UNATTRIBUTED_POOL_BALANCE not in ranked
    assert control.unattributed_raw < min(ranked.values())


async def test_a_ranking_remainder_appears_as_its_own_holder(now) -> None:
    chain, _, _ = revenue_like("official")
    chain.extra_pool_balance = 9_900_000 * UNIT
    control = (await build(now, chain)).pool_control
    ranked = {item.holder: item.balance_raw for item in control.economic_top_holders}
    assert ranked[UNATTRIBUTED_POOL_BALANCE] == 9_900_000 * UNIT


async def test_an_exact_view_has_no_bound(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    spread_pool(chain)
    rows = tuple(
        HolderSourceRow(address="0x" + f"{i + 0x30:02x}" * 20, balance_raw=10**25 - i)
        for i in range(5)
    )
    from src.agents.atlas.models import HolderCompleteness

    control = (
        await build(now, chain, rows=rows, completeness=HolderCompleteness.COMPLETE)
    ).pool_control
    # Position owners are not in the (complete, wholly retained) holder set.
    assert control.status is Availability.AVAILABLE
    assert control.unattributed_raw == 0
    assert control.basis is ConcentrationBasis.EXACT


async def test_a_large_unexplained_remainder_is_unknown_not_a_figure(now) -> None:
    """A pool initialized before the scan start is missed — and still caught."""
    chain, _, _ = revenue_like("official")
    early = chain.add_pool(
        traded_pool(fee=10_000, tick_spacing=200, created_block=CREATED_BLOCK - 5)
    )
    chain.mint(
        early,
        CREATOR,
        77,
        -887_200,
        887_200,
        liquidity_for(50_000_000 * UNIT, early.tick, -887_200),
    )
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    assert early.id not in {pool.pool_id for pool in control.census.pools}
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.POOL_BALANCE_UNATTRIBUTED
    assert control.economic_top10_share is None
    assert control.unattributed_pool_supply_fraction > MAX_UNATTRIBUTED_POOL_FRACTION
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict is AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.V4_POOL_BALANCE_UNATTRIBUTED in decision.data_gaps


# ------------------------------------------------------------- negative cases


async def test_a_normal_v4_pool_with_spread_owners_passes(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    spread_pool(chain)
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    assert control.status is Availability.AVAILABLE
    assert control.economic_top10_share < Decimal("0.35")
    assert evaluate_snapshot(snapshot, now, LIMITED).verdict is AtlasVerdict.CLEAR


async def test_a_second_legitimate_pool_is_no_false_block(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    spread_pool(chain)
    other = chain.add_pool(traded_pool(fee=100, tick_spacing=1, created_block=CREATED_BLOCK + 500))
    chain.mint(
        other,
        "0x" + "77" * 20,
        99,
        *FULL_RANGE,
        liquidity_for(5_000_000 * UNIT, other.tick, FULL_RANGE[0]),
    )
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    assert control.pools_total == 2
    assert control.creator_controlled_pool_supply_fraction == 0
    assert control.creator_controlled_hooks == 0
    assert evaluate_snapshot(snapshot, now, LIMITED).verdict is AtlasVerdict.CLEAR


async def test_a_hook_without_owner_is_never_the_creators(now) -> None:
    chain, _, _ = revenue_like("official")
    del chain.hook_owners[CREATOR_HOOK]
    control = (await build(now, chain)).pool_control
    assert control.creator_controlled_hooks == 0
    hook = next(pool.hook_facts for pool in control.census.pools if pool.hook_facts is not None)
    assert hook.owner_is_creator is None


async def test_creator_control_needs_a_verified_creator(now) -> None:
    chain, _, _ = revenue_like("official")
    unverified = origin(verification=OriginVerification.UNVERIFIED)
    control = (await build(now, chain, origin_facts=unverified)).pool_control
    assert control.creator_controlled_pool_supply_fraction is None
    assert control.creator_controlled_hooks == 0
    # The economic figure itself never depended on who the creator is.
    assert control.economic_top10_share > Decimal("0.60")


async def test_a_position_owner_who_is_not_the_creator_is_attributed_to_them(now) -> None:
    chain, _, hidden = revenue_like("official")
    from tests.atlas.v4.chain import POSITION_MANAGER

    other = "0x" + "b9" * 20
    chain.nft_owners[(POSITION_MANAGER, 3_498_775)] = other
    control = (await build(now, chain)).pool_control
    position = next(item for item in control.census.positions if item.pool_id == hidden.id)
    assert position.owner == other and position.owner_is_creator is False
    assert control.economic_top_holders[0].holder == other
    assert control.creator_controlled_pool_supply_fraction == 0


async def test_an_unknown_direct_position_fails_closed(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = spread_pool(chain)
    vault = "0x" + "5b" * 20
    chain.contracts.add(vault)
    chain.modify(
        pool,
        vault,
        -160_100,
        198_050,
        liquidity_for(100_000_000 * UNIT, pool.tick, -160_100),
        "0x" + "00" * 32,
    )
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.POSITION_OWNER_UNKNOWN
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict is AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.V4_POSITION_FACTS_INCOMPLETE in decision.data_gaps
    assert onchain_payload(snapshot, decision).holder_integrity == "UNKNOWN"


async def test_a_census_timeout_leaves_a_v4_token_unestablished(now) -> None:
    chain, _, _ = revenue_like("official")
    chain.fail_at = 9
    snapshot = await build(now, chain)
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict is AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE in decision.data_gaps
    # The raw holder figure is still measured, recorded — and not trusted.
    assert snapshot.holders.top10_share is not None


async def test_a_v4_market_without_a_census_is_unestablished(now) -> None:
    chain, traded, _ = revenue_like("official")
    from tests.atlas.v4.scenarios import v4_market

    snapshot = await build(now, None, market=v4_market(traded))
    control = snapshot.pool_control
    assert control is not None and control.required
    assert control.gap is PoolControlGap.DEPLOYMENT_NOT_CONFIGURED
    assert AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE in evaluate_snapshot(snapshot, now).data_gaps


async def test_another_market_without_a_census_is_exactly_as_before(now) -> None:
    snapshot = await build(now, None, market=market_identity())
    assert snapshot.pool_control is None
    assert "pool_control" not in snapshot_document(snapshot)
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict is AtlasVerdict.CLEAR


async def test_another_market_whose_census_fails_keeps_its_holder_path(now) -> None:
    chain, _, _ = revenue_like("official")
    chain.fail_at = 2
    snapshot = await build(now, chain, market=market_identity())
    assert snapshot.pool_control is not None and not snapshot.pool_control.required
    assert evaluate_snapshot(snapshot, now).verdict is AtlasVerdict.CLEAR


async def test_another_market_whose_token_has_v4_pools_requires_them(now) -> None:
    chain, _, _ = revenue_like("official")
    snapshot = await build(now, chain, market=market_identity())
    assert snapshot.pool_control_required
    assert snapshot.pool_control.economic_top10_share > Decimal("0.60")


async def test_a_wrong_chain_census_is_refused_through_the_builder(now) -> None:
    chain, _, _ = revenue_like("official")
    chain.network_id = 56
    with pytest.raises(AtlasContextUnavailable) as refused:
        await build(now, chain)
    assert refused.value.reason_code == "SOURCE_CHAIN_MISMATCH"


async def test_unavailable_holders_leave_no_economic_figure(now) -> None:
    chain, _, _ = revenue_like("official")
    control = (await build(now, chain, rows=())).pool_control
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.HOLDER_BASIS_UNAVAILABLE


# ----------------------------------------------------------- digest, evidence


async def test_the_digest_covers_pool_control(now) -> None:
    chain, _, _ = revenue_like("official")
    first = await build(now, chain)
    again = await build(now, chain)
    assert atlas_snapshot_digest(first) == atlas_snapshot_digest(again)

    from tests.atlas.v4.chain import POSITION_MANAGER

    chain.nft_owners[(POSITION_MANAGER, 3_498_775)] = "0x" + "b9" * 20
    moved = await build(now, chain)
    assert atlas_snapshot_digest(moved) != atlas_snapshot_digest(first)
    without = first.model_copy(update={"pool_control": None})
    assert atlas_snapshot_digest(without) != atlas_snapshot_digest(first)


async def test_evidence_carries_a_bounded_audit_record(now) -> None:
    chain, traded, hidden = revenue_like("official")
    snapshot = await build(now, chain)
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    summary = payload.intelligence.pool_control

    assert summary.required and summary.status == "AVAILABLE"
    assert {pool.pool_id for pool in summary.pools} == {traded.id, hidden.id}
    assert summary.raw_top_ten_fraction == snapshot.holders.top10_share
    assert summary.economic_top_ten_fraction == snapshot.pool_control.economic_top10_share
    assert summary.economic_concentration > Decimal("0.60")
    assert summary.snapshot_block == chain.head
    assert summary.pools_with_hooks == 1 and summary.creator_controlled_hooks == 1
    hook_pool = next(pool for pool in summary.pools if pool.pool_id == hidden.id)
    assert "BEFORE_SWAP" in hook_pool.hook_permissions and hook_pool.hook_owner == CREATOR
    # The raw holder record is exactly what it always was.
    assert payload.intelligence.holders.top_ten_fraction == snapshot.holders.top10_share
    # Round-trips through JSON unchanged.
    restored = OnchainPayload.model_validate_json(payload.model_dump_json())
    assert restored == payload
    assert restored.model_dump_json() == payload.model_dump_json()


async def test_listed_positions_are_bounded_while_counts_stay_complete(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    spread_pool(chain, owners=30, each=1_000_000 * UNIT)
    snapshot = await build(now, chain)
    summary = onchain_payload(snapshot, evaluate_snapshot(snapshot, now)).intelligence.pool_control
    assert summary.positions_total == 30
    assert len(summary.positions) == 16


def legacy_intelligence() -> dict:
    return {
        "verdict": "CLEAR",
        "policy_version": "atlas-policy-v2",
        "blockers": [],
        "data_gaps": [],
        "domain_status": {"CONTRACT": "AVAILABLE"},
        "chain_id": 4663,
        "block_number": 1,
        "snapshot_digest": "a" * 64,
    }


def test_legacy_evidence_without_pool_control_replays_byte_for_byte() -> None:
    raw = json.dumps(legacy_intelligence(), separators=(",", ":"))
    parsed = OnchainIntelligence.model_validate_json(raw)
    assert parsed.pool_control is None
    assert "pool_control" not in json.loads(parsed.model_dump_json())
    assert "holders" not in json.loads(parsed.model_dump_json())


async def test_unavailable_pool_control_is_recorded_with_its_reason(now) -> None:
    chain, _, _ = revenue_like("official")
    chain.fail_at = 9
    snapshot = await build(now, chain)
    summary = onchain_payload(snapshot, evaluate_snapshot(snapshot, now)).intelligence.pool_control
    assert summary.status == "UNAVAILABLE"
    assert summary.gap == "V4_POOL_CENSUS_UNAVAILABLE"
    assert summary.failure == "TIMEOUT"
    assert summary.economic_concentration is None


async def test_the_model_may_cite_pool_control_addresses(now) -> None:
    chain, _, _ = revenue_like()
    snapshot = await build(now, chain)
    assert {CREATOR_HOOK, CREATOR, LOCKER} <= snapshot.addresses


async def test_a_census_that_breaks_off_after_finding_pools_keeps_them_required(now) -> None:
    """Pools proven before a failure cannot make the token look V4-free."""
    chain, _, _ = revenue_like("official")
    chain.fail_at = 20  # inside the position reads, after both pools were found
    snapshot = await build(now, chain, market=market_identity())
    control = snapshot.pool_control
    assert control.census.pools == () and control.census.pools_found == 2
    assert control.required and snapshot.pool_control_required
    assert (
        AtlasReasonCode.V4_POSITION_FACTS_INCOMPLETE in evaluate_snapshot(snapshot, now).data_gaps
    )


async def test_a_v4_market_whose_own_pool_is_missing_is_incomplete(now) -> None:
    from tests.atlas.v4.scenarios import v4_market

    chain, _, _ = revenue_like("official")
    elsewhere = Pool(NATIVE, TOKEN, 3000, 60, created_block=CREATED_BLOCK - 5)
    snapshot = await build(now, chain, market=v4_market(elsewhere))
    control = snapshot.pool_control
    assert control.gap is PoolControlGap.MARKET_POOL_NOT_FOUND
    assert control.economic_top10_share is None
    assert AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE in evaluate_snapshot(snapshot, now).data_gaps
