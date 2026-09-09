from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from src.core.models import (
    ExecutionTiming,
    HolderSnapshot,
    LiquiditySnapshot,
    MarketSnapshot,
    RiskContext,
    SafetyStatus,
    Side,
    TokenSnapshot,
    TradeIntent,
)


@pytest.fixture
def now():
    return datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture
def trace():
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def market(now, trace):
    meta = dict(source="test-feed", correlation_id=trace, created_at=now, updated_at=now)
    return MarketSnapshot(
        **meta,
        asset_id="paper:DEMO",
        observed_at=now,
        price_usd=Decimal("100"),
        token=TokenSnapshot(
            **meta, asset_id="paper:DEMO", symbol="DEMO", decimals=9, tradable=SafetyStatus.PASS
        ),
        liquidity=LiquiditySnapshot(
            **meta,
            asset_id="paper:DEMO",
            liquidity_usd=Decimal("1000000"),
            routing=SafetyStatus.PASS,
            estimated_slippage_bps=Decimal("50"),
        ),
        holders=HolderSnapshot(
            **meta,
            asset_id="paper:DEMO",
            holder_count=1000,
            top_ten_fraction=Decimal("0.1"),
            concentration_check=SafetyStatus.PASS,
        ),
        fee_bps=Decimal("10"),
    )


@pytest.fixture
def intent(now, trace):
    return TradeIntent(
        source="COMMANDER",
        correlation_id=trace,
        created_at=now,
        updated_at=now,
        asset_id="paper:DEMO",
        side=Side.BUY,
        quantity=Decimal("2"),
        signal_price=Decimal("100"),
        max_slippage_bps=Decimal("100"),
        timing=ExecutionTiming(detected_at=now, decision_at=now),
    )


@pytest.fixture
def context():
    return RiskContext(
        cash_usd=Decimal("10000"),
        exposure_usd=Decimal("0"),
        position_quantity=Decimal("0"),
        daily_loss_usd=Decimal("0"),
        accounting=SafetyStatus.PASS,
    )
