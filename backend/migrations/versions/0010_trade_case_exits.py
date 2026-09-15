"""One durable paper exit per entry, bound to everything it closes.

Nothing existing records that a position was sold *deliberately*, or which
authorization permitted it. `execution_results` holds the SELL fill, `trades`
holds the realised result and `positions` is left at zero — but none of them
names the entry it closes, and a position row that reached zero looks exactly
like one that was never opened.

This table is that join: the position, the TradeCase that opened it, the entry
fill, the market both happened in, and the exit's own SELL intent, order, fill
and risk decision. The uniqueness is the contract rather than a convenience —
one exit per position, per entry fill and per case — so a second sale of the
same holding cannot exist even if two callers race for it.

Additive. Nothing existing is altered, and no status is rewritten: the entry
case stays `EXECUTED`, which is terminal and still bars the market from opening
another case. A closed position is not a re-entry permit.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_case_exits",
        sa.Column("exit_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_execution_id",
            sa.Uuid(),
            sa.ForeignKey("trade_case_executions.case_execution_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("request_key", sa.String(200), nullable=False, unique=True),
        # The holding this closed, and the market it was held in. Both are
        # recorded rather than looked up later: the position row is left at zero
        # and the market it named can be edited, so an exit that only pointed at
        # them could not be checked afterwards.
        sa.Column("position_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("asset_id", sa.String(200), nullable=False),
        sa.Column("market_pair_id", sa.String(512), nullable=False),
        # The Phase 0 records this exit is made of, each unique in its own table.
        sa.Column("intent_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("order_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("execution_id", sa.Uuid(), nullable=False, unique=True),
        # The exit's own decision. The entry's approval authorised a purchase
        # and nothing else, so it is referenced by the entry row and never here.
        sa.Column("risk_decision_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("execution_price_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("fees_usd", sa.Numeric(38, 18), nullable=False),
        # What the sale actually produced. Signed: a loss is a real outcome, not
        # a missing value, and storing only the gains would be a lie by omission.
        sa.Column("realized_pnl_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("cost_basis_released_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="trade_case_exit_quantity_positive"),
        sa.CheckConstraint("execution_price_usd > 0", name="trade_case_exit_price_positive"),
        sa.CheckConstraint("notional_usd > 0", name="trade_case_exit_notional_positive"),
        sa.CheckConstraint("fees_usd >= 0", name="trade_case_exit_fees_nonnegative"),
        sa.CheckConstraint(
            "cost_basis_released_usd >= 0", name="trade_case_exit_basis_nonnegative"
        ),
        # One exit per case, beside the one entry per case that already holds.
        sa.UniqueConstraint("trade_case_id", name="uq_trade_case_exit_case"),
    )
    op.create_index(
        "ix_trade_case_exits_case_time", "trade_case_exits", ["trade_case_id", "recorded_at"]
    )
    op.create_index("ix_trade_case_exits_correlation", "trade_case_exits", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_trade_case_exits_correlation", table_name="trade_case_exits")
    op.drop_index("ix_trade_case_exits_case_time", table_name="trade_case_exits")
    op.drop_table("trade_case_exits")
