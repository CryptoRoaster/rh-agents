"""One durable paper fill per TradeCase, bound to the risk request that authorised it.

`execution_results.intent_id` is already unique, and the intent identity derives
from the stored request, so a second fill for one order is impossible by
construction. What is missing is the case reference: an execution row names no
case, so nothing joins a fill back to the authorization it was allowed under.
This table is that join, plus the fill-time re-check the fill actually rested on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_case_executions",
        sa.Column("case_execution_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("trade_case_risk_requests.request_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("request_key", sa.String(200), nullable=False),
        # The Phase 0 records this fill is made of. Each is unique in its own
        # table; repeating the identity here is what makes the chain from case
        # to fill answerable in one read.
        sa.Column("intent_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("order_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("execution_id", sa.Uuid(), nullable=False, unique=True),
        # The authorization that permitted this, and the re-check immediately
        # before the fill. Two decisions, kept apart: the first is not rewritten.
        sa.Column("authorizing_binding_id", sa.Uuid(), nullable=False),
        sa.Column("recheck_decision_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("risk_input_digest", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("execution_price_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("fees_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="trade_case_execution_quantity_positive"),
        sa.CheckConstraint("execution_price_usd > 0", name="trade_case_execution_price_positive"),
        sa.CheckConstraint("notional_usd > 0", name="trade_case_execution_notional_positive"),
        sa.CheckConstraint("fees_usd >= 0", name="trade_case_execution_fees_nonnegative"),
        # One entry per case. A case asked one question, got one authorization
        # and spends it once; re-entry has no contract yet.
        sa.UniqueConstraint("trade_case_id", name="uq_trade_case_execution_case"),
    )
    op.create_index(
        "ix_trade_case_executions_case_time",
        "trade_case_executions",
        ["trade_case_id", "recorded_at"],
    )
    op.create_index(
        "ix_trade_case_executions_correlation", "trade_case_executions", ["correlation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_case_executions_correlation", table_name="trade_case_executions")
    op.drop_index("ix_trade_case_executions_case_time", table_name="trade_case_executions")
    op.drop_table("trade_case_executions")
