"""A synthetic ORBIT task input. No market provider, no database, no credential.

The candidate is marked `is_fixture=True`, which the domain model already knows
about: `OrbitReasonCode.FIXTURE_DATA` exists precisely so a fixture-derived
assessment can say where its data came from.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from src.agents.orbit.models import (
    ObservedMeasurement,
    OrbitCandidateContext,
    OrbitTaskInput,
)
from src.markets.models import Availability

EVALUATED_AT = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 9, 22, 7, 59, 30, tzinfo=UTC)

SNAPSHOT_ID = UUID("aaaaaaaa-0000-4000-8000-000000000001")
PRICE_ID = UUID("aaaaaaaa-0000-4000-8000-000000000002")
LIQUIDITY_ID = UUID("aaaaaaaa-0000-4000-8000-000000000003")
VOLUME_ID = UUID("aaaaaaaa-0000-4000-8000-000000000004")


def candidate() -> OrbitCandidateContext:
    return OrbitCandidateContext(
        snapshot_id=SNAPSHOT_ID,
        pair_id="fixture-pair-0001",
        chain="bsc",
        network="bsc",
        venue="fixture-venue",
        base_symbol="FIX",
        quote_symbol="USDT",
        provider="fixture",
        is_fixture=True,
        observed_at=OBSERVED_AT,
        age_seconds=30,
        price=ObservedMeasurement(
            observation_id=PRICE_ID,
            status=Availability.AVAILABLE,
            value_usd=Decimal("1.250000000000000000"),
            observed_at=OBSERVED_AT,
        ),
        liquidity=ObservedMeasurement(
            observation_id=LIQUIDITY_ID,
            status=Availability.AVAILABLE,
            value_usd=Decimal("42000.000000000000000000"),
            observed_at=OBSERVED_AT,
        ),
        volume=ObservedMeasurement(
            observation_id=VOLUME_ID,
            status=Availability.AVAILABLE,
            value_usd=Decimal("15000.000000000000000000"),
            observed_at=OBSERVED_AT,
        ),
        volume_window_seconds=3600,
    )


def task_input() -> OrbitTaskInput:
    return OrbitTaskInput(
        trade_case_id=UUID("bbbbbbbb-0000-4000-8000-000000000001"),
        task_id=UUID("bbbbbbbb-0000-4000-8000-000000000002"),
        candidate=candidate(),
        discovery_liquidity_floor_usd=Decimal("1000.000000000000000000"),
        evaluated_at=EVALUATED_AT,
        discovery_reference=UUID("bbbbbbbb-0000-4000-8000-000000000003"),
    )
