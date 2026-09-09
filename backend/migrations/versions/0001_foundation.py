"""Phase 0 typed events, spot positions, and one serialized paper portfolio."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def event_table(name: str, *columns: sa.Column) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        *columns,
    )
    op.create_index(f"ix_{name}_correlation_id", name, ["correlation_id"])


def reference(name: str, target: str, unique: bool = False) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=False, unique=unique)


def upgrade() -> None:
    event_table("market_snapshots")
    event_table("agent_decisions")
    event_table("trade_intents")
    event_table(
        "risk_decisions",
        reference("intent_id", "trade_intents.id", True),
        reference("market_snapshot_id", "market_snapshots.id"),
    )
    event_table(
        "order_intents",
        reference("intent_id", "trade_intents.id", True),
        reference("risk_id", "risk_decisions.id", True),
    )
    event_table(
        "execution_results",
        reference("order_id", "order_intents.id", True),
        reference("intent_id", "trade_intents.id", True),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.String(200), nullable=False, unique=True),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("cost_basis_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("realized_pnl_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="position_quantity_nonnegative"),
        sa.CheckConstraint("cost_basis_usd >= 0", name="position_basis_nonnegative"),
    )
    op.create_index("ix_positions_correlation_id", "positions", ["correlation_id"])
    event_table(
        "trades",
        reference("execution_id", "execution_results.id", True),
        reference("position_id", "positions.id"),
    )
    event_table("pnl_snapshots")
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cash_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("initial_cash_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("fees_paid_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("loss_day", sa.Date(), nullable=False),
        sa.Column("realized_loss_today_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1", name="single_paper_account"),
        sa.CheckConstraint("cash_usd >= 0", name="account_cash_nonnegative"),
    )
    op.execute(
        sa.text(
            "INSERT INTO paper_accounts "
            "(id, cash_usd, initial_cash_usd, fees_paid_usd, loss_day, "
            "realized_loss_today_usd, paused) "
            "VALUES (1, 10000, 10000, 0, CURRENT_DATE, 0, false)"
        )
    )


def downgrade() -> None:
    for name in (
        "paper_accounts",
        "pnl_snapshots",
        "trades",
        "positions",
        "execution_results",
        "order_intents",
        "risk_decisions",
        "trade_intents",
        "agent_decisions",
        "market_snapshots",
    ):
        op.drop_table(name)
