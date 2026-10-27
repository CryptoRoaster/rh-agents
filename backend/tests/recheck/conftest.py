"""Fixtures for the liquidity/FDV re-check.

A predecessor is carried to `RISK_REJECTED` in one of two ways. The real way
runs the whole chain: evidence through the workflow, the real risk request and
the real SENTINEL, with ATLAS evidence whose supply puts the ratio outside the
band. The direct way binds a SENTINEL decision with exactly the reason codes a
test names, through the workflow's own `record_risk_decision`, so eligibility
can be pinned code by code without engineering a market for each.

What is stubbed is the market feed's read and the stop source. No provider is
called and no specialist worker runs.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.models import RiskDecision, RiskLimits, RiskMetrics, RiskOutcome, TradingMode
from src.data.tables import TradeCaseEventRow, TradeCaseRiskBindingRow, TradeCaseRow
from src.orchestration.recheck.service import LiquidityFdvRecheckService
from src.orchestration.workflow.models import TradeCaseStatus
from src.orchestration.workflow.service import TradeCaseService
from tests.riskrequest.conftest import (  # noqa: F401
    FRESH,
    PAIR_ID,
    RunningSystem,
    build_service,
    evidence_for,
    fresh_onchain,
    holder_block,
    ready_case,
    risk_db,
    set_account,
)

# Past the policy's minimum interval, inside its window.
LATER = timedelta(minutes=31)

# The fixture market prices 1e6 tokens at 1.25 USD against 750 000 USD of pool:
# a ratio of 60 %. Ten times the supply puts it at 6 % (below the floor); about
# half of it at 120 % (above the ceiling).
THIN_SUPPLY = "10000000000000000000000000"
HEAVY_SUPPLY = "500000000000000000000000"


def build_recheck(sessions, now, *, limits=None, pause="running", **overrides):
    """The real workflow service behind one narrow call. No market port at all."""
    clock = FixedClock(now)
    arguments = {
        "sessions": sessions,
        "cases": TradeCaseService(sessions, clock=clock),
        "limits": limits if limits is not None else RiskLimits(),
        "enabled_chains": frozenset({"robinhood"}),
        "early_only": False,
        "trading_mode": TradingMode.PAPER,
        "clock": clock,
        "pause": RunningSystem() if pause == "running" else pause,
    }
    return LiquidityFdvRecheckService(**{**arguments, **overrides})


def onchain_with(now, **holders):
    """Fresh ATLAS evidence whose holder block differs only in the named fields."""
    block = holder_block(now, age=FRESH).model_copy(update=holders)
    return fresh_onchain(now, holders=block)


async def rejected_by_sentinel(sessions, now, trace, *, key, **holders):
    """A case refused by the real SENTINEL, on the evidence it was given."""
    service = build_service(sessions, now)
    case = await ready_case(
        service.cases, now, trace, key=key, onchain=onchain_with(now, **holders)
    )
    result = await service.request_risk_evaluation(case.id, request_key=f"{key}-risk")
    assert result.kind == "risk_request_evaluated", getattr(result, "reason", None)
    assert result.outcome is RiskOutcome.REJECT
    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_REJECTED
    return after, result


def decision(trade_case, now, *, codes, outcome=RiskOutcome.REJECT):
    return RiskDecision(
        source="SENTINEL",
        correlation_id=trade_case.correlation_id,
        created_at=now,
        updated_at=now,
        intent_id=uuid4(),
        intent_fingerprint="intent",
        market_snapshot_id=uuid4(),
        market_fingerprint="market",
        outcome=outcome,
        reason_codes=tuple(codes),
        position_size_limit_usd=Decimal("0"),
        max_additional_notional_usd=Decimal("0"),
        max_slippage_bps=Decimal("100"),
        metrics=RiskMetrics(
            requested_notional_usd=Decimal("100"),
            worst_case_notional_usd=Decimal("101"),
            exposure_usd=Decimal("0"),
            daily_loss_usd=Decimal("0"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
        ),
        evaluated_at=now,
        expires_at=now + timedelta(hours=2),
    )


async def rejected_with(sessions, now, trace, *, key, codes, outcome=RiskOutcome.REJECT):
    """A case bound to a SENTINEL decision carrying exactly `codes`."""
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    case = await ready_case(cases, now, trace, key=key)
    assert case.status is TradeCaseStatus.READY_FOR_RISK
    await cases.record_risk_decision(
        case.id,
        decision(case, now, codes=codes, outcome=outcome),
        risk_input_digest=case.risk_input_digest,
    )
    after = await cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_REJECTED
    return after


async def history(sessions, trade_case_id):
    """Everything recorded about one case, to prove it was never rewritten."""
    async with sessions() as session:
        row = await session.get(TradeCaseRow, trade_case_id)
        bindings = (
            await session.scalars(
                select(TradeCaseRiskBindingRow.payload).where(
                    TradeCaseRiskBindingRow.trade_case_id == trade_case_id
                )
            )
        ).all()
        events = (
            await session.scalars(
                select(TradeCaseEventRow.event_type)
                .where(TradeCaseEventRow.trade_case_id == trade_case_id)
                .order_by(TradeCaseEventRow.sequence)
            )
        ).all()
        return (row.status, row.revision, row.risk_input_digest, list(bindings), list(events))


async def market_cases(sessions):
    async with sessions() as session:
        return await session.scalar(
            select(func.count()).select_from(TradeCaseRow).where(TradeCaseRow.market_key == PAIR_ID)
        )
