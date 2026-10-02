"""The REVENUE mechanism, end to end: ATLAS → durable evidence → risk data → SENTINEL.

A pre-entry snapshot of a token whose raw top ten is ~9.5 % while its creator
controls ~57.4 % of supply through a single-sided position in a never-traded
second V4 pool. The entry must be rejected by SENTINEL's existing 35 % holder
concentration limit — not by symbol, price, market cap or any address — and a
V4 token whose economic figure is unknown must never pass on its raw one.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import AgentRole, RiskLimits, Side
from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactKind
from src.orchestration.riskrequest.service import entry_concentration, risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType, OnchainIntelligence, OnchainPayload
from src.risk.engine import evaluate
from tests.anchor.conftest import evidence_envelope
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.chain import FakeV4Chain
from tests.atlas.v4.scenarios import build, revenue_like, v4_market
from tests.atlas.v4.test_economic import spread_pool
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

CORRELATION = UUID("00000000-0000-4000-8000-0000000000aa")


async def atlas_payload(now, chain) -> OnchainPayload:
    snapshot = await build(now, chain)
    return onchain_payload(snapshot, evaluate_snapshot(snapshot, now))


def sentinel_market(now, payload: OnchainPayload, side: Side = Side.BUY):
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
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
        identity_key="revenue-regression",
        side=side,
    )


def buy(intent):
    return intent.model_copy(
        update={"asset_id": BASE_ASSET, "signal_price": Decimal("1.25"), "quantity": Decimal("10")}
    )


async def test_sentinel_rejects_the_revenue_like_entry_on_holder_concentration(
    now, intent, context
) -> None:
    payload = await atlas_payload(now, revenue_like()[0])
    raw = payload.intelligence.holders.top_ten_fraction
    economic = payload.intelligence.pool_control.economic_concentration
    assert raw < Decimal("0.10") and economic > Decimal("0.60")

    market = sentinel_market(now, payload)
    assert market.holders.top_ten_fraction == economic

    decision = evaluate(buy(intent), market, context, RiskLimits(), now=now)
    assert "HOLDER_CONCENTRATION_LIMIT" in decision.reason_codes
    assert RiskLimits().max_top_ten_holder_fraction == Decimal("0.35")


async def test_the_same_token_judged_on_its_raw_figure_would_have_passed_that_check(
    now, intent, context
) -> None:
    """What the blind spot looked like: the limit never fired on raw holders."""
    payload = await atlas_payload(now, revenue_like()[0])
    intelligence = payload.intelligence.model_copy(update={"pool_control": None})
    blind = payload.model_copy(update={"intelligence": intelligence})
    decision = evaluate(buy(intent), sentinel_market(now, blind), context, RiskLimits(), now=now)
    assert "HOLDER_CONCENTRATION_LIMIT" not in decision.reason_codes


async def test_a_normal_v4_token_is_not_rejected_on_concentration(now, intent, context) -> None:
    chain = FakeV4Chain(token=TOKEN)
    spread_pool(chain)
    payload = await atlas_payload(now, chain)
    decision = evaluate(buy(intent), sentinel_market(now, payload), context, RiskLimits(), now=now)
    assert "HOLDER_CONCENTRATION_LIMIT" not in decision.reason_codes
    assert "HOLDER_METRICS_UNKNOWN" not in decision.reason_codes


async def test_an_unknown_economic_figure_never_falls_back_to_raw(now, intent, context) -> None:
    chain, _, _ = revenue_like()
    chain.fail_at = 9
    payload = await atlas_payload(now, chain)
    assert entry_concentration(payload, payload.intelligence.holders) is None
    # ATLAS already marks the holder domain unestablished; even a payload that
    # claimed otherwise could not pass SENTINEL on the raw figure.
    forced = payload.model_copy(update={"holder_integrity": "PASS"})
    decision = evaluate(buy(intent), sentinel_market(now, forced), context, RiskLimits(), now=now)
    assert "HOLDER_METRICS_UNKNOWN" in decision.reason_codes


async def test_a_sale_is_never_trapped_by_a_missing_census(now) -> None:
    chain, _, _ = revenue_like()
    chain.fail_at = 9
    payload = (await atlas_payload(now, chain)).model_copy(update={"holder_integrity": "PASS"})
    market = sentinel_market(now, payload, side=Side.SELL)
    assert market.holders.top_ten_fraction == payload.intelligence.holders.top_ten_fraction


# ---------------------------------------------------------------- risk data


async def v4_case(reader, now, trace, payload):
    chain, traded, _ = revenue_like()
    trade_case = await open_case(
        reader.cases, now, trace, key=f"v4-{uuid4()}", identity=v4_market(traded)
    )
    await record_onchain(reader.cases, trade_case, now, payload)
    return trade_case


def v4_reader(sessions, now):
    chain, traded, _ = revenue_like()
    pair = v4_market(traded).pair_id
    return build_reader(sessions, now, feed=RecordedMarkets(recorded_snapshot(now, pair_id=pair)))


def concentration_gap(reading):
    return next(
        (item for item in reading.gaps if item.kind is RiskFactKind.HOLDER_CONCENTRATION), None
    )


async def test_risk_data_carries_the_established_economic_figure(worker_db, now, trace) -> None:
    _, sessions = worker_db
    reader = v4_reader(sessions, now)
    payload = await atlas_payload(now, revenue_like()[0])
    trade_case = await v4_case(reader, now, trace, payload)

    reading = await reader.readiness(trade_case.id)
    assert concentration_gap(reading) is None
    assert any(item.kind is RiskFactKind.HOLDER_CONCENTRATION for item in reading.facts)


async def test_risk_data_refuses_a_v4_case_with_unknown_economics(worker_db, now, trace) -> None:
    _, sessions = worker_db
    reader = v4_reader(sessions, now)
    chain, _, _ = revenue_like()
    chain.fail_at = 9
    payload = await atlas_payload(now, chain)
    # Forced to a PASS verdict so that only the concentration rule can refuse.
    forced = payload.model_copy(update={"holder_integrity": "PASS"})
    trade_case = await v4_case(reader, now, trace, forced)

    reading = await reader.readiness(trade_case.id)
    assert concentration_gap(reading).code is RiskDataGapCode.ECONOMIC_CONCENTRATION_UNKNOWN
    assert not reading.complete


async def test_legacy_evidence_for_a_v4_case_is_readable_and_insufficient(
    worker_db, now, trace
) -> None:
    """Evidence written before pool control existed still parses — and cannot pass."""
    _, sessions = worker_db
    reader = v4_reader(sessions, now)
    payload = await atlas_payload(now, revenue_like()[0])
    legacy = payload.model_copy(
        update={
            "intelligence": OnchainIntelligence.model_validate(
                {
                    key: value
                    for key, value in payload.intelligence.model_dump(mode="json").items()
                    if key != "pool_control"
                }
            )
        }
    )
    assert "pool_control" not in legacy.model_dump_json()
    trade_case = await v4_case(reader, now, trace, legacy)

    reading = await reader.readiness(trade_case.id)
    assert concentration_gap(reading).code is RiskDataGapCode.ECONOMIC_CONCENTRATION_UNKNOWN


async def test_an_exit_read_never_makes_a_v4_position_unsellable(now) -> None:
    """The census only an entry needs cannot hold a sale hostage."""
    from src.core.clock import FixedClock
    from src.orchestration.paperexit.exitread import AtlasExitRead
    from tests.atlas.v4.scenarios import builder

    chain, traded, _ = revenue_like()
    reader = AtlasExitRead(builder=builder(now, None), clock=FixedClock(now))
    read = await reader.read(uuid4(), v4_market(traded), "exit-key")
    assert read.payload.holder_integrity == "PASS"
    assert read.payload.intelligence.pool_control.status == "UNAVAILABLE"
    market = sentinel_market(now, read.payload, side=Side.SELL)
    assert market.holders.top_ten_fraction == read.payload.intelligence.holders.top_ten_fraction
    # The entry policy, on the very same snapshot, does wait on it.
    entry = await build(now, None, market=v4_market(traded))
    assert evaluate_snapshot(entry, now).data_gaps
