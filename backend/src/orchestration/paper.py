"""Trusted internal entry point: serialize risk + fill + ledger in one DB transaction."""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import (
    ExecutionResult,
    MarketSnapshot,
    OrderIntent,
    Position,
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


@dataclass(frozen=True)
class PaperOutcome:
    """What one order produced: the verdict, and the fill when there was one.

    The decision travels beside the fill rather than being inferred from it. A
    caller that must record *which* evaluation authorised an execution cannot
    get that from an `ExecutionResult` — it names its order, not the decision
    behind it — and deriving a second identifier would be recording a decision
    that does not exist.
    """

    decision: RiskDecision
    order: OrderIntent | None = None
    fill: ExecutionResult | None = None

    @property
    def result(self) -> ExecutionResult | RiskDecision:
        """The historical answer, in the shape the standalone path returns."""
        return self.fill if self.fill is not None else self.decision


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
            replayed = await self.replay_in_session(session, intent)
            if replayed is not None:
                return replayed
            positions = await self.positions_in_session(session)
            # Read time after acquiring the lock and loading the portfolio: time
            # spent waiting must count toward freshness and the UTC loss day.
            now = self._clock.now()
            outcome = await self.execute_in_session(
                session, account, intent, market, positions=positions, marks=marks, now=now
            )
            return outcome.result

    async def replay_in_session(
        self, session: AsyncSession, intent: TradeIntent
    ) -> ExecutionResult | RiskDecision | None:
        """What this exact order already produced, or nothing if it is new.

        Idempotency lives on the intent identity rather than on a caller's
        retry discipline: the same order returns the same fill, or the same
        verdict when it never became one. `None` means nothing has been decided
        about it yet.

        A stored intent whose content differs is a collision, not a replay, and
        is refused rather than silently answered with somebody else's outcome.
        """
        stored = await session.get(IntentRow, intent.id)
        if stored is None:
            return None
        if stored.payload != intent.model_dump(mode="json"):
            raise ValueError("Idempotency conflict: intent ID has different content")
        execution = await session.scalar(
            select(ExecutionRow).where(ExecutionRow.intent_id == intent.id)
        )
        if execution:
            return ExecutionResult.model_validate(execution.payload)
        stored_risk = await session.scalar(select(RiskRow).where(RiskRow.intent_id == intent.id))
        if stored_risk is None:
            raise ValueError("Incomplete persisted intent; reconciliation required")
        return RiskDecision.model_validate(stored_risk.payload)

    async def positions_in_session(self, session: AsyncSession) -> list[Position]:
        return [read_position(row) for row in (await session.scalars(select(PositionRow))).all()]

    async def execute_in_session(
        self,
        session: AsyncSession,
        account: AccountRow,
        intent: TradeIntent,
        market: MarketSnapshot,
        *,
        positions: list[Position],
        marks: dict[str, MarketSnapshot] | None,
        now: datetime,
    ) -> PaperOutcome:
        """Risk-check, fill and book one order inside a transaction the caller owns.

        Exists so a case-bound execution can commit the fill, the ledger and its
        own durable case reference together with this one. A caller that has
        already locked the account and the case cannot hand the write to a
        service that opens its own transaction: the two would commit
        independently, and a crash between them would leave a fill nothing
        points at.

        The caller must already hold the paper account locked, must have taken
        `positions` and any replay check before reading `now`, and owns the
        commit. Everything here is the same risk evaluation, the same executor
        and the same accounting the standalone path performs.

        `now` is the only instant used. The order is requested at it rather than
        at a clock read taken after the intervening writes — those writes are
        this transaction's own, and re-reading the clock between the checks and
        the fill would place the execution outside validities that were checked
        and found good, without anything having actually changed.
        """
        if self.mode != TradingMode.PAPER:
            raise ValueError("Paper execution must be explicitly enabled")
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
            return PaperOutcome(decision=risk)
        execution_requested_at = now
        order = OrderIntent(
            source="PAPER_EXECUTION",
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
        return PaperOutcome(decision=risk, order=order, fill=fill)
