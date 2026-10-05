"""LOCKED_ONLY_HOLDER_BASIS_GAP: an empty economic distribution is zero only when proven.

At T+0 an official launch has one holder -- the PoolManager -- and one
position, permanently locked in the official FeeSplitter. Nobody can move any
principal, so the economic concentration is exactly zero over the full supply.
Every weaker variant -- a holder prefix, a provider exclusion, a count above
the rows, a contract nobody verified, a running timelock, a remainder -- keeps
the conservative answer it had.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from src.agents.atlas.context import atlas_snapshot_digest, snapshot_document
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.models import AtlasReasonCode, HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.control import PositionControlState
from src.agents.atlas.v4.economic import MAX_UNATTRIBUTED_POOL_FRACTION
from src.agents.atlas.v4.models import (
    UNATTRIBUTED_POOL_BALANCE,
    ConcentrationBasis,
    PoolControlGap,
)
from src.core.models import AgentRole, RiskLimits, SafetyStatus, Side
from src.markets.models import Availability
from src.orchestration.riskdata.models import RiskFactKind
from src.orchestration.riskrequest.service import risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType, OnchainPayload
from src.risk.engine import evaluate
from tests.anchor.conftest import evidence_envelope
from tests.atlas.conftest import CREATOR, TOKEN
from tests.atlas.v4.chain import POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.custody import (
    OFFICIAL_SPLITTER,
    OPERATOR,
    TIMELOCK_RECIPIENT,
    fee_splitter_code,
    timelocked_code,
)
from tests.atlas.v4.scenarios import (
    MEME,
    REVENUE_REFERENCE_BLOCK,
    build,
    locked_only_launch,
    revenue_like,
    v4_market,
)
from tests.atlas.v4.test_economic import LIMITED
from tests.riskdata.conftest import (
    BASE_ASSET,
    RecordedMarkets,
    anchor_payload,
    build_reader,
    configured_costs,
    open_case,
    record_onchain,
    recorded_snapshot,
)

CORRELATION = UUID("00000000-0000-4000-8000-0000000000ac")
COMPLETE = HolderCompleteness.COMPLETE


async def locked_only(now, chain, supply, rows, **kw):
    options = {"rows": rows, "completeness": COMPLETE, "supply": supply, "holder_count": 1}
    options.update(kw)
    return await build(now, chain, **options)


def sentinel(now, payload: OnchainPayload, intent, context):
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    market = risk_market(
        base_asset_id=BASE_ASSET,
        price=price,
        base_asset=metadata,
        snapshot=snapshot,
        onchain=evidence_envelope(now, EvidenceType.ONCHAIN, AgentRole.ATLAS, payload),
        anchor=evidence_envelope(
            now,
            EvidenceType.LIQUIDITY_EXECUTION,
            AgentRole.ANCHOR,
            anchor_payload(uuid4(), uuid4()),
        ),
        costs=configured_costs(),
        correlation_id=CORRELATION,
        identity_key="locked-only",
        side=Side.BUY,
    )
    order = intent.model_copy(
        update={"asset_id": BASE_ASSET, "signal_price": Decimal("1.25"), "quantity": Decimal("10")}
    )
    return market, evaluate(order, market, context, RiskLimits(), now=now)


# ------------------------------------------------------------ the proven zero


async def test_a_locked_only_launch_has_exactly_zero_economic_concentration(now) -> None:
    chain, pool, supply, rows = locked_only_launch()
    snapshot = await locked_only(now, chain, supply, rows)
    control = snapshot.pool_control

    # Raw: the PoolManager is technically the only holder, with everything.
    assert snapshot.holders.top1_share == Decimal(1)
    assert snapshot.holders.top10_share == Decimal(1)

    (position,) = control.census.positions
    assert position.control.control_state is PositionControlState.PERMANENTLY_LOCKED
    assert control.status is Availability.AVAILABLE
    assert control.gap is None
    assert control.basis is ConcentrationBasis.EXACT
    assert control.economic_top1_share == 0
    assert control.economic_top5_share == 0
    assert control.economic_top10_share == 0
    assert control.economic_top_holders == ()
    # The lock does not shrink the denominator: the whole supply is locked.
    assert control.total_supply_raw == supply
    assert control.permanently_locked_pool_supply_fraction == 1
    assert control.unattributed_raw == 0
    # The fee beneficiary -- the verified creator -- holds no principal.
    assert control.creator_controlled_raw == 0


async def test_atlas_establishes_the_holder_domain_and_blocks_nothing(now) -> None:
    chain, _, supply, rows = locked_only_launch()
    snapshot = await locked_only(now, chain, supply, rows)
    for policy in (None, LIMITED):
        decision = (
            evaluate_snapshot(snapshot, now)
            if policy is None
            else evaluate_snapshot(snapshot, now, policy)
        )
        assert AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED not in decision.blockers
        assert AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE not in decision.data_gaps
        assert not any(code.value.startswith("V4_") for code in decision.data_gaps)


async def test_sentinel_judges_the_zero_as_an_established_figure(now, intent, context) -> None:
    chain, _, supply, rows = locked_only_launch()
    snapshot = await locked_only(now, chain, supply, rows)
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    assert payload.holder_integrity == "PASS"
    market, verdict = sentinel(now, payload, intent, context)
    assert market.holders.top_ten_fraction == 0
    assert market.holders.concentration_check is SafetyStatus.PASS
    assert "HOLDER_CONCENTRATION_LIMIT" not in verdict.reason_codes
    assert "HOLDER_METRICS_UNKNOWN" not in verdict.reason_codes
    assert "HOLDERS_UNKNOWN" not in verdict.reason_codes


async def test_a_meme_meme_locked_only_launch_is_zero_too(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(
        Pool(currency0=TOKEN, currency1=MEME, fee=3000, tick_spacing=60, tick=-1_000)
    )
    chain.mint(pool, OFFICIAL_SPLITTER, 9, 0, 6_000, 10**24)
    supply = chain.pool_balance(chain.head)
    rows = (HolderSourceRow(address=POOL_MANAGER, balance_raw=supply),)
    control = (await locked_only(now, chain, supply, rows, market=v4_market(pool))).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.economic_top10_share == 0 and control.basis is ConcentrationBasis.EXACT


# ---------------------------------------------------- nothing weaker passes


async def test_a_holder_prefix_never_proves_zero(now) -> None:
    chain, _, supply, rows = locked_only_launch()
    control = (
        await locked_only(now, chain, supply, rows, completeness=HolderCompleteness.TOP_N_ONLY)
    ).pool_control
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.HOLDER_BASIS_UNAVAILABLE
    assert control.economic_top10_share is None


async def test_a_provider_count_above_the_rows_never_proves_zero(now) -> None:
    chain, _, supply, rows = locked_only_launch()
    control = (await locked_only(now, chain, supply, rows, holder_count=2)).pool_control
    assert control.gap is PoolControlGap.HOLDER_BASIS_UNAVAILABLE


async def test_an_unresolved_provider_exclusion_never_proves_zero(now) -> None:
    chain, _, supply, rows = locked_only_launch()
    excluded = ("0x" + "e1" * 20,)
    snapshot = await locked_only(now, chain, supply, rows, excluded=excluded)
    assert snapshot.holders.unresolved_exclusions == excluded
    assert snapshot.pool_control.gap is PoolControlGap.HOLDER_BASIS_UNAVAILABLE
    assert snapshot.pool_control.economic_top10_share is None


async def test_supply_outside_every_row_never_proves_zero(now) -> None:
    """A complete set whose rows fall short of the supply misses somebody."""
    chain, _, supply, rows = locked_only_launch()
    control = (await locked_only(now, chain, supply + 10**18, rows)).pool_control
    assert control.status is Availability.UNAVAILABLE


async def test_unknown_custody_of_the_whole_supply_is_unresolved(now) -> None:
    locker = "0x" + "1c" * 20
    chain, _, supply, rows = locked_only_launch(owner=locker)
    chain.contracts.add(locker)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.gap is PoolControlGap.POSITION_CONTROL_UNRESOLVED
    assert control.unknown_custody_pool_supply_fraction == 1
    assert control.economic_top10_share is None


async def test_a_running_timelock_of_the_whole_supply_is_unresolved(now) -> None:
    chain, _, supply, rows = locked_only_launch(owner=TIMELOCK_RECIPIENT)
    chain.codes[TIMELOCK_RECIPIENT] = timelocked_code(timelock=chain.head + 1)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.gap is PoolControlGap.POSITION_CONTROL_UNRESOLVED
    assert control.timelocked_pool_supply_fraction == 1


async def test_a_released_timelock_is_the_operators_whole_supply(now) -> None:
    chain, _, supply, rows = locked_only_launch(owner=TIMELOCK_RECIPIENT)
    chain.codes[TIMELOCK_RECIPIENT] = timelocked_code(timelock=1)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.economic_top1_share == 1
    assert control.economic_top_holders[0].holder == OPERATOR


async def test_a_creator_owned_launch_is_the_creators_whole_supply(now) -> None:
    chain, _, supply, rows = locked_only_launch(owner=CREATOR)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.economic_top1_share == 1
    assert control.creator_controlled_pool_supply_fraction == 1


async def test_a_small_remainder_stays_a_pseudo_holder_not_zero(now) -> None:
    chain, _, supply, rows = locked_only_launch(extra=5 * 10**18)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.basis is ConcentrationBasis.UPPER_BOUND
    assert control.economic_top_holders[0].holder == UNATTRIBUTED_POOL_BALANCE
    assert control.economic_top10_share > 0


async def test_a_large_remainder_is_still_unknown(now) -> None:
    chain, _, supply, rows = locked_only_launch(extra=50_000_000 * 10**18)
    control = (await locked_only(now, chain, supply, rows)).pool_control
    assert control.unattributed_pool_supply_fraction > MAX_UNATTRIBUTED_POOL_FRACTION
    assert control.gap is PoolControlGap.POOL_BALANCE_UNATTRIBUTED


async def test_buyers_are_ranked_against_the_full_supply(now) -> None:
    """95 % locked, 5 % with buyers: the largest buyer's 2 % is the top one."""
    chain, _, locked, _ = locked_only_launch()
    supply = locked * 100 // 95
    free = supply - locked
    two_percent = supply * 2 // 100
    one_and_a_half = supply * 15 // 1000
    buyers = (
        HolderSourceRow(address="0x" + "b1" * 20, balance_raw=two_percent),
        HolderSourceRow(address="0x" + "b2" * 20, balance_raw=one_and_a_half),
        HolderSourceRow(address="0x" + "b3" * 20, balance_raw=free - two_percent - one_and_a_half),
    )
    rows = (HolderSourceRow(address=POOL_MANAGER, balance_raw=locked), *buyers)
    control = (await locked_only(now, chain, supply, rows, holder_count=4)).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.basis is ConcentrationBasis.EXACT
    tolerance = Decimal("1e-15")
    assert abs(control.economic_top1_share - Decimal("0.02")) < tolerance
    assert abs(control.economic_top10_share - Decimal("0.05")) < tolerance
    assert control.permanently_locked_pool_supply_fraction > Decimal("0.94")
    assert {item.holder for item in control.economic_top_holders} == {row.address for row in buyers}


# ----------------------------------------------------- evidence and digest


async def test_evidence_and_digest_carry_the_zero(now) -> None:
    chain, _, supply, rows = locked_only_launch()
    snapshot = await locked_only(now, chain, supply, rows)
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    summary = payload.intelligence.pool_control
    assert summary.status == "AVAILABLE" and summary.basis == "EXACT"
    assert summary.economic_concentration == 0
    assert summary.economic_top_holders == ()
    assert summary.permanently_locked_pool_supply_fraction == 1
    restored = OnchainPayload.model_validate_json(payload.model_dump_json())
    assert restored.model_dump_json() == payload.model_dump_json()

    document = snapshot_document(snapshot)["pool_control"]
    assert document["economic_top10_share"] == "0"
    assert document["basis"] == "EXACT"
    prefix = await locked_only(
        now, *locked_only_launch()[::2], rows, completeness=HolderCompleteness.TOP_N_ONLY
    )
    assert atlas_snapshot_digest(snapshot) != atlas_snapshot_digest(prefix)


async def test_risk_data_carries_the_proven_zero(worker_db, now, trace) -> None:
    _, sessions = worker_db
    chain, pool, supply, rows = locked_only_launch()
    payload = onchain_payload(
        snapshot := await locked_only(now, chain, supply, rows),
        evaluate_snapshot(snapshot, now),
    )
    pair = v4_market(pool).pair_id
    reader = build_reader(sessions, now, feed=RecordedMarkets(recorded_snapshot(now, pair_id=pair)))
    trade_case = await open_case(
        reader.cases, now, trace, key=f"locked-only-{uuid4()}", identity=v4_market(pool)
    )
    await record_onchain(reader.cases, trade_case, now, payload)
    reading = await reader.readiness(trade_case.id)
    assert not any(item.kind is RiskFactKind.HOLDER_CONCENTRATION for item in reading.gaps)
    assert any(item.kind is RiskFactKind.HOLDER_CONCENTRATION for item in reading.facts)


# ------------------------------------------------------------------ REVENUE


async def test_revenue_is_unchanged_at_its_reference_block(now) -> None:
    """The creator's 57.4 % is still direct control before its removal."""
    chain, _, hidden = revenue_like("official")
    assert chain.head <= REVENUE_REFERENCE_BLOCK
    control = (await build(now, chain)).pool_control
    creator = next(item for item in control.census.positions if item.pool_id == hidden.id)
    assert creator.control.control_state is PositionControlState.DIRECT_CONTROL
    assert control.creator_controlled_pool_supply_fraction == Decimal("0.574")
    assert control.economic_top10_share > Decimal("0.35")
