"""Position control end to end: ATLAS facts → economic view → evidence → risk data → SENTINEL.

The official launch puts the whole fixed supply into one position whose NFT
the official FeeSplitter holds for good. Its raw holder view is PoolManager-
dominated, and that position is nobody's to dump: it must not read as a 100 %
holder. Everything that only claims such a lock -- a contract nobody verified,
a creator's own wrapper, a timelock still running -- stays unresolved and
fails closed, while a REVENUE-style creator position is still the creator's.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from src.agents.atlas.context import atlas_snapshot_digest, snapshot_document
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.models import AtlasReasonCode, AtlasVerdict
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.control import PositionControlState
from src.agents.atlas.v4.models import PoolControlGap
from src.core.models import AgentRole, RiskLimits, SafetyStatus, Side
from src.markets.models import Availability
from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactKind
from src.orchestration.riskrequest.service import (
    entry_concentration,
    entry_concentration_established,
    risk_market,
)
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import (
    EvidenceType,
    OnchainPayload,
    PoolControlPosition,
    PoolControlSummary,
)
from src.risk.engine import evaluate
from tests.anchor.conftest import evidence_envelope
from tests.atlas.conftest import CREATOR, TOKEN
from tests.atlas.v4.chain import POOL_MANAGER, POSITION_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.custody import (
    OFFICIAL_SPLITTER,
    OPERATOR,
    TIMELOCK_RECIPIENT,
    fee_splitter_code,
    timelocked_code,
)
from tests.atlas.v4.scenarios import (
    BENEFICIARY_VAULT,
    MEME,
    build,
    liquidity_for,
    official_launch,
    revenue_like,
    traded_pool,
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

CORRELATION = UUID("00000000-0000-4000-8000-0000000000ab")
WRAPPER = "0x" + "c4" * 20
UNKNOWN_LOCKER = "0x" + "1c" * 20


async def payload_for(now, chain, **kw) -> OnchainPayload:
    snapshot = await build(now, chain, **kw)
    return onchain_payload(snapshot, evaluate_snapshot(snapshot, now))


def sentinel(now, payload: OnchainPayload, intent, context, side: Side = Side.BUY):
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
        identity_key="position-control",
        side=side,
    )
    order = intent.model_copy(
        update={
            "asset_id": BASE_ASSET,
            "side": side,
            "signal_price": Decimal("1.25"),
            "quantity": Decimal("10"),
        }
    )
    return market, evaluate(order, market, context, RiskLimits(), now=now)


def position_in(control, pool: Pool):
    return next(item for item in control.census.positions if item.pool_id == pool.id)


# --------------------------------------------------------- official launch


async def test_the_official_launch_lock_is_not_a_holder(now) -> None:
    chain, pool, rows = official_launch()
    snapshot = await build(now, chain, rows=rows)
    control = snapshot.pool_control

    # Raw: the PoolManager looks like the dominant holder.
    assert snapshot.holders.top10_share > Decimal("0.95")
    assert snapshot.holders.top_holders[0].address == POOL_MANAGER

    position = position_in(control, pool)
    assert position.owner == OFFICIAL_SPLITTER
    assert position.control.control_state is PositionControlState.PERMANENTLY_LOCKED
    assert position.control.proof_version == "v3.3.0"

    # Economic: the locked principal is nobody's; only the buyers remain.
    assert control.status is Availability.AVAILABLE
    assert control.permanently_locked_pool_supply_fraction > Decimal("0.87")
    assert control.economic_top10_share < Decimal("0.11")
    holders = {item.holder for item in control.economic_top_holders}
    assert OFFICIAL_SPLITTER not in holders and POOL_MANAGER not in holders
    assert control.unknown_custody_raw == 0 and control.timelocked_raw == 0


async def test_the_official_launch_is_not_refused_for_holder_concentration(
    now, intent, context
) -> None:
    chain, _, rows = official_launch()
    snapshot = await build(now, chain, rows=rows)
    decision = evaluate_snapshot(snapshot, now, LIMITED)
    assert AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED not in decision.blockers
    assert not decision.data_gaps

    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    market, verdict = sentinel(now, payload, intent, context)
    assert (
        market.holders.top_ten_fraction == payload.intelligence.pool_control.economic_concentration
    )
    assert market.holders.concentration_check is SafetyStatus.PASS
    assert "HOLDER_CONCENTRATION_LIMIT" not in verdict.reason_codes
    assert "HOLDER_METRICS_UNKNOWN" not in verdict.reason_codes
    assert "HOLDERS_UNKNOWN" not in verdict.reason_codes


async def test_the_fee_beneficiary_is_not_credited_with_the_principal(now) -> None:
    """The creator is paid the launch's fees; it cannot move the principal."""
    chain, pool, rows = official_launch()
    # The beneficiary vault holds some token, as fee routes do.
    rows = (*rows, rows[-1].model_copy(update={"address": BENEFICIARY_VAULT, "balance_raw": 5}))
    control = (await build(now, chain, rows=rows)).pool_control
    assert control.status is Availability.AVAILABLE
    assert control.creator_controlled_raw == 0
    assert control.creator_controlled_pool_supply_fraction == 0
    assert CREATOR not in {item.holder for item in control.economic_top_holders}
    # The vault ranks with its own few tokens, nothing of the LP.
    assert all(
        item.balance_raw == 5
        for item in control.economic_top_holders
        if item.holder == BENEFICIARY_VAULT
    )
    assert position_in(control, pool).controller is None


async def test_the_same_launch_in_an_unverified_locker_fails_closed(now, intent, context) -> None:
    chain, pool, rows = official_launch(owner=UNKNOWN_LOCKER)
    chain.contracts.add(UNKNOWN_LOCKER)
    snapshot = await build(now, chain, rows=rows)
    control = snapshot.pool_control
    assert position_in(control, pool).control.control_state is (
        PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    )
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.POSITION_CONTROL_UNRESOLVED
    assert control.unknown_custody_pool_supply_fraction > Decimal("0.87")
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict is AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.V4_POSITION_CONTROL_UNRESOLVED in decision.data_gaps

    payload = onchain_payload(snapshot, decision)
    assert payload.holder_integrity == "UNKNOWN"
    _, verdict = sentinel(now, payload, intent, context)
    assert "HOLDERS_UNKNOWN" in verdict.reason_codes


# ---------------------------------------------------------- REVENUE intact


async def test_a_creator_position_stays_direct_control_beside_an_official_lock(now) -> None:
    chain, _, hidden = revenue_like("official")
    control = (await build(now, chain)).pool_control
    creator = position_in(control, hidden)
    assert creator.control.control_state is PositionControlState.DIRECT_CONTROL
    assert creator.control.controller == CREATOR
    assert control.creator_controlled_pool_supply_fraction == Decimal("0.574")
    assert control.economic_top_holders[0].holder == CREATOR
    assert control.economic_top10_share > Decimal("0.35")


async def test_a_creator_wrapper_never_turns_the_position_into_a_lock(now, intent, context) -> None:
    """Creator position → any contract in between → still not locked."""
    chain, _, hidden = revenue_like("official")
    chain.contracts.add(WRAPPER)
    chain.nft_owners[(POSITION_MANAGER, 3_498_775)] = WRAPPER
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    wrapped = position_in(control, hidden)
    assert wrapped.owner == WRAPPER
    assert wrapped.control.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert control.permanently_locked_pool_supply_fraction == Decimal("0.15")
    assert control.status is Availability.UNAVAILABLE
    assert control.economic_top10_share is None
    # The wrapped 57.4 % is in no figure at all, so the floor is low -- and
    # proves nothing, while the entry still fails closed.
    assert control.economic_top10_floor < Decimal("0.35")
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    assert not entry_concentration_established(payload)
    market, verdict = sentinel(now, payload, intent, context)
    assert market.holders.concentration_check is SafetyStatus.UNKNOWN
    assert "HOLDERS_UNKNOWN" in verdict.reason_codes
    assert verdict.outcome.value != "APPROVED"


async def test_a_creator_who_sends_its_own_position_to_the_official_splitter_gave_it_up(
    now,
) -> None:
    """Only verified code decides: the official splitter really can never return it."""
    chain, _, hidden = revenue_like("official")
    chain.nft_owners[(POSITION_MANAGER, 3_498_775)] = OFFICIAL_SPLITTER
    control = (await build(now, chain)).pool_control
    assert position_in(control, hidden).control.control_state is (
        PositionControlState.PERMANENTLY_LOCKED
    )
    assert control.creator_controlled_raw == 0
    assert control.permanently_locked_pool_supply_fraction == Decimal("0.724")


# --------------------------------------------------------------- timelocks


def timelocked_launch(timelock: int, operator: str = OPERATOR) -> tuple[FakeV4Chain, Pool]:
    chain, pool, _ = revenue_like("official")
    chain.codes[TIMELOCK_RECIPIENT] = timelocked_code(timelock=timelock, operator=operator)
    chain.nft_owners[(POSITION_MANAGER, 3_498_758)] = TIMELOCK_RECIPIENT
    return chain, pool


async def test_a_running_timelock_is_unresolved_never_safe(now, intent, context) -> None:
    chain, traded = timelocked_launch(chain_head := FakeV4Chain(token=TOKEN).head + 50)
    snapshot = await build(now, chain)
    control = snapshot.pool_control
    position = position_in(control, traded)
    assert position.control.control_state is PositionControlState.TIMELOCKED
    assert position.control.unlock_block == chain_head
    assert control.timelocked_pool_supply_fraction == Decimal("0.15")
    assert control.status is Availability.UNAVAILABLE
    assert control.gap is PoolControlGap.POSITION_CONTROL_UNRESOLVED
    # The creator's own position is established without it, and already too much.
    assert control.economic_top10_floor > Decimal("0.60")
    limited = evaluate_snapshot(snapshot, now, LIMITED)
    assert AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED in limited.blockers
    assert AtlasReasonCode.V4_POSITION_CONTROL_UNRESOLVED in limited.data_gaps


async def test_a_passed_timelock_is_the_operators_supply(now) -> None:
    chain, traded = timelocked_launch(1, operator=CREATOR)
    control = (await build(now, chain)).pool_control
    position = position_in(control, traded)
    assert position.control.control_state is PositionControlState.RELEASABLE
    assert position.control.controller == CREATOR
    assert control.status is Availability.AVAILABLE
    assert control.releasable_pool_supply_fraction == Decimal("0.15")
    # Released to the creator, it adds to the creator's own position.
    assert control.creator_controlled_pool_supply_fraction == Decimal("0.724")
    assert control.economic_top_holders[0].holder == CREATOR


async def test_a_passed_timelock_to_another_operator_is_theirs(now) -> None:
    chain, _ = timelocked_launch(1)
    control = (await build(now, chain)).pool_control
    ranked = {item.holder: item.balance_raw for item in control.economic_top_holders}
    assert ranked[OPERATOR] > 0
    assert TIMELOCK_RECIPIENT not in ranked


# --------------------------------------------------------- arbitrary pairs


async def test_a_meme_meme_position_in_the_official_splitter_is_locked_too(now) -> None:
    """Nothing about the lock depends on a native or stable quote."""
    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(
        Pool(currency0=TOKEN, currency1=MEME, fee=3000, tick_spacing=60, tick=-1_000)
    )
    chain.mint(pool, OFFICIAL_SPLITTER, 9, 0, 6_000, 10**24)
    control = (await build(now, chain)).pool_control
    position = position_in(control, pool)
    assert position.controlled_token_raw > 0
    assert position.control.control_state is PositionControlState.PERMANENTLY_LOCKED
    assert control.status is Availability.AVAILABLE


async def test_a_meme_meme_position_of_an_account_is_still_that_account(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    pool = chain.add_pool(
        Pool(currency0=TOKEN, currency1=MEME, fee=3000, tick_spacing=60, tick=-1_000)
    )
    owner = "0x" + "6a" * 20
    chain.mint(pool, owner, 9, 0, 6_000, 10**24)
    control = (await build(now, chain)).pool_control
    assert position_in(control, pool).controller == owner


async def test_a_native_token_position_in_either_orientation_resolves_alike(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(traded_pool())
    chain.mint(
        pool, OFFICIAL_SPLITTER, 1, -160_100, 198_050, liquidity_for(10**26, 190_000, -160_100)
    )
    control = (await build(now, chain)).pool_control
    assert position_in(control, pool).control.control_state is (
        PositionControlState.PERMANENTLY_LOCKED
    )


# ------------------------------------------------------------- evidence


async def test_evidence_records_raw_owner_and_interpreted_control(now) -> None:
    chain, pool, rows = official_launch()
    payload = await payload_for(now, chain, rows=rows)
    summary = payload.intelligence.pool_control
    record = next(item for item in summary.positions if item.pool_id == pool.id)
    assert record.owner == OFFICIAL_SPLITTER
    assert record.control.position_owner == OFFICIAL_SPLITTER
    assert record.control.control_state == "PERMANENTLY_LOCKED"
    assert record.control.proof_kind == "VERIFIED_CUSTODY_CODE"
    assert record.control.proof_contract == "uniswap-liquidity-launcher:FeeSplitter"
    assert record.control.proof_version == "v3.3.0"
    assert record.control.completeness == "VERIFIED"
    assert record.control.owner_code_hash is not None
    assert summary.position_control_states == {"PERMANENTLY_LOCKED": 1}
    assert summary.permanently_locked_pool_supply_fraction > Decimal("0.87")
    restored = OnchainPayload.model_validate_json(payload.model_dump_json())
    assert restored.model_dump_json() == payload.model_dump_json()


async def test_evidence_records_a_timelock_and_its_floor(now) -> None:
    chain, traded = timelocked_launch(FakeV4Chain(token=TOKEN).head + 50)
    payload = await payload_for(now, chain)
    summary = payload.intelligence.pool_control
    record = next(item for item in summary.positions if item.pool_id == traded.id)
    assert record.control.control_state == "TIMELOCKED"
    assert record.control.unlock_block == chain.head + 50
    assert summary.gap == "V4_POSITION_CONTROL_UNRESOLVED"
    assert summary.economic_concentration is None
    assert summary.economic_concentration_floor > Decimal("0.60")


def test_a_summary_written_before_control_facts_replays_byte_for_byte() -> None:
    legacy_position = {
        "kind": "POSITION_MANAGER",
        "pool_id": "0x" + "11" * 32,
        "owner_key": POSITION_MANAGER,
        "position_manager": POSITION_MANAGER,
        "token_id": "7",
        "tick_lower": -60,
        "tick_upper": 60,
        "liquidity": "1000",
        "owner": CREATOR,
        "owner_status": "ATTRIBUTED",
        "controlled_token_raw": "500",
        "owner_is_creator": True,
    }
    legacy = {
        "measurement": "V4_POOL_CONTROL",
        "status": "AVAILABLE",
        "gap": None,
        "failure": None,
        "required": True,
        "source": "evm-rpc-v4-pool-manager",
        "chain_id": 4663,
        "pool_manager": POOL_MANAGER,
        "position_managers": [POSITION_MANAGER],
        "scan_from_block": 1,
        "snapshot_block": 2,
        "total_supply_raw": "1000",
        "pool_manager_balance_raw": "500",
        "attributed_raw": "500",
        "unattributed_raw": "0",
        "creator_controlled_raw": "500",
        "pool_held_supply_fraction": "0.5",
        "attributable_pool_supply_fraction": "0.5",
        "creator_controlled_pool_supply_fraction": "0.5",
        "unattributed_pool_supply_fraction": "0",
        "pools_total": 0,
        "pools_with_hooks": 0,
        "creator_controlled_hooks": 0,
        "positions_total": 1,
        "positions_attributed": 1,
        "positions_unattributed": 0,
        "pools": [],
        "positions": [legacy_position],
        "raw_top_ten_fraction": "0.1",
        "basis": "EXACT",
        "economic_top_one_fraction": "0.5",
        "economic_top_five_fraction": "0.5",
        "economic_top_ten_fraction": "0.5",
        "economic_top_holders": [],
    }
    summary = PoolControlSummary.model_validate(legacy)
    assert summary.positions[0].control is None
    assert summary.economic_concentration == Decimal("0.5")
    assert summary.economic_concentration_floor is None
    assert summary.model_dump(mode="json") == legacy
    assert PoolControlPosition.model_validate(legacy_position).model_dump(mode="json") == (
        legacy_position
    )


async def test_the_digest_covers_the_control_state(now) -> None:
    locked_chain, _, rows = official_launch()
    locked = await build(now, locked_chain, rows=rows)
    unknown_chain, _, rows = official_launch(owner=UNKNOWN_LOCKER)
    unknown_chain.contracts.add(UNKNOWN_LOCKER)
    unknown = await build(now, unknown_chain, rows=rows)
    assert atlas_snapshot_digest(locked) != atlas_snapshot_digest(unknown)
    document = snapshot_document(locked)["pool_control"]
    (position,) = document["positions"]
    assert position["control"]["control_state"] == "PERMANENTLY_LOCKED"
    assert document["permanently_locked_raw"] == str(locked.pool_control.permanently_locked_raw)


async def test_the_digest_covers_the_unlock_block(now) -> None:
    head = FakeV4Chain(token=TOKEN).head
    early = await build(now, timelocked_launch(head + 50)[0])
    late = await build(now, timelocked_launch(head + 51)[0])
    assert atlas_snapshot_digest(early) != atlas_snapshot_digest(late)


# ------------------------------------------------------------- risk data


def v4_reader(sessions, now, pool: Pool):
    pair = v4_market(pool).pair_id
    return build_reader(sessions, now, feed=RecordedMarkets(recorded_snapshot(now, pair_id=pair)))


async def recorded(reader, now, trace, payload, pool: Pool):
    trade_case = await open_case(
        reader.cases, now, trace, key=f"control-{uuid4()}", identity=v4_market(pool)
    )
    await record_onchain(reader.cases, trade_case, now, payload)
    return trade_case


def concentration_gap(reading):
    return next(
        (item for item in reading.gaps if item.kind is RiskFactKind.HOLDER_CONCENTRATION), None
    )


async def test_risk_data_carries_the_official_launch_figure(worker_db, now, trace) -> None:
    _, sessions = worker_db
    chain, pool, rows = official_launch()
    reader = v4_reader(sessions, now, pool)
    payload = await payload_for(now, chain, rows=rows)
    reading = await reader.readiness((await recorded(reader, now, trace, payload, pool)).id)
    assert concentration_gap(reading) is None
    fact = next(item for item in reading.facts if item.kind is RiskFactKind.HOLDER_CONCENTRATION)
    assert fact is not None


async def test_risk_data_refuses_unresolved_custody(worker_db, now, trace) -> None:
    _, sessions = worker_db
    chain, pool, rows = official_launch(owner=UNKNOWN_LOCKER)
    chain.contracts.add(UNKNOWN_LOCKER)
    reader = v4_reader(sessions, now, pool)
    payload = (await payload_for(now, chain, rows=rows)).model_copy(
        update={"holder_integrity": "PASS"}
    )
    reading = await reader.readiness((await recorded(reader, now, trace, payload, pool)).id)
    assert concentration_gap(reading).code is RiskDataGapCode.ECONOMIC_CONCENTRATION_UNKNOWN
    assert not reading.complete


async def test_a_floor_never_passes_sentinel_even_on_a_forced_pass(now, intent, context) -> None:
    """Defence in depth: a floor below the limit can never stand in for the figure."""
    chain, _, rows = official_launch(owner=UNKNOWN_LOCKER)
    chain.contracts.add(UNKNOWN_LOCKER)
    payload = (await payload_for(now, chain, rows=rows)).model_copy(
        update={"holder_integrity": "PASS"}
    )
    floor = payload.intelligence.pool_control.economic_concentration_floor
    assert entry_concentration(payload, payload.intelligence.holders) == floor
    assert floor < Decimal("0.35")
    market, verdict = sentinel(now, payload, intent, context)
    assert market.holders.concentration_check is SafetyStatus.UNKNOWN
    assert "HOLDERS_UNKNOWN" in verdict.reason_codes


async def test_a_sale_keeps_its_raw_figure_whatever_the_custody(now, intent, context) -> None:
    chain, _, rows = official_launch(owner=UNKNOWN_LOCKER)
    chain.contracts.add(UNKNOWN_LOCKER)
    payload = (await payload_for(now, chain, rows=rows)).model_copy(
        update={"holder_integrity": "PASS"}
    )
    market, _ = sentinel(now, payload, intent, context, side=Side.SELL)
    assert market.holders.top_ten_fraction == payload.intelligence.holders.top_ten_fraction
    assert market.holders.concentration_check is SafetyStatus.PASS
