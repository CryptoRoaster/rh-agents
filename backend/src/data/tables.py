"""PostgreSQL is authoritative; JSONB stores versioned typed event payloads, not files."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(200))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class MarketRow(Document):
    __tablename__ = "market_snapshots"


class MarketObservationRow(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="market_observation_version"),
        Index("ix_observation_pair_time", "provider", "pair_id", "observed_at", "id"),
        Index("ix_observation_asset_time", "asset_id", "observed_at"),
        Index("ix_observation_observed_at", "observed_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    provider: Mapped[str] = mapped_column(String(200))
    chain: Mapped[str] = mapped_column(String(60))
    network: Mapped[str] = mapped_column(String(60))
    asset_id: Mapped[str] = mapped_column(String(200))
    pair_id: Mapped[str] = mapped_column(String(200))
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    freshness_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available: Mapped[bool] = mapped_column(Boolean)
    is_fixture: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class AgentDecisionRow(Document):
    __tablename__ = "agent_decisions"


class IntentRow(Document):
    __tablename__ = "trade_intents"


class RiskRow(Document):
    __tablename__ = "risk_decisions"
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("trade_intents.id"), unique=True)
    market_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("market_snapshots.id"))


class OrderRow(Document):
    __tablename__ = "order_intents"
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("trade_intents.id"), unique=True)
    risk_id: Mapped[UUID] = mapped_column(ForeignKey("risk_decisions.id"), unique=True)


class ExecutionRow(Document):
    __tablename__ = "execution_results"
    order_id: Mapped[UUID] = mapped_column(ForeignKey("order_intents.id"), unique=True)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("trade_intents.id"), unique=True)


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="position_quantity_nonnegative"),
        CheckConstraint("cost_basis_usd >= 0", name="position_basis_nonnegative"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(200), unique=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    cost_basis_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    realized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(200))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class TradeRow(Document):
    __tablename__ = "trades"
    execution_id: Mapped[UUID] = mapped_column(ForeignKey("execution_results.id"), unique=True)
    position_id: Mapped[UUID] = mapped_column(ForeignKey("positions.id"))


class PnLRow(Document):
    __tablename__ = "pnl_snapshots"


class AccountRow(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_paper_account"),
        CheckConstraint("cash_usd >= 0", name="account_cash_nonnegative"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    cash_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    initial_cash_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fees_paid_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    loss_day: Mapped[date] = mapped_column(Date)
    realized_loss_today_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    paused: Mapped[bool] = mapped_column(default=False)
