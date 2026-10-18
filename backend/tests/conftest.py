from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from src.core.models import (
    LP_CONTROL_V4_POOL_CENSUS,
    ExecutionTiming,
    HolderSnapshot,
    LiquiditySnapshot,
    LpControlSnapshot,
    LpControlStatus,
    MarketSnapshot,
    RiskContext,
    SafetyStatus,
    Side,
    TokenSnapshot,
    TradeIntent,
)

# A fixture 32-byte pool id; no such pool exists anywhere.
FIXTURE_POOL_ID = "0x" + "ab" * 32
# Tests that judge LP control themselves and so must see the real derivation:
# the LP-control suite and the offline acceptance of the early entry path.
REAL_LP_CONTROL = ("tests/lpcontrol/", "tests/acceptance/")


@pytest.fixture(autouse=True)
def fixture_lp_control(request):
    """Fixture evidence that the traded pool's LP control is established.

    Issue #92 refuses every BUY whose pool's liquidity control nobody verified,
    and the only verified source is a complete V4 census. The shared fixture
    markets of the other suites are 20-byte pools, chosen long before, and those
    suites test accounting, exits, re-entry and concurrency -- all of which need
    a filled BUY first. For them the *derivation* alone is replaced by a stated
    verified census. SENTINEL and the executor still enforce LP control on
    every BUY there; `tests/lpcontrol/` runs the real derivation and the red
    regression, unpatched.
    """
    path = str(request.node.path).replace("\\", "/")
    if any(prefix in path for prefix in REAL_LP_CONTROL):
        yield
        return
    from src.orchestration.riskrequest import service

    # Its own patcher: a test's `monkeypatch.undo()` must not remove it.
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        service,
        "lp_control_view",
        lambda *, snapshot, onchain: service.LpControlFinding(
            LpControlStatus.PASS,
            LP_CONTROL_V4_POOL_CENSUS,
            "BYTES32_POOL_ID",
            FIXTURE_POOL_ID,
            1_000_000,
        ),
    )
    yield
    patcher.undo()


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
        # Fixture evidence: a verified V4 pool census of the market's own pool,
        # the one basis a BUY's LP control may pass on (issue #92).
        lp_control=LpControlSnapshot(
            **meta,
            asset_id="paper:DEMO",
            status=LpControlStatus.PASS,
            pool_locator_kind="BYTES32_POOL_ID",
            pool_id=FIXTURE_POOL_ID,
            basis=LP_CONTROL_V4_POOL_CENSUS,
            census_block=1_000_000,
            detail=LP_CONTROL_V4_POOL_CENSUS,
        ),
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
