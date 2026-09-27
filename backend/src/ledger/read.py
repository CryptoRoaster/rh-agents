"""Read-only paper portfolio views: account, positions, fills and P&L snapshots.

Only what the ledger persisted is shown. Nothing here computes a mark, an
unrealized P&L or an equity figure the database does not already hold: where the
ledger has no P&L snapshot, the answer is "none recorded", never an estimate.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.models import PnLSnapshot
from src.data.repository import aware
from src.data.tables import (
    AccountRow,
    PnLRow,
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
)
from src.runner.models import Identifier, Immutable


class AccountView(Immutable):
    cash_usd: Decimal
    initial_cash_usd: Decimal
    fees_paid_usd: Decimal
    realized_loss_today_usd: Decimal
    paused: bool


class PositionView(Immutable):
    position_id: UUID
    asset_id: Identifier
    market_pair_id: Identifier | None = None
    market_chain: Identifier | None = None
    quantity: Decimal
    cost_basis_usd: Decimal
    realized_pnl_usd: Decimal
    open: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime


class FillView(Immutable):
    """One paper fill, entry or exit, as the case workflow booked it."""

    side: Literal["BUY", "SELL"]
    trade_case_id: UUID
    execution_id: UUID
    quantity: Decimal
    execution_price_usd: Decimal
    notional_usd: Decimal
    fees_usd: Decimal
    realized_pnl_usd: Decimal | None = None
    filled_at: AwareDatetime
    mode: Literal["PAPER"] = "PAPER"


class PnLView(Immutable):
    recorded_at: AwareDatetime
    snapshot: PnLSnapshot


class PaperPortfolio(Immutable):
    account: AccountView | None = None
    positions: tuple[PositionView, ...] = ()
    fills: tuple[FillView, ...] = ()
    pnl: tuple[PnLView, ...] = ()


@dataclass(frozen=True)
class PaperReadService:
    sessions: async_sessionmaker[AsyncSession]

    async def portfolio(self, *, fills: int = 50, pnl: int = 50) -> PaperPortfolio:
        async with self.sessions() as session:
            account = await session.get(AccountRow, 1)
            positions = (
                await session.scalars(
                    select(PositionRow).order_by(PositionRow.updated_at.desc(), PositionRow.id)
                )
            ).all()
            entries = (
                await session.scalars(
                    select(TradeCaseExecutionRow)
                    .order_by(TradeCaseExecutionRow.filled_at.desc())
                    .limit(fills)
                )
            ).all()
            exits = (
                await session.scalars(
                    select(TradeCaseExitRow)
                    .order_by(TradeCaseExitRow.filled_at.desc())
                    .limit(fills)
                )
            ).all()
            snapshots = (
                await session.scalars(select(PnLRow).order_by(PnLRow.created_at.desc()).limit(pnl))
            ).all()
        booked = [
            FillView(
                side="BUY",
                trade_case_id=row.trade_case_id,
                execution_id=row.execution_id,
                quantity=row.quantity,
                execution_price_usd=row.execution_price_usd,
                notional_usd=row.notional_usd,
                fees_usd=row.fees_usd,
                filled_at=aware(row.filled_at),
            )
            for row in entries
        ] + [
            FillView(
                side="SELL",
                trade_case_id=row.trade_case_id,
                execution_id=row.execution_id,
                quantity=row.quantity,
                execution_price_usd=row.execution_price_usd,
                notional_usd=row.notional_usd,
                fees_usd=row.fees_usd,
                realized_pnl_usd=row.realized_pnl_usd,
                filled_at=aware(row.filled_at),
            )
            for row in exits
        ]
        booked.sort(key=lambda item: item.filled_at, reverse=True)
        return PaperPortfolio(
            account=None
            if account is None
            else AccountView(
                cash_usd=account.cash_usd,
                initial_cash_usd=account.initial_cash_usd,
                fees_paid_usd=account.fees_paid_usd,
                realized_loss_today_usd=account.realized_loss_today_usd,
                paused=account.paused,
            ),
            positions=tuple(
                PositionView(
                    position_id=row.id,
                    asset_id=row.asset_id,
                    market_pair_id=row.market_pair_id,
                    market_chain=row.market_chain,
                    quantity=row.quantity,
                    cost_basis_usd=row.cost_basis_usd,
                    realized_pnl_usd=row.realized_pnl_usd,
                    open=row.quantity != 0,
                    created_at=aware(row.created_at),
                    updated_at=aware(row.updated_at),
                )
                for row in positions
            ),
            fills=tuple(booked[:fills]),
            pnl=tuple(
                PnLView(
                    recorded_at=aware(row.created_at),
                    snapshot=PnLSnapshot.model_validate(row.payload),
                )
                for row in snapshots
            ),
        )
