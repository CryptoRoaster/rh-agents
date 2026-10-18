"""Offline acceptance of the early PAPER entry path, end to end, without any provider.

One young token, from discovery to a simulated fill and back out, through the
production components: the discovery prefilter's market classification, the
ATLAS V4 census with the PoolManager-wide pool index and the remainder
credit, ATLAS's policy and payload, SENTINEL's market view and evaluation, and
the PAPER executor's own last check. Every chain answer comes from the
deterministic fixture chain; the fill is the executor's pure simulation, so no
PAPER-BUY is recorded anywhere.

The safe case passes. Each unsafe case must stop the purchase -- in SENTINEL
and again at the executor -- while a sale out of the same market still fills.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow, OriginVerification
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.pool_index import PoolIndexHistory, V4PoolIndexer
from src.core.models import (
    AgentRole,
    LpControlStatus,
    MarketSnapshot,
    OrderIntent,
    RiskDecision,
    RiskLimits,
    RiskOutcome,
    Side,
)
from src.execution.paper import PaperExecutor
from src.markets.models import Availability, PoolLocator, PoolLocatorKind
from src.orchestration.riskrequest.service import risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType, OnchainPayload
from src.risk.engine import evaluate
from src.scout.repository import V4_PAIR
from tests.anchor.conftest import evidence_envelope
from tests.atlas.conftest import CREATOR
from tests.atlas.v4.chain import HEAD_BLOCK, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.custody import OFFICIAL_SPLITTER
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import build, deployment, official_launch, origin, v4_market
from tests.riskdata.conftest import (
    BASE_ASSET,
    anchor_payload,
    configured_costs,
    recorded_snapshot,
)

CORRELATION = UUID("00000000-0000-4000-8000-0000000ea21e")
GROWTH = 1 << 120
UNVERIFIED_VAULT = "0x" + "7b" * 20
FOREIGN_MANAGER = "0x" + "11" * 20


@dataclass(frozen=True)
class Token:
    chain: FakeV4Chain
    pool: Pool


def young_token(owner: str = OFFICIAL_SPLITTER, configure=None) -> Token:
    """An official InstantLaunch-style token minutes old, its launch NFT in ``owner``."""
    chain, pool, _ = official_launch(owner=owner)
    chain.extra_pool_balance = 13  # rounding dust and the like: beyond every liability
    chain.manager_deployed_block = HEAD_BLOCK - 350_000
    if owner not in (OFFICIAL_SPLITTER, CREATOR):
        chain.contracts.add(owner)
    if configure is not None:
        configure(chain, pool)
    return Token(chain, pool)


async def atlas(
    now,
    token: Token,
    *,
    indexed: bool = True,
    index_budget: int = 200,
    census_age: timedelta | None = None,
    verified_creator: bool = True,
) -> OnchainPayload:
    """ATLAS's evidence for the token: census, index, credit, policy, payload."""
    store = MemoryPoolIndexStore()
    if indexed:
        await V4PoolIndexer(
            reads=token.chain,
            store=store,
            deployment=deployment(),
            confirmations=20,
            max_requests=index_budget,
        ).advance()
    _, _, launch_rows = official_launch()
    rows = (
        HolderSourceRow(
            address=POOL_MANAGER, balance_raw=token.chain.pool_balance(token.chain.head)
        ),
        *launch_rows[1:],
    )
    snapshot = await build(
        now,
        token.chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
        index=PoolIndexHistory(store),
        block_timestamp=None if census_age is None else now - census_age,
        origin_facts=origin(
            verification=(
                OriginVerification.RECEIPT_CONFIRMED
                if verified_creator
                else OriginVerification.UNVERIFIED
            )
        ),
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now))


def recorded(
    now, pool_id: str, *, manager: str | None = None, kind=PoolLocatorKind.BYTES32_POOL_ID
):
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    locator = PoolLocator(
        kind=kind,
        value=pool_id,
        venue="uniswap-v4-robinhood",
        pool_manager_address=manager,
        manager_status=Availability.AVAILABLE if manager else Availability.UNKNOWN,
    )
    pair = snapshot.pair.model_copy(update={"pool_locator": locator})
    return snapshot.model_copy(update={"pair": pair})


def market(now, payload: OnchainPayload, snapshot, side: Side) -> MarketSnapshot:
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    return risk_market(
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
        identity_key=f"acceptance:{side.value}",
        side=side,
    )


def decide(now, intent, context, view: MarketSnapshot, side: Side) -> tuple:
    order = intent.model_copy(
        update={
            "asset_id": BASE_ASSET,
            "side": side,
            "signal_price": Decimal("1.25"),
            "quantity": Decimal("10"),
            "correlation_id": CORRELATION,
        }
    )
    held = (
        context
        if side is Side.BUY
        else context.model_copy(update={"position_quantity": Decimal("10")})
    )
    return order, evaluate(order, view, held, RiskLimits(), now=now)


def order_for(order, decision: RiskDecision, now) -> OrderIntent:
    return OrderIntent(
        source="acceptance",
        correlation_id=CORRELATION,
        intent=order,
        risk=decision,
        execution_requested_at=now,
    )


def forged(order, view: MarketSnapshot, now) -> OrderIntent:
    """An approval the engine never gave, bound to this exact market."""
    decision = RiskDecision(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        source="FORGED",
        correlation_id=CORRELATION,
        intent_id=order.id,
        intent_fingerprint=order.fingerprint(),
        market_snapshot_id=view.id,
        market_fingerprint=view.fingerprint(),
        outcome=RiskOutcome.APPROVE,
        reason_codes=("WITHIN_LIMITS",),
        max_notional_usd=Decimal("1000"),
        max_slippage_bps=Decimal("100"),
        evaluated_at=now,
        expires_at=now + timedelta(seconds=30),
        policy_version="forged",
    )
    return order_for(order, decision, now)


async def refused_buy_but_exit_fills(now, intent, context, payload, snapshot, detail, status):
    view = market(now, payload, snapshot, Side.BUY)
    assert view.lp_control is not None
    assert (view.lp_control.status, view.lp_control.detail) == (status, detail)
    order, decision = decide(now, intent, context, view, Side.BUY)
    assert decision.outcome is RiskOutcome.REJECT
    assert any(code.startswith("LP_CONTROL_") for code in decision.reason_codes)
    # The executor refuses the purchase even with an approval it never saw.
    with pytest.raises(ValueError):
        await PaperExecutor().execute(forged(order, view, now), view)
    # A sale out of the same market is never held to LP control.
    sale_view = market(now, payload, snapshot, Side.SELL)
    assert sale_view.lp_control is None
    sale, sale_decision = decide(now, intent, context, sale_view, Side.SELL)
    assert sale_decision.outcome is RiskOutcome.APPROVE, sale_decision.reason_codes
    fill = await PaperExecutor().execute(order_for(sale, sale_decision, now), sale_view)
    assert fill.side is Side.SELL


# ------------------------------------------------------------------ discovery


def test_discovery_sends_only_v4_markets_to_atlas_and_keeps_every_observation() -> None:
    token = young_token()
    v4 = v4_market(token.pool)
    assert v4.pool_locator is not None and v4.pool_locator.kind is PoolLocatorKind.BYTES32_POOL_ID
    pattern = V4_PAIR.strip("%")
    assert pattern in v4.pair_id
    v2_pair = "robinhood:mainnet:contract_address:0x" + "ab" * 20
    assert pattern not in v2_pair


# ------------------------------------------------------------------ the safe case


async def test_a_safe_young_v4_token_passes_and_fills(now, intent, context) -> None:
    token = young_token()
    payload = await atlas(now, token)
    control = payload.intelligence.pool_control  # type: ignore[union-attr]
    assert control is not None
    assert control.pool_discovery_basis == "MANAGER_HISTORY"
    assert control.remainder_credit_status == "COMPLETE"
    view = market(now, payload, recorded(now, token.pool.id), Side.BUY)
    assert view.lp_control is not None and view.lp_control.status is LpControlStatus.PASS
    order, decision = decide(now, intent, context, view, Side.BUY)
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes
    fill = await PaperExecutor().execute(order_for(order, decision, now), view)
    assert fill.side is Side.BUY and fill.quantity == Decimal("10")


# ------------------------------------------------------------------ unsafe cases


async def test_unknown_fees_stop_the_purchase(now, intent, context) -> None:
    token = young_token(configure=lambda chain, pool: chain.accrue(pool, GROWTH))
    payload = await atlas(now, token)
    await refused_buy_but_exit_fills(
        now,
        intent,
        context,
        payload,
        recorded(now, token.pool.id),
        "V4_POOL_FEES_NOT_CREDITED",
        LpControlStatus.UNKNOWN,
    )


async def test_creator_controlled_liquidity_is_blocked(now, intent, context) -> None:
    token = young_token(owner=CREATOR)
    payload = await atlas(now, token)
    await refused_buy_but_exit_fills(
        now,
        intent,
        context,
        payload,
        recorded(now, token.pool.id),
        "V4_CREATOR_CAN_WITHDRAW",
        LpControlStatus.BLOCKED,
    )


async def test_an_incomplete_pool_index_leaves_the_surplus_unexplained(
    now, intent, context
) -> None:
    token = young_token()
    payload = await atlas(now, token, index_budget=34)  # stops before the token's block
    await refused_buy_but_exit_fills(
        now,
        intent,
        context,
        payload,
        recorded(now, token.pool.id),
        "V4_POOL_SURPLUS_UNEXPLAINED",
        LpControlStatus.UNKNOWN,
    )


async def test_a_stale_census_stops_the_purchase(now, intent, context) -> None:
    token = young_token()
    payload = await atlas(now, token, census_age=timedelta(minutes=4))
    view = market(now, payload, recorded(now, token.pool.id), Side.BUY)
    assert view.lp_control is not None and view.lp_control.status is LpControlStatus.PASS
    order, decision = decide(now, intent, context, view, Side.BUY)
    assert decision.outcome is RiskOutcome.REJECT
    assert "STALE_OR_FUTURE_SAFETY_DATA" in decision.reason_codes


async def test_a_foreign_pool_manager_stops_the_purchase(now, intent, context) -> None:
    token = young_token()
    payload = await atlas(now, token)
    await refused_buy_but_exit_fills(
        now,
        intent,
        context,
        payload,
        recorded(now, token.pool.id, manager=FOREIGN_MANAGER),
        "V4_MARKET_POOL_MANAGER_MISMATCH",
        LpControlStatus.UNKNOWN,
    )


async def test_a_v2_v3_market_without_lp_evidence_is_unsupported(now, intent, context) -> None:
    token = young_token()
    payload = await atlas(now, token)
    pair = "0x" + "ab" * 20
    await refused_buy_but_exit_fills(
        now,
        intent,
        context,
        payload,
        recorded(now, pair, kind=PoolLocatorKind.CONTRACT_ADDRESS),
        "LP_CONTROL_SOURCE_ABSENT",
        LpControlStatus.UNSUPPORTED,
    )


async def test_unknown_custody_stops_the_purchase(now, intent, context) -> None:
    token = young_token(owner=UNVERIFIED_VAULT)
    payload = await atlas(now, token)
    view = market(now, payload, recorded(now, token.pool.id), Side.BUY)
    assert view.lp_control is not None and view.lp_control.status is LpControlStatus.UNKNOWN
    _, decision = decide(now, intent, context, view, Side.BUY)
    assert decision.outcome is RiskOutcome.REJECT


# ------------------------------------------------------------------ approvals


async def test_a_reused_approval_cannot_fill_another_market(now, intent, context) -> None:
    safe = young_token()
    payload = await atlas(now, safe)
    view = market(now, payload, recorded(now, safe.pool.id), Side.BUY)
    order, decision = decide(now, intent, context, view, Side.BUY)
    assert decision.outcome is RiskOutcome.APPROVE
    approved = order_for(order, decision, now)

    # The same approval against the market as it reads once the creator holds
    # the launch: another fingerprint, so no fill.
    risky = await atlas(now, young_token(owner=CREATOR))
    changed = market(now, risky, recorded(now, safe.pool.id), Side.BUY)
    with pytest.raises(ValueError, match="changed after approval"):
        await PaperExecutor().execute(approved, changed)
    # And an approval recorded before LP control existed fills no purchase.
    legacy = view.model_copy(update={"lp_control": None})
    with pytest.raises(ValueError):
        await PaperExecutor().execute(forged(order, legacy, now), legacy)


# ------------------------------------------------------------------ FeeSplitter


async def test_eth_side_fees_of_the_locked_launch_do_not_block(now, intent, context) -> None:
    # Fees in the quote currency are neither token supply nor principal: the
    # FeeSplitter routes them, and they change nobody's control of liquidity.
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        side = chain.side(pool)
        pair = [0, 0]
        pair[1 - side] = GROWTH
        chain.fee_growth_global[pool.id] = (pair[0], pair[1])

    token = young_token(configure=configure)
    payload = await atlas(now, token)
    view = market(now, payload, recorded(now, token.pool.id), Side.BUY)
    assert view.lp_control is not None
    assert view.lp_control.status is LpControlStatus.PASS, view.lp_control.detail


@pytest.mark.parametrize(
    "splitter",
    [
        "0x9411fa7f956f64aa7981aa27cb3bc6ec0415449c",
        "0x882ae5e2095435a62fd1bbdefcb637f5ceafc0ee",
        "0xeff166aaf189323c58dc27ed1206eb2c37faacdf",
        "0x222d6d4f1ce59b0d48d5505114ec8addc90a4359",
    ],
)
def test_no_registered_fee_splitter_names_its_token_fee_recipient(splitter) -> None:
    # Token-side fees of a locked launch stay uncredited until a recipient is
    # verified (issue #103): the registry carries none, for every deployment.
    from src.agents.atlas.v4.custody.uniswap_launcher import REGISTRIES

    (deployment_entry,) = [
        item for item in REGISTRIES["robinhood"].fee_splitters if item.address == splitter
    ]
    assert not hasattr(deployment_entry, "token_fee_recipient")
