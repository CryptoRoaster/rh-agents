"""EARLY_ANCHOR_EXECUTION_V1: the normal integrity bounds over an early-sized ladder."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from src.agents.anchor.context import AnchorContextReader
from src.agents.anchor.handler import AnchorWorkerHandler
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, EARLY_ANCHOR_EXECUTION_V1
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.quotes import QuoteFailure
from src.orchestration.strategy.early import capacity_refusal
from src.orchestration.worker.capabilities import AnchorCapabilities
from src.orchestration.worker.models import TaskLease
from tests.anchor.conftest import (
    StubCases,
    StubMarkets,
    market_identity,
    source,
    triggered_pair,
)
from tests.anchor.test_context import snapshot_for


class EarlyTradeCase:
    def __init__(self, workflow_version: str) -> None:
        self.market = market_identity()
        self.id = uuid4()
        self.workflow_version = workflow_version


def _reader(now, quotes, *, workflow="trade-case-early-v1"):
    return AnchorContextReader(
        cases=StubCases(EarlyTradeCase(workflow), triggered_pair(now)),
        markets=StubMarkets(snapshot_for(now)),
        quotes=quotes,
        clock=FixedClock(now),
        include_fixtures=True,
    )


def _lease(now):
    return TaskLease(
        lease_id=uuid4(),
        task_id=uuid4(),
        trade_case_id=uuid4(),
        role=AgentRole.ANCHOR,
        task_type="ASSESS_EXECUTION",
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=uuid4(),
    )


async def _assess(now, quotes, **kw):
    reader = _reader(now, quotes, **kw)
    lease = _lease(now)
    capabilities = AnchorCapabilities(lease=lease, context=reader, submit=None)  # type: ignore[arg-type]
    return await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(lease, capabilities)


async def test_an_early_case_walks_the_early_ladder(now):
    context = await _reader(now, source(now)).execution_context(uuid4(), uuid4())
    assert context.policy_version == EARLY_ANCHOR_EXECUTION_V1.version
    # Rungs round up, so each tests at least its target and never less.
    targets = EARLY_ANCHOR_EXECUTION_V1.ladder_notional
    assert len(context.ladder) == len(targets)
    for attempt, target in zip(context.ladder, targets, strict=True):
        assert attempt.notional_usd >= target
        assert attempt.notional_usd - target < Decimal("0.001")


async def test_ten_dollars_accepted_establishes_early_capacity(now):
    report = await _assess(now, source(now))
    assert report.kind == "evidence", report
    detail = report.submission.payload.execution
    assert detail.policy_version == EARLY_ANCHOR_EXECUTION_V1.version
    assert detail.largest_tested_acceptable_notional_usd >= Decimal(10)
    assert capacity_refusal(detail.largest_tested_acceptable_notional_usd) is None
    assert report.submission.provenance.source_version == EARLY_ANCHOR_EXECUTION_V1.version


def _blocked(report):
    assert report.kind == "evidence", report
    payload = report.submission.payload
    assert payload.acceptance().value == "BLOCKED"
    capacity = payload.execution.largest_tested_acceptable_notional_usd
    assert capacity is None
    assert capacity_refusal(capacity) == "EARLY_EXECUTABLE_CAPACITY_UNKNOWN"


async def test_a_rejected_first_rung_proves_no_early_capacity(now):
    _blocked(await _assess(now, source(now, fails_above=Decimal(1))))


async def test_an_unavailable_quote_source_proves_nothing(now):
    report = await _assess(now, source(now, always_fails=QuoteFailure.PROVIDER_UNAVAILABLE))
    assert report.kind == "failure"
    assert report.reason_code == "QUOTES_UNAVAILABLE"


async def test_a_stale_quote_proves_nothing(now):
    _blocked(await _assess(now, source(now, quoted_at=now - timedelta(seconds=60))))


async def test_excessive_deviation_proves_nothing(now):
    quotes = source(now, deviation_bps_per_step=Decimal(500), depth_notional=Decimal(1))
    _blocked(await _assess(now, quotes))


async def test_excessive_provider_impact_proves_nothing(now):
    _blocked(await _assess(now, source(now, provider_price_impact_bps=Decimal(400))))


async def test_an_arbitrary_payment_asset_is_quoted_in_its_own_units(now):
    """A meme/meme style pool: the payment asset is worth a fraction of a cent."""
    from tests.anchor.conftest import payment_snapshot

    price = Decimal("0.00042")
    quotes = source(now, quote_asset_usd_price=price)
    reader = AnchorContextReader(
        cases=StubCases(EarlyTradeCase("trade-case-early-v1"), triggered_pair(now)),
        markets=StubMarkets(snapshot_for(now), payment=payment_snapshot(snapshot_for(now), price)),
        quotes=quotes,
        clock=FixedClock(now),
        include_fixtures=True,
    )
    context = await reader.execution_context(uuid4(), uuid4())
    assert context.quote_asset_valuation.usd_per_token == price
    assert context.ladder[0].notional_usd >= Decimal(10)
    # Tokens of the payment asset, never dollars: ten dollars is many tokens.
    assert context.ladder[0].amount_in_tokens > Decimal(20_000)


async def test_a_normal_case_keeps_the_normal_ladder(now):
    context = await _reader(now, source(now), workflow="trade-case-v2").execution_context(
        uuid4(), uuid4()
    )
    assert context.policy_version == ANCHOR_EXECUTION_V1.version
    for attempt, target in zip(context.ladder, ANCHOR_EXECUTION_V1.ladder_notional, strict=True):
        assert attempt.notional_usd <= target
    assert context.ladder[0].notional_usd > Decimal(99)
