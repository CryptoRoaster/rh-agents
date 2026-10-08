"""The fixed early geometry, and PULSE binding to it exactly as to a VECTOR setup."""

from datetime import timedelta
from decimal import Decimal

from src.agents.early.handler import early_geometry
from src.agents.pulse.context import watched_trigger
from src.agents.pulse.evaluator import evaluate
from src.agents.pulse.models import PriceObservation, PulseTaskInput, TriggerOutcome
from src.core.clock import FixedClock
from src.orchestration.workflow.models import TradeCaseStatus
from src.orchestration.workflow.service import TradeCaseService
from tests.early.conftest import (
    early_setup,
    open_early_case,
    record_early_onchain,
    record_trigger,
    young_history,
)
from tests.riskdata.conftest import PAIR_ID, RecordedMarkets
from tests.riskrequest.conftest import fresh_snapshot

P = Decimal("1.25")


async def _setup(risk_db, now, trace):
    _, sessions = risk_db
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_early_case(cases, sessions, now, trace)
    await record_early_onchain(cases, trade_case, now)
    result, envelope = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "evidence", result
    return cases, trade_case, envelope


async def test_the_geometry_is_fixed_around_the_fresh_price(risk_db, now, trace):
    _, _, envelope = await _setup(risk_db, now, trace)
    payload = envelope.payload
    detail = payload.setup
    assert detail.reference_price == P
    assert detail.entry_low == P * Decimal("0.95") == Decimal("1.1875")
    assert detail.entry_high == P * Decimal("1.05") == Decimal("1.3125")
    assert detail.trigger.type == "PRICE_IN_RANGE"
    assert (detail.trigger.zone_low, detail.trigger.zone_high) == (
        detail.entry_low,
        detail.entry_high,
    )
    assert detail.trigger.reference_price is None
    assert payload.invalidation_price == P * Decimal("0.40")
    assert payload.target_prices == (P * 2,)
    assert payload.side.value == "BUY"
    assert detail.trigger.valid_from == now
    assert detail.trigger.expires_at == now + timedelta(minutes=10)
    assert detail.expires_at == now + timedelta(minutes=10)
    assert envelope.valid_until == now + timedelta(minutes=10)
    assert detail.kind == "PRE_VECTOR_EARLY_ENTRY"
    assert detail.policy_version == "pre-vector-early-setup-v1"
    assert envelope.producer_role.value == "EARLY"


def test_the_geometry_rounds_inward_at_ledger_precision():
    tiny = Decimal("0.0000000123456789012345")  # more places than the ledger holds
    geometry = early_geometry(tiny)
    assert geometry.entry_low >= tiny * Decimal("0.95")
    assert geometry.entry_high <= tiny * Decimal("1.05")
    assert geometry.invalidation_price <= tiny * Decimal("0.40")
    assert geometry.target <= tiny * 2
    for value in (geometry.entry_low, geometry.entry_high, geometry.invalidation_price):
        assert value == value.quantize(Decimal("0.000000000000000001"))


def test_a_price_below_ledger_precision_is_refused_not_collapsed():
    import pytest

    from src.agents.early.ports import EarlyContextUnavailable

    with pytest.raises(EarlyContextUnavailable, match="EARLY_PRICE_PRECISION_UNSUPPORTED"):
        early_geometry(Decimal("0.000000000000000001"))


def _observation(now, price, *, at=None):
    from uuid import uuid4

    return PriceObservation(
        observation_id=uuid4(),
        snapshot_id=uuid4(),
        pair_id=PAIR_ID,
        chain="robinhood",
        network="mainnet",
        venue="uniswap-v3",
        base_asset_id="robinhood:mainnet:0xa1",
        quote_asset_id="robinhood:mainnet:0xb2",
        provider="geckoterminal",
        is_fixture=False,
        price=price,
        observed_at=at or now + timedelta(minutes=1),
    )


def _input(envelope, now, prices):
    trigger = watched_trigger(envelope, now)
    assert trigger is not None
    return PulseTaskInput(
        trade_case_id=envelope.trade_case_id,
        task_id=envelope.evidence_id,
        market_pair_id=PAIR_ID,
        trigger=trigger,
        observations=tuple(_observation(now, price) for price in prices),
        policy_version="pulse-trigger-v1",
        evaluated_at=now,
    )


async def test_pulse_binds_to_the_early_setup_exactly(risk_db, now, trace):
    _, _, envelope = await _setup(risk_db, now, trace)
    trigger = watched_trigger(envelope, now)
    assert trigger.setup_evidence_id == envelope.evidence_id
    assert trigger.setup_id == envelope.payload.setup_id
    assert trigger.setup_fingerprint == envelope.payload.setup.setup_fingerprint
    assert (trigger.zone_low, trigger.zone_high) == (Decimal("1.1875"), Decimal("1.3125"))


async def test_a_price_inside_the_zone_triggers(risk_db, now, trace):
    _, _, envelope = await _setup(risk_db, now, trace)
    later = now + timedelta(minutes=2)
    result = evaluate(_input(envelope, now, (Decimal("1.30"),)), later)
    assert result.outcome is TriggerOutcome.TRIGGERED


async def test_a_price_outside_the_zone_does_not(risk_db, now, trace):
    _, _, envelope = await _setup(risk_db, now, trace)
    later = now + timedelta(minutes=2)
    for price in (Decimal("1.3126"), Decimal("1.1874")):
        result = evaluate(_input(envelope, now, (price,)), later)
        assert result.outcome is TriggerOutcome.NOT_TRIGGERED


async def test_an_expired_early_setup_does_not_trigger(risk_db, now, trace):
    _, _, envelope = await _setup(risk_db, now, trace)
    result = evaluate(_input(envelope, now, (P,)), now + timedelta(minutes=10))
    assert result.outcome is TriggerOutcome.SETUP_EXPIRED


async def test_a_trigger_for_another_setup_is_refused_by_the_workflow(risk_db, now, trace):
    from uuid import uuid4

    import pytest

    from src.core.models import AgentRole
    from src.orchestration.workflow.models import EvidenceType, WorkflowFailure
    from tests.riskdata.conftest import record
    from tests.worker.conftest import trigger_payload

    cases, trade_case, _ = await _setup(risk_db, now, trace)
    with pytest.raises(WorkflowFailure, match="EVIDENCE_BINDING"):
        await record(
            cases,
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(uuid4()),
            key="wrong-trigger",
        )
    current = await cases.get_trade_case(trade_case.id)
    assert current.status is TradeCaseStatus.READY_FOR_TRIGGER


async def test_the_right_trigger_moves_the_case_on(risk_db, now, trace):
    cases, trade_case, envelope = await _setup(risk_db, now, trace)
    await record_trigger(cases, trade_case, now, envelope)
    current = await cases.get_trade_case(trade_case.id)
    assert current.status in (TradeCaseStatus.TRIGGERED, TradeCaseStatus.EXECUTION_EVIDENCE_PENDING)
