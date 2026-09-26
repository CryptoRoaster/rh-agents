"""PostgreSQL is authoritative; JSONB stores versioned typed event payloads, not files."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
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
        CheckConstraint("schema_version IN (1, 2)", name="market_observation_version"),
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
    pair_id: Mapped[str] = mapped_column(String(512))
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
        Index("ix_positions_market_pair", "market_pair_id"),
        Index("ix_positions_cycle", "cycle_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(200), unique=True)
    # The trading cycle this holding currently belongs to. One row per asset is
    # deliberate — the valuation contract rests on exactly one holding per
    # asset — so the row is reused after an exit and this is what distinguishes
    # its current owner from every cycle before it. Nullable for holdings that
    # predate cycles, and those are reported rather than attributed.
    cycle_id: Mapped[UUID | None] = mapped_column(Uuid)
    # The market this position was acquired in. An asset is not a market: a
    # token can trade in several pools, and observations are indexed by pair, so
    # a position that does not name its own market cannot be valued without
    # resolving an ambiguity silently. Nullable because positions written before
    # this carry none, and those are reported unvaluable rather than guessed at.
    market_pair_id: Mapped[str | None] = mapped_column(String(512))
    market_chain: Mapped[str | None] = mapped_column(String(60))
    market_network: Mapped[str | None] = mapped_column(String(60))
    market_provider: Mapped[str | None] = mapped_column(String(200))
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


class RuntimeAuditRow(Base):
    __tablename__ = "runtime_audit"
    __table_args__ = (Index("ix_runtime_audit_stream_time", "stream", "recorded_at", "id"),)
    sequence: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    stream: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class EvmCursorRow(Base):
    __tablename__ = "evm_chain_cursors"
    __table_args__ = (
        CheckConstraint(
            "chain IN ('robinhood', 'bsc') AND network = 'mainnet'", name="evm_cursor_chain"
        ),
        CheckConstraint(
            "last_processed_block >= 0 AND last_safe_block >= 0 AND last_seen_head >= 0",
            name="evm_cursor_nonnegative",
        ),
    )
    chain: Mapped[str] = mapped_column(String(60), primary_key=True)
    network: Mapped[str] = mapped_column(String(60))
    last_seen_head: Mapped[int] = mapped_column(BigInteger)
    last_safe_block: Mapped[int] = mapped_column(BigInteger)
    last_processed_block: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[UUID] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class EvmLogRow(Base):
    __tablename__ = "evm_log_observations"
    __table_args__ = (Index("ix_evm_logs_chain_block", "chain", "block_number"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    chain: Mapped[str] = mapped_column(String(60))
    network: Mapped[str] = mapped_column(String(60))
    block_number: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[UUID] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseRow(Base):
    __tablename__ = "trade_cases"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="trade_case_revision_positive"),
        Index("ix_trade_cases_status_updated", "status", "updated_at", "id"),
        Index("ix_trade_cases_market", "chain", "network", "market_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workflow_version: Mapped[str] = mapped_column(String(40))
    market_key: Mapped[str] = mapped_column(String(1200))
    chain: Mapped[str] = mapped_column(String(60))
    network: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(40))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    originating_discovery_reference: Mapped[UUID] = mapped_column(Uuid)
    strategy_policy_id: Mapped[str | None] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(80))
    blockers: Mapped[list[dict[str, Any]]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    risk_input_digest: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    open_idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    open_fingerprint: Mapped[str] = mapped_column(String(64))
    market_payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseTaskRow(Base):
    __tablename__ = "trade_case_tasks"
    __table_args__ = (
        UniqueConstraint(
            "trade_case_id", "role", "task_type", "attempt", name="uq_trade_case_task_attempt"
        ),
        # One row per work slot; `attempt` is a mutable counter, not a new row.
        UniqueConstraint("trade_case_id", "role", "task_type", name="uq_trade_case_task_slot"),
        CheckConstraint("lease_renewals >= 0", name="trade_case_task_renewals_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="trade_case_task_attempts_positive"),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_started_at IS NULL"
            " OR lease_expires_at > lease_started_at",
            name="trade_case_task_lease_window",
        ),
        # A lease is all-or-nothing: never a holder without a token, or the reverse.
        CheckConstraint(
            "(lease_id IS NULL) = (worker_instance_id IS NULL)",
            name="trade_case_task_lease_pairing",
        ),
        Index("ix_trade_case_tasks_case_status", "trade_case_id", "status"),
        Index("ix_trade_case_tasks_claim", "role", "status", "next_eligible_at", "created_at"),
        Index("ix_trade_case_tasks_lease_expiry", "status", "lease_expires_at"),
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(40))
    task_type: Mapped[str] = mapped_column(String(80))
    required: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    reason_code: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    # Phase 2B current lease and scheduling state. Held on the task aggregate
    # rather than in a separate lease table so that "at most one active lease"
    # holds by construction: one row owns at most one lease_id. Immutable attempt
    # history lives in worker_task_attempts.
    lease_id: Mapped[UUID | None] = mapped_column(Uuid)
    worker_instance_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("worker_instances.worker_instance_id")
    )
    lease_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_renewals: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    failure_category: Mapped[str | None] = mapped_column(String(40))


class TradeCaseEvidenceRow(Base):
    __tablename__ = "trade_case_evidence"
    __table_args__ = (Index("ix_trade_case_evidence_case_type", "trade_case_id", "evidence_type"),)
    evidence_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    producer_role: Mapped[str] = mapped_column(String(40))
    evidence_type: Mapped[str] = mapped_column(String(60))
    schema_version: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trade_case_evidence.evidence_id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    submission_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseRiskBindingRow(Base):
    """Immutable SENTINEL decisions bound to one TradeCase risk-input digest.

    Every restriction a future execution boundary must honour has its own typed
    column. ``payload`` keeps the whole decision for audit provenance only.
    """

    __tablename__ = "trade_case_risk_bindings"
    __table_args__ = (
        UniqueConstraint("trade_case_id", "case_revision", name="uq_trade_case_risk_revision"),
        CheckConstraint(
            "position_size_limit_usd >= 0", name="trade_case_risk_position_limit_nonnegative"
        ),
        CheckConstraint(
            "max_additional_notional_usd >= 0", name="trade_case_risk_notional_nonnegative"
        ),
        CheckConstraint(
            "max_slippage_bps >= 0 AND max_slippage_bps <= 10000",
            name="trade_case_risk_slippage_bounded",
        ),
        Index("ix_trade_case_risk_case_time", "trade_case_id", "evaluated_at"),
    )
    binding_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    case_revision: Mapped[int] = mapped_column(Integer)
    risk_input_digest: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(40))
    authorization: Mapped[str] = mapped_column("risk_authorization", String(40))
    reason_codes: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    position_size_limit_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    max_additional_notional_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    max_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseRiskRequestRow(Base):
    """One durable trade request per TradeCase, and the whole basis it rested on.

    Written only when SENTINEL actually ran. A refusal — incomplete data, a
    stale source, a stop in force — records nothing, so an attempt that never
    reached a verdict cannot permanently consume the case's one request.

    The unique constraint on ``trade_case_id`` is the contract: a case gets one
    canonical trade request, and a changed data situation is not by itself
    permission for another. When a second request may legitimately be made is an
    open question this phase deliberately does not answer.
    """

    __tablename__ = "trade_case_risk_requests"
    __table_args__ = (
        CheckConstraint("case_revision >= 1", name="trade_case_risk_request_revision_positive"),
        CheckConstraint(
            "requested_notional_usd > 0", name="trade_case_risk_request_notional_positive"
        ),
        CheckConstraint("quantity > 0", name="trade_case_risk_request_quantity_positive"),
        UniqueConstraint("trade_case_id", name="uq_trade_case_risk_request_case"),
        Index("ix_trade_case_risk_requests_case_time", "trade_case_id", "recorded_at"),
        Index("ix_trade_case_risk_requests_correlation", "correlation_id"),
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    request_key: Mapped[str] = mapped_column(String(200), unique=True)
    case_revision: Mapped[int] = mapped_column(Integer)
    # The safety-evidence digest as it stood, copied rather than recomputed.
    risk_input_digest: Mapped[str] = mapped_column(String(64))
    # Sizing, intent, portfolio, limits, cost assumptions and their sources.
    # Separate from the safety digest so neither redefines the other.
    risk_request_digest: Mapped[str] = mapped_column(String(64))
    intent_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    intent_fingerprint: Mapped[str] = mapped_column(String(64))
    requested_notional_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    binding_id: Mapped[UUID] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(40))
    authorization: Mapped[str] = mapped_column("risk_authorization", String(40))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    basis: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseExecutionRow(Base):
    """One durable paper fill per TradeCase, bound to what authorised it.

    `execution_results.intent_id` is already unique and the intent identity
    derives from the stored request, so a second fill for one order cannot
    exist. What was missing is the case reference: an execution row names no
    case, so nothing joined a fill back to the authorization it ran under.

    Two decisions are referenced and kept apart. ``authorizing_binding_id`` is
    the original approval, untouched; ``recheck_decision_id`` is the evaluation
    performed immediately before the fill, on the portfolio and market as they
    were at that moment.
    """

    __tablename__ = "trade_case_executions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="trade_case_execution_quantity_positive"),
        CheckConstraint("execution_price_usd > 0", name="trade_case_execution_price_positive"),
        CheckConstraint("notional_usd > 0", name="trade_case_execution_notional_positive"),
        CheckConstraint("fees_usd >= 0", name="trade_case_execution_fees_nonnegative"),
        UniqueConstraint("trade_case_id", name="uq_trade_case_execution_case"),
        UniqueConstraint("cycle_id", name="uq_trade_case_execution_cycle"),
        Index("ix_trade_case_executions_case_time", "trade_case_id", "recorded_at"),
        Index("ix_trade_case_executions_correlation", "correlation_id"),
    )
    case_execution_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_cycles.cycle_id", ondelete="RESTRICT"), name="cycle_id"
    )
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_case_risk_requests.request_id", ondelete="CASCADE"), unique=True
    )
    request_key: Mapped[str] = mapped_column(String(200))
    intent_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    execution_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    authorizing_binding_id: Mapped[UUID] = mapped_column(Uuid)
    recheck_decision_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    risk_input_digest: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    execution_price_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    notional_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    basis: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCycleRow(Base):
    """One trading cycle: a case that opened a position, and the exit that closed it.

    "One position per asset" and "one trade per market" used to be the same
    sentence. A position row reached zero and stayed, and every record pointing
    at it pointed at the only entry there had ever been. Re-entry breaks that:
    the row is reused, a second entry and exit appear beside the first, and
    "which entry does this holding come from?" stops having a single answer.

    This is that answer, and it is the only thing that distinguishes a reused
    position row's current owner from every cycle that came before it. The
    position table stays one row per asset — the whole valuation contract rests
    on exactly one holding per asset — and carries the cycle it currently
    belongs to rather than a history it would have to merge.

    `predecessor_exit_id` is unique, so a completed cycle has at most one
    successor: a second re-entry request for the same exit is refused rather
    than opening a parallel one.
    """

    __tablename__ = "trade_cycles"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="trade_cycle_sequence_positive"),
        # A first cycle has neither a predecessor nor a request key; every later
        # one has both. Half of either would be a cycle nobody could place.
        CheckConstraint(
            "(predecessor_exit_id IS NULL) = (request_key IS NULL)",
            name="trade_cycle_succession_complete",
        ),
        CheckConstraint(
            "(predecessor_exit_id IS NULL) = (sequence = 1)",
            name="trade_cycle_first_has_no_predecessor",
        ),
        UniqueConstraint("market_pair_id", "sequence", name="uq_trade_cycle_market_sequence"),
        Index("ix_trade_cycles_asset", "asset_id"),
        Index("ix_trade_cycles_market", "market_pair_id", "sequence"),
    )
    cycle_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_cases.id", ondelete="CASCADE"), unique=True
    )
    asset_id: Mapped[str] = mapped_column(String(200))
    market_pair_id: Mapped[str] = mapped_column(String(512))
    sequence: Mapped[int] = mapped_column(Integer)
    # Deliberately not a foreign key: an exit already points at its cycle, and
    # pointing back would make the two tables mutually dependent, which no
    # schema tool can order and no fresh database can create. The value is read
    # under the account and case locks from the exit row itself.
    predecessor_exit_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True)
    request_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid)


class TradeCaseExitRow(Base):
    """One durable paper exit per entry, bound to everything it closes.

    A position row that reached zero looks exactly like one that was never
    opened, and the SELL fill beside it names no case. This is the join that
    makes a deliberate close answerable in one read: the holding, the case that
    opened it, the entry fill, the market both happened in, and the exit's own
    intent, order, fill and decision.

    The uniqueness is the contract. One exit per position, per entry fill and
    per case, so two callers racing for the same holding cannot both sell it —
    the account lock orders them and this refuses the loser even if it did not.
    """

    __tablename__ = "trade_case_exits"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="trade_case_exit_quantity_positive"),
        CheckConstraint("execution_price_usd > 0", name="trade_case_exit_price_positive"),
        CheckConstraint("notional_usd > 0", name="trade_case_exit_notional_positive"),
        CheckConstraint("fees_usd >= 0", name="trade_case_exit_fees_nonnegative"),
        CheckConstraint("cost_basis_released_usd >= 0", name="trade_case_exit_basis_nonnegative"),
        UniqueConstraint("trade_case_id", name="uq_trade_case_exit_case"),
        # One exit per *cycle* rather than per position: the position row
        # outlives the cycle and is reused by the next one. Two sales of one
        # holding inside a cycle stay impossible.
        UniqueConstraint("cycle_id", name="uq_trade_case_exit_cycle"),
        Index("ix_trade_case_exits_case_time", "trade_case_id", "recorded_at"),
        Index("ix_trade_case_exits_correlation", "correlation_id"),
    )
    exit_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    case_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_case_executions.case_execution_id", ondelete="CASCADE"), unique=True
    )
    cycle_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cycles.cycle_id", ondelete="RESTRICT"))
    request_key: Mapped[str] = mapped_column(String(200), unique=True)
    position_id: Mapped[UUID] = mapped_column(Uuid)
    asset_id: Mapped[str] = mapped_column(String(200))
    market_pair_id: Mapped[str] = mapped_column(String(512))
    intent_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    execution_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    # The exit's own decision. The entry's approval authorised a purchase and
    # nothing else, so it is referenced by the entry row and never here.
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    execution_price_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    notional_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    # Signed: a loss is a real outcome, not a missing value.
    realized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    cost_basis_released_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    basis: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class TradeCaseTransitionRow(Base):
    __tablename__ = "trade_case_transitions"
    __table_args__ = (
        UniqueConstraint("trade_case_id", "revision", name="uq_trade_case_transition_revision"),
        Index("ix_trade_case_transition_case_time", "trade_case_id", "recorded_at"),
    )
    transition_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    reason_code: Mapped[str] = mapped_column(String(80))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    blockers: Mapped[list[dict[str, Any]]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    risk_input_digest: Mapped[str | None] = mapped_column(String(64))


class TradeCaseEventRow(Base):
    __tablename__ = "trade_case_events"
    __table_args__ = (Index("ix_trade_case_events_case_sequence", "trade_case_id", "sequence"),)
    sequence: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    event_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(80))
    reason_code: Mapped[str] = mapped_column(String(80))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))


class WorkerInstanceRow(Base):
    """A registered worker runtime instance.

    Persisted because lease ownership, crash diagnosis and attempt audit all need a
    stable, independently identifiable claimant. Ephemeral host or process metadata
    is deliberately not part of this logical identity.
    """

    __tablename__ = "worker_instances"
    __table_args__ = (Index("ix_worker_instances_role_status", "role", "status"),)
    worker_instance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(String(40))
    runtime_version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registration_key: Mapped[str] = mapped_column(String(200), unique=True)
    registration_fingerprint: Mapped[str] = mapped_column(String(64))


class WorkerTaskAttemptRow(Base):
    """Immutable-once-finished history of every worker attempt.

    Heartbeats deliberately do not touch this table; they update the task
    aggregate's current lease state. An attempt row is inserted on claim and
    written exactly once more, when it finishes. A database trigger then rejects
    any further update, and deletes and truncation always.
    """

    __tablename__ = "worker_task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_worker_attempt_number"),
        CheckConstraint("attempt_number >= 1", name="worker_attempt_number_positive"),
        CheckConstraint("lease_expires_at > started_at", name="worker_attempt_lease_window"),
        CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)", name="worker_attempt_outcome_pairing"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="worker_attempt_ordering"
        ),
        Index("ix_worker_attempts_task", "task_id", "attempt_number"),
        Index("ix_worker_attempts_instance", "worker_instance_id", "started_at"),
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    trade_case_id: Mapped[UUID] = mapped_column(ForeignKey("trade_cases.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("trade_case_tasks.task_id"))
    role: Mapped[str] = mapped_column(String(40))
    worker_instance_id: Mapped[UUID] = mapped_column(
        ForeignKey("worker_instances.worker_instance_id")
    )
    lease_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(40))
    reason_code: Mapped[str] = mapped_column(String(80))
    failure_category: Mapped[str | None] = mapped_column(String(40))
    runtime_version: Mapped[str] = mapped_column(String(40))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class DiscoveryWatchRow(Base):
    """One market the early-discovery scout found and keeps looking at.

    Deliberately not a TradeCase. A case is a short-lived decision workflow that
    ends in a verdict; a watch is a long-lived observation schedule that ends in
    nothing more than PROMOTABLE or DORMANT. Mixing the two would either keep a
    case open for days or throw a young market away after its first reading.

    One row per market stream — provider, chain, network, pair and fixture flag —
    so repeated discovery updates the same watch. `first_seen_at` is the source
    instant of the first observation this system recorded, never a claim about
    when the pool was created.
    """

    __tablename__ = "discovery_watches"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "chain",
            "network",
            "pair_id",
            "is_fixture",
            name="uq_discovery_watch_stream",
        ),
        CheckConstraint(
            "status IN ('WATCHING', 'PROMOTABLE', 'DORMANT', 'RETIRED')",
            name="discovery_watch_status",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="discovery_watch_seen_order"),
        CheckConstraint(
            "orbit_checkpoint_index IS NULL OR orbit_checkpoint_index >= 0",
            name="discovery_watch_checkpoint_index",
        ),
        Index("ix_discovery_watches_orbit_due", "status", "next_orbit_review_at"),
        Index("ix_discovery_watches_history_due", "status", "next_history_review_at"),
        Index("ix_discovery_watches_first_seen", "first_seen_at", "pair_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(200))
    chain: Mapped[str] = mapped_column(String(60))
    network: Mapped[str] = mapped_column(String(60))
    pair_id: Mapped[str] = mapped_column(String(512))
    is_fixture: Mapped[bool] = mapped_column(Boolean)
    # The canonical `MarketIdentity`, pool locator included. Refresh addresses
    # the market by this and nothing else.
    market_payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latest_snapshot_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    next_orbit_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last checkpoint an ORBIT review was taken for. NULL before the first.
    orbit_checkpoint_index: Mapped[int | None] = mapped_column(Integer)
    next_history_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_vector_sufficiency: Mapped[str | None] = mapped_column(String(60))
    vector_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str] = mapped_column(String(80))
    last_promoted_trade_case_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trade_cases.id", ondelete="SET NULL")
    )


class DiscoveryWatchAssessmentRow(Base):
    """One scout ORBIT review of one watch at one checkpoint. Append-only.

    Discovery history, never case evidence: nothing reads these rows into a
    TradeCase. A failed review is recorded as FAILED with its reason and no
    classification, so a contradicted answer can never be read back as a valid
    one.
    """

    __tablename__ = "discovery_watch_assessments"
    __table_args__ = (
        UniqueConstraint("watch_id", "checkpoint_index", name="uq_discovery_watch_checkpoint"),
        CheckConstraint(
            "status IN ('COMPLETED', 'FAILED')", name="discovery_watch_assessment_status"
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (classification IS NOT NULL)",
            name="discovery_watch_assessment_classified",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (failure_reason IS NOT NULL)",
            name="discovery_watch_assessment_failure_named",
        ),
        CheckConstraint(
            "checkpoint_index >= 0 AND checkpoint_seconds >= 0",
            name="discovery_watch_assessment_checkpoint",
        ),
        Index("ix_discovery_watch_assessments_watch_time", "watch_id", "assessed_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    watch_id: Mapped[UUID] = mapped_column(ForeignKey("discovery_watches.id", ondelete="RESTRICT"))
    snapshot_id: Mapped[UUID] = mapped_column(Uuid)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checkpoint_index: Mapped[int] = mapped_column(Integer)
    checkpoint_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    failure_reason: Mapped[str | None] = mapped_column(String(80))
    classification: Mapped[str | None] = mapped_column(String(40))
    strength: Mapped[str | None] = mapped_column(String(20))
    reason_codes: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    data_gaps: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    cited_observation_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    summary: Mapped[str | None] = mapped_column(String(400))
    input_digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(40))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    output_schema_version: Mapped[int] = mapped_column(Integer)
    reasoning_provider: Mapped[str | None] = mapped_column(String(200))
    reasoning_model: Mapped[str | None] = mapped_column(String(200))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class ScoutRunRow(Base):
    """One `--scout-once` execution that reached the scout, as it ended.

    Written once, when the run is over, so a row is always terminal. Counts and
    typed codes only: no provider payload, token name, address or model text.
    Runs refused before they started (the scout disabled, another run holding
    the lock) are not runs and leave no row.
    """

    __tablename__ = "scout_runs"
    __table_args__ = (
        CheckConstraint("status IN ('COMPLETED', 'STOPPED', 'FAILED')", name="scout_run_status"),
        CheckConstraint("completed_at >= started_at", name="scout_run_ordering"),
        Index("ix_scout_runs_started", "started_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    stop: Mapped[str] = mapped_column(String(80))
    errors: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    policy_version: Mapped[str] = mapped_column(String(40))
    discovered: Mapped[int] = mapped_column(Integer)
    valid_markets: Mapped[int] = mapped_column(Integer)
    provider_identity_rejects: Mapped[int] = mapped_column(Integer)
    other_provider_rejects: Mapped[int] = mapped_column(Integer)
    watches_created: Mapped[int] = mapped_column(Integer)
    watches_updated: Mapped[int] = mapped_column(Integer)
    bootstrapped: Mapped[int] = mapped_column(Integer)
    refreshed: Mapped[int] = mapped_column(Integer)
    watches_due_orbit: Mapped[int] = mapped_column(Integer)
    orbit_reviews_started: Mapped[int] = mapped_column(Integer)
    orbit_reviews_completed: Mapped[int] = mapped_column(Integer)
    interesting: Mapped[int] = mapped_column(Integer)
    not_interesting: Mapped[int] = mapped_column(Integer)
    insufficient_data: Mapped[int] = mapped_column(Integer)
    watches_due_history: Mapped[int] = mapped_column(Integer)
    history_checks: Mapped[int] = mapped_column(Integer)
    vector_sufficient: Mapped[int] = mapped_column(Integer)
    promotable_new: Mapped[int] = mapped_column(Integer)
    dormant_new: Mapped[int] = mapped_column(Integer)
    retired_new: Mapped[int] = mapped_column(Integer)
    provider_failures: Mapped[int] = mapped_column(Integer)
    model_failures: Mapped[int] = mapped_column(Integer)
    provider_requests: Mapped[int] = mapped_column(Integer)
    orbit_backlog_before: Mapped[int] = mapped_column(Integer)
    orbit_backlog_after: Mapped[int] = mapped_column(Integer)
    oldest_orbit_due_age_seconds: Mapped[int | None] = mapped_column(Integer)
    new_watches_without_orbit_assessment: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
