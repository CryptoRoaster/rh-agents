from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.orbit.context import OrbitContextReader
from src.agents.orbit.models import OrbitAssessment, OrbitClassification, OrbitStrength
from src.core.clock import FixedClock
from src.markets.fake import fixture_snapshot
from src.markets.models import Availability, MarketCandidate, MarketSnapshot
from src.orchestration.workflow.models import TradeCase, TradeCaseStatus

# The worker runtime database fixture is shared rather than duplicated.
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

DISCOVERY_FLOOR = Decimal("25000")
MAX_INPUT_AGE = timedelta(minutes=15)


class StubMarkets:
    """Stands in for recorded market data; performs no I/O."""

    def __init__(self, snapshot: MarketSnapshot | None) -> None:
        self.snapshot = snapshot

    async def candidates(
        self, *, include_fixtures: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[MarketCandidate, ...]:
        if self.snapshot is None:
            return ()
        return (MarketCandidate.from_snapshot(self.snapshot),)

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        return self.snapshot


class StubCases:
    def __init__(self, trade_case: TradeCase, evidence: tuple = ()) -> None:
        self.trade_case = trade_case
        self._evidence = evidence

    async def get_trade_case(self, trade_case_id):
        return self.trade_case

    async def evidence(self, trade_case_id):
        return self._evidence


def trade_case_for(snapshot: MarketSnapshot, now, trace) -> TradeCase:
    market = snapshot.pair.market_identity
    return TradeCase(
        id=uuid4(),
        market=market,
        chain=market.chain,
        network=market.network,
        status=TradeCaseStatus.EVIDENCE_PENDING,
        opened_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        originating_discovery_reference=uuid4(),
        revision=1,
        reason_code="CASE_OPENED",
        correlation_id=trace,
        open_idempotency_key="orbit-case",
        open_fingerprint="a" * 64,
    )


def reader_for(snapshot, now, trace, *, max_age=MAX_INPUT_AGE, clock_at=None):
    trade_case = trade_case_for(snapshot, now, trace) if snapshot else None
    return OrbitContextReader(
        cases=StubCases(trade_case) if trade_case else None,
        markets=StubMarkets(snapshot),
        liquidity_floor_usd=DISCOVERY_FLOOR,
        max_input_age=max_age,
        clock=FixedClock(clock_at or now),
        include_fixtures=True,
    )


@pytest.fixture
def snapshot(now, trace):
    return fixture_snapshot(now, trace)


@pytest.fixture
async def task_input(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    return await reader.candidate_context(reader.cases.trade_case.id, uuid4())


def unknown_snapshot(snapshot, field: str, status=Availability.UNKNOWN):
    """Replace one measurement with an unobserved one, preserving provenance."""
    measurement = getattr(snapshot, field)
    return snapshot.model_copy(
        update={field: measurement.model_copy(update={"status": status, "value_usd": None})}
    )


def valued_snapshot(snapshot, field: str, value: Decimal):
    measurement = getattr(snapshot, field)
    return snapshot.model_copy(
        update={
            field: measurement.model_copy(
                update={"status": Availability.AVAILABLE, "value_usd": value}
            )
        }
    )


def assessment_for(
    task_input,
    *,
    classification=OrbitClassification.INTERESTING,
    reason_codes=None,
    data_gaps=(),
    cited=None,
    pair_id=None,
    chain=None,
    summary="Liquidity and price observed on the supplied snapshot.",
):
    from src.agents.orbit.models import OrbitReasonCode

    candidate = task_input.candidate
    return OrbitAssessment(
        classification=classification,
        strength=OrbitStrength.MODERATE,
        reason_codes=reason_codes or (OrbitReasonCode.PRICE_AVAILABLE,),
        data_gaps=data_gaps,
        cited_observation_ids=cited or (candidate.snapshot_id,),
        pair_id=pair_id or candidate.pair_id,
        chain=chain or candidate.chain,
        summary=summary,
    )
