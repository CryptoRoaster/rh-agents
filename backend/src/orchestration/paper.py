"""Trusted internal entry point: serialize risk + fill + ledger in one DB transaction."""

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import (
    ExecutionResult,
    MarketSnapshot,
    OrderIntent,
    RiskDecision,
    RiskLimits,
    RiskOutcome,
    TradeIntent,
    TradingMode,
)
from src.data.repository import append, read_position, save_position
from src.data.tables import AccountRow, ExecutionRow, IntentRow, PositionRow, RiskRow
from src.execution.paper import PaperExecutor
from src.ledger.accounting import apply_fill, calculate_pnl
from src.ledger.portfolio import portfolio_state, roll_loss_day
from src.risk.engine import evaluate

logger = logging.getLogger(__name__)


class PaperTradingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        limits: RiskLimits,
        mode: TradingMode = TradingMode.OBSERVE,
        *,
        clock: Clock | None = None,
    ) -> None:
        if mode == TradingMode.LIVE_AUTONOMOUS:
            raise ValueError("Live execution is unavailable")
        self.sessions, self.limits, self.mode = sessions, limits, mode
        self.executor = PaperExecutor()
        # Only trusted bootstrap/test infrastructure constructs this service.
        self._clock = clock if clock is not None else SystemClock()

    async def process(
        self,
        intent: TradeIntent,
        market: MarketSnapshot,
        *,
        marks: dict[str, MarketSnapshot] | None = None,
    ) -> ExecutionResult | RiskDecision:
        try:
            return await self._process(intent, market, marks=marks)
        except Exception:
            logger.exception(
                "paper_processing_failed",
                extra={"correlation_id": str(intent.correlation_id), "intent_id": str(intent.id)},
            )
            raise

    async def _process(
        self,
        intent: TradeIntent,
        market: MarketSnapshot,
        *,
        marks: dict[str, MarketSnapshot] | None = None,
    ) -> ExecutionResult | RiskDecision:
        if self.mode != TradingMode.PAPER:
            raise ValueError("Paper execution must be explicitly enabled")
        # One AsyncSession per task. PostgreSQL row locking serializes this portfolio.
        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise ValueError("Paper account has not been initialized by migrations")
            old_intent = await session.get(IntentRow, intent.id)
            if old_intent:
                if old_intent.payload != intent.model_dump(mode="json"):
                    raise ValueError("Idempotency conflict: intent ID has different content")
                execution = await session.scalar(
                    select(ExecutionRow).where(ExecutionRow.intent_id == intent.id)
                )
                if execution:
                    return ExecutionResult.model_validate(execution.payload)
                stored_risk = await session.scalar(
                    select(RiskRow).where(RiskRow.intent_id == intent.id)
                )
                if stored_risk is None:
                    raise ValueError("Incomplete persisted intent; reconciliation required")
                return RiskDecision.model_validate(stored_risk.payload)
            positions = [
                read_position(row) for row in (await session.scalars(select(PositionRow))).all()
            ]
            # Read time after acquiring the lock and loading the portfolio: time
            # spent waiting must count toward freshness and the UTC loss day.
            now = self._clock.now()
            roll_loss_day(account, now)
            # One implementation of "what does the account hold, valued?", shared
            # with the case-bound risk request. Two would eventually disagree
            # about money, and which was right would be decided by whichever ran.
            state = portfolio_state(
                cash_usd=account.cash_usd,
                realized_loss_today_usd=account.realized_loss_today_usd,
                positions=positions,
                asset_id=intent.asset_id,
                price_usd=market.price_usd,
                marks=marks,
                now=now,
                max_snapshot_age_seconds=self.limits.max_snapshot_age_seconds,
                correlation_id=intent.correlation_id,
            )
            for mark in state.marks_used:
                await append(session, mark)
            position, prices, context = state.position, state.prices, state.context
            limits = self.limits.model_copy(
                update={"kill_switch": self.limits.kill_switch or account.paused}
            )
            risk = evaluate(intent, market, context, limits, now=now)
            await append(session, market)
            await append(session, intent)
            await append(session, risk)
            if risk.outcome != RiskOutcome.APPROVE:
                if risk.outcome == RiskOutcome.PAUSE_SYSTEM:
                    account.paused = True
                return risk
            execution_requested_at = self._clock.now()
            order = OrderIntent(
                source="COMMANDER",
                correlation_id=intent.correlation_id,
                created_at=execution_requested_at,
                updated_at=execution_requested_at,
                intent=intent,
                risk=risk,
                execution_requested_at=execution_requested_at,
            )
            await append(session, order)
            fill = await self.executor.execute(order, market)
            await append(session, fill)
            updated, trade, cash = apply_fill(position, fill, account.cash_usd)
            await save_position(session, updated)
            await append(session, trade)
            account.cash_usd = cash
            account.fees_paid_usd += fill.fees_usd + fill.gas_usd
            account.realized_loss_today_usd += max(Decimal("0"), -trade.realized_pnl_usd)
            positions = [p for p in positions if p.asset_id != updated.asset_id] + [updated]
            pnl = calculate_pnl(
                positions, prices, cash=cash, fees_paid=account.fees_paid_usd, fill=fill
            )
            await append(session, pnl)
            return fill
