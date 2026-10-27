"""Issue #92: LP control is a BUY requirement, fail-closed, and never a SELL's.

The engine half judges SENTINEL's snapshot as given -- a missing field, a legacy
snapshot, a forged PASS. The market-view half derives the fact from recorded
evidence, the only place it is produced: a 20-byte pool is UNSUPPORTED, a V4
pool passes only on a complete census of its own pool without a hook.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.models import (
    LP_CONTROL_V4_POOL_CENSUS,
    AgentRole,
    LpControlStatus,
    MarketSnapshot,
    OrderIntent,
    RiskLimits,
    RiskOutcome,
    Side,
)
from src.execution.paper import PaperExecutor
from src.markets.models import PoolLocator, PoolLocatorKind
from src.orchestration.riskrequest.service import lp_control_view, risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType, OnchainPayload
from src.risk.engine import evaluate, lp_control_refusal
from tests.anchor.conftest import evidence_envelope
from tests.atlas.v4.scenarios import CREATOR_HOOK, build, locked_only_launch, revenue_like
from tests.riskdata.conftest import (
    BASE_ASSET,
    LIQUIDITY,
    PRICE,
    anchor_payload,
    configured_costs,
    onchain_payload,
    recorded_snapshot,
)

CORRELATION = UUID("00000000-0000-4000-8000-00000000092b")
OTHER_POOL = "0x" + "cd" * 32


def without_lp_control(market: MarketSnapshot) -> MarketSnapshot:
    return market.model_copy(update={"lp_control": None})


def with_lp(market: MarketSnapshot, **changes: object) -> MarketSnapshot:
    assert market.lp_control is not None
    return market.model_copy(update={"lp_control": market.lp_control.model_copy(update=changes)})


# ------------------------------------------------------------------ SENTINEL


def test_the_fixture_market_passes_on_its_verified_census(intent, market, context, now) -> None:
    decision = evaluate(intent, market, context, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.APPROVE
    assert lp_control_refusal(market) is None


def test_a_missing_field_is_unknown_and_refuses_a_buy(intent, market, context, now) -> None:
    decision = evaluate(intent, without_lp_control(market), context, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.REJECT
    assert decision.reason_codes == ("LP_CONTROL_UNKNOWN",)
    assert decision.max_additional_notional_usd == 0


def test_a_legacy_snapshot_reads_as_unknown_and_keeps_its_fingerprint(
    intent, market, context, now
) -> None:
    legacy = without_lp_control(market)
    recorded = legacy.model_dump(mode="json")
    # A snapshot stored before issue #92 has no such key at all, and one without
    # the evidence is written in exactly that shape: same bytes, same digest.
    assert "lp_control" not in recorded
    replayed = MarketSnapshot.model_validate_json(json.dumps(recorded))
    assert replayed.lp_control is None
    assert replayed.fingerprint() == legacy.fingerprint()
    decision = evaluate(intent, replayed, context, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


@pytest.mark.parametrize(
    "status", [LpControlStatus.UNKNOWN, LpControlStatus.UNSUPPORTED, LpControlStatus.BLOCKED]
)
def test_every_status_but_pass_refuses_a_buy(intent, market, context, now, status) -> None:
    decision = evaluate(intent, with_lp(market, status=status), context, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.REJECT
    assert decision.reason_codes == (f"LP_CONTROL_{status.value}",)


@pytest.mark.parametrize(
    "forgery",
    [
        {"pool_locator_kind": "CONTRACT_ADDRESS"},
        {"pool_locator_kind": None},
        {"pool_id": None},
        {"basis": None},
        {"basis": "HOLDERS_PASS"},
        {"census_block": None},
    ],
)
def test_a_pass_without_its_verified_basis_is_not_evidence(
    intent, market, context, now, forgery
) -> None:
    forged = with_lp(market, **forgery)
    assert forged.lp_control is not None and forged.lp_control.status is LpControlStatus.PASS
    decision = evaluate(intent, forged, context, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.REJECT
    assert decision.reason_codes == ("LP_CONTROL_EVIDENCE_INCONSISTENT",)


def test_positive_holder_evidence_does_not_stand_in(intent, market, context, now) -> None:
    unknown = without_lp_control(market)
    assert unknown.holders.concentration_check.value == "PASS"
    assert unknown.holders.top_ten_fraction == Decimal("0.1")
    decision = evaluate(intent, unknown, context, RiskLimits(), now=now)
    assert decision.reason_codes == ("LP_CONTROL_UNKNOWN",)


def test_stale_lp_control_evidence_is_stale_safety_data(intent, market, context, now) -> None:
    old = with_lp(market, created_at=now - timedelta(seconds=31))
    decision = evaluate(intent, old, context, RiskLimits(), now=now)
    assert "STALE_OR_FUTURE_SAFETY_DATA" in decision.reason_codes


@pytest.mark.parametrize("lp", [None, LpControlStatus.UNSUPPORTED, LpControlStatus.BLOCKED])
def test_a_sale_is_never_held_to_lp_control(intent, market, context, now, lp) -> None:
    sale = intent.model_copy(update={"side": Side.SELL})
    held = context.model_copy(update={"position_quantity": Decimal("2")})
    view = without_lp_control(market) if lp is None else with_lp(market, status=lp)
    decision = evaluate(sale, view, held, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.APPROVE
    assert decision.reason_codes == ("WITHIN_LIMITS",)


def test_stale_lp_control_evidence_never_holds_a_sale(intent, market, context, now) -> None:
    sale = intent.model_copy(update={"side": Side.SELL})
    held = context.model_copy(update={"position_quantity": Decimal("2")})
    old = with_lp(market, created_at=now - timedelta(days=1), status=LpControlStatus.BLOCKED)
    decision = evaluate(sale, old, held, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.APPROVE
    assert decision.reason_codes == ("WITHIN_LIMITS",)


def test_the_holder_limit_is_unchanged(intent, market, context, now) -> None:
    assert RiskLimits().max_top_ten_holder_fraction == Decimal("0.35")
    concentrated = market.model_copy(
        update={"holders": market.holders.model_copy(update={"top_ten_fraction": Decimal("0.36")})}
    )
    decision = evaluate(intent, concentrated, context, RiskLimits(), now=now)
    assert decision.reason_codes == ("HOLDER_CONCENTRATION_LIMIT",)
    at_limit = market.model_copy(
        update={"holders": market.holders.model_copy(update={"top_ten_fraction": Decimal("0.35")})}
    )
    assert evaluate(intent, at_limit, context, RiskLimits(), now=now).outcome is RiskOutcome.APPROVE


# ------------------------------------------------------------------ executor


def forged_order(intent, market, context, now, unproven: MarketSnapshot) -> OrderIntent:
    """An approval that names the unproven market as if SENTINEL had passed it."""
    approved = evaluate(intent, market, context, RiskLimits(), now=now)
    assert approved.outcome is RiskOutcome.APPROVE
    risk = approved.model_copy(
        update={"market_snapshot_id": unproven.id, "market_fingerprint": unproven.fingerprint()}
    )
    return OrderIntent(
        source="test",
        correlation_id=intent.correlation_id,
        intent=intent,
        risk=risk,
        execution_requested_at=now,
    )


@pytest.mark.parametrize("variant", ["missing", "UNKNOWN", "UNSUPPORTED", "BLOCKED", "forged-pass"])
async def test_the_executor_refuses_a_buy_without_lp_control(
    intent, market, context, now, variant
) -> None:
    unproven = (
        without_lp_control(market)
        if variant == "missing"
        else with_lp(market, basis=None)
        if variant == "forged-pass"
        else with_lp(market, status=LpControlStatus(variant))
    )
    order = forged_order(intent, market, context, now, unproven)
    with pytest.raises(ValueError, match="established LP control"):
        await PaperExecutor().execute(order, unproven)


async def test_the_executor_fills_a_sale_without_lp_control(intent, market, context, now) -> None:
    sale = intent.model_copy(update={"side": Side.SELL})
    held = context.model_copy(update={"position_quantity": Decimal("2")})
    view = without_lp_control(market)
    risk = evaluate(sale, view, held, RiskLimits(), now=now)
    order = OrderIntent(
        source="test",
        correlation_id=sale.correlation_id,
        intent=sale,
        risk=risk,
        execution_requested_at=now,
    )
    fill = await PaperExecutor().execute(order, view)
    assert fill.side is Side.SELL


# --------------------------------------------------------------- market view


def recorded(now, *, pool_id: str | None = None, liquidity: Decimal = LIQUIDITY):
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5), liquidity=liquidity)
    if pool_id is None:
        return snapshot
    locator = PoolLocator(kind=PoolLocatorKind.BYTES32_POOL_ID, value=pool_id, venue="uniswap-v4")
    pair = snapshot.pair.model_copy(update={"pool_locator": locator, "venue": "uniswap-v4"})
    return snapshot.model_copy(update={"pair": pair})


def sentinel(now, intent, context, payload: OnchainPayload, *, pool_id: str | None):
    # A pool of a fifth of the evidence's own FDV, so SENTINEL's liquidity/FDV
    # bar holds whichever supply the payload carries: the V4 scenarios' billion
    # tokens or the risk-data fixture's million.
    holders = None if payload.intelligence is None else payload.intelligence.holders
    supply = None if holders is None else holders.total_supply_raw
    liquidity = LIQUIDITY if supply is None else Decimal(supply) / 10**18 * PRICE / 5
    snapshot = recorded(now, pool_id=pool_id, liquidity=liquidity)
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
        identity_key=f"issue-92-v4:{pool_id}",
        side=Side.BUY,
    )
    order = intent.model_copy(
        update={"asset_id": BASE_ASSET, "signal_price": Decimal("1.25"), "quantity": Decimal("10")}
    )
    return market, evaluate(order, market, context, RiskLimits(), now=now)


async def official_payload(now) -> tuple[OnchainPayload, str]:
    from src.agents.atlas.handler import onchain_payload as atlas_payload
    from src.agents.atlas.models import HolderCompleteness
    from src.agents.atlas.policy import evaluate_snapshot

    chain, pool, supply, rows = locked_only_launch()
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=supply,
        holder_count=1,
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now)), pool.id


async def test_a_verified_v4_pool_keeps_its_approval(now, intent, context) -> None:
    payload, pool_id = await official_payload(now)
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS
    assert market.lp_control.basis == LP_CONTROL_V4_POOL_CENSUS
    assert market.lp_control.pool_id == pool_id
    assert market.lp_control.census_block is not None
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes


async def test_a_v4_census_never_passes_a_twenty_byte_market(now, intent, context) -> None:
    # The token's V4 census is complete, but the market traded is a 20-byte
    # pool whose own liquidity nobody read.
    payload, _ = await official_payload(now)
    market, decision = sentinel(now, intent, context, payload, pool_id=None)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.UNSUPPORTED
    assert decision.reason_codes == ("LP_CONTROL_UNSUPPORTED",)


async def test_a_pool_id_alone_never_passes(now, intent, context) -> None:
    payload, _ = await official_payload(now)
    # A 32-byte market whose pool the census did not list.
    _, decision = sentinel(now, intent, context, payload, pool_id=OTHER_POOL)
    assert decision.reason_codes == ("LP_CONTROL_UNKNOWN",)
    # And one with no census at all.
    _, decision = sentinel(now, intent, context, onchain_payload(now), pool_id=OTHER_POOL)
    assert decision.reason_codes == ("LP_CONTROL_UNKNOWN",)


async def test_unresolved_v4_custody_refuses_a_buy(now, intent, context) -> None:
    from src.agents.atlas.handler import onchain_payload as atlas_payload
    from src.agents.atlas.models import HolderCompleteness
    from src.agents.atlas.policy import evaluate_snapshot

    # The launch position NFT sits in a contract nobody verified.
    chain, traded, _hidden = revenue_like("unverified")
    snapshot = await build(now, chain, completeness=HolderCompleteness.COMPLETE)
    payload = atlas_payload(snapshot, evaluate_snapshot(snapshot, now))
    market, decision = sentinel(now, intent, context, payload, pool_id=traded.id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.UNKNOWN
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


def hooked(payload: OnchainPayload, hook: str, *, creator: bool | None) -> OnchainPayload:
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    control = payload.intelligence.pool_control
    pools = tuple(
        pool.model_copy(update={"hook": hook, "hook_owner_is_creator": creator})
        for pool in control.pools
    )
    intelligence = payload.intelligence.model_copy(
        update={"pool_control": control.model_copy(update={"pools": pools})}
    )
    return payload.model_copy(update={"intelligence": intelligence})


async def test_an_unverified_hook_is_unknown_and_a_creator_hook_blocks(now) -> None:
    payload, pool_id = await official_payload(now)
    snapshot = recorded(now, pool_id=pool_id)
    assert lp_control_view(snapshot=snapshot, onchain=payload)[0] is LpControlStatus.PASS
    unverified = hooked(payload, CREATOR_HOOK, creator=None)
    assert lp_control_view(snapshot=snapshot, onchain=unverified)[:2] == (
        LpControlStatus.UNKNOWN,
        "V4_HOOK_UNVERIFIED",
    )
    foreign = hooked(payload, CREATOR_HOOK, creator=False)
    assert lp_control_view(snapshot=snapshot, onchain=foreign)[0] is LpControlStatus.UNKNOWN
    creator = hooked(payload, CREATOR_HOOK, creator=True)
    assert lp_control_view(snapshot=snapshot, onchain=creator)[:2] == (
        LpControlStatus.BLOCKED,
        "V4_HOOK_CREATOR_CONTROLLED",
    )


def test_a_market_without_a_locator_is_unknown(now) -> None:
    snapshot = recorded_snapshot(now, quote_price="legacy")
    assert snapshot.pair.pool_locator is None
    assert lp_control_view(snapshot=snapshot, onchain=onchain_payload(now))[0] is (
        LpControlStatus.UNKNOWN
    )


def test_a_sale_market_view_carries_no_lp_control(now) -> None:
    snapshot = recorded(now)
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    market = risk_market(
        base_asset_id=BASE_ASSET,
        price=price,
        base_asset=metadata,
        snapshot=snapshot,
        onchain=evidence_envelope(now, EvidenceType.ONCHAIN, AgentRole.ATLAS, onchain_payload(now)),
        anchor=evidence_envelope(
            now,
            EvidenceType.LIQUIDITY_EXECUTION,
            AgentRole.ANCHOR,
            anchor_payload(uuid4(), uuid4()),
        ),
        costs=configured_costs(),
        correlation_id=CORRELATION,
        identity_key="issue-92-sale",
        side=Side.SELL,
    )
    assert market.lp_control is None
    assert "lp_control" not in market.model_dump(mode="json")
