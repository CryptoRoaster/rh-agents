"""One durable trade request per TradeCase, with the whole basis it was judged on.

A risk request is allocated once and kept. An execution identity derived on
demand from live inputs cannot survive a crash and retry — the price will have
moved by then, so the retry would mint a different identity and the idempotency
it was supposed to provide disappears exactly when it is needed.

The unique constraint on `trade_case_id` is the contract, not an index: a case
gets one canonical trade request, and a changed data situation is not by itself
permission for another one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_case_risk_requests",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(200), nullable=False, unique=True),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        # The safety-evidence digest as it stood, copied rather than recomputed.
        sa.Column("risk_input_digest", sa.String(64), nullable=False),
        # The whole basis: sizing, intent, portfolio, limits, cost assumptions
        # and the source references behind each. Kept separate from the safety
        # digest above so neither is silently redefined by the other.
        sa.Column("risk_request_digest", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("intent_fingerprint", sa.String(64), nullable=False),
        sa.Column("requested_notional_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("risk_decision_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("risk_authorization", sa.String(40), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "basis",
            sa.JSON().with_variant(postgresql.JSONB, "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint("case_revision >= 1", name="trade_case_risk_request_revision_positive"),
        sa.CheckConstraint(
            "requested_notional_usd > 0", name="trade_case_risk_request_notional_positive"
        ),
        sa.CheckConstraint("quantity > 0", name="trade_case_risk_request_quantity_positive"),
        # One canonical trade request per case. The contract, enforced by the
        # database rather than by whoever remembers to check first.
        sa.UniqueConstraint("trade_case_id", name="uq_trade_case_risk_request_case"),
    )
    op.create_index(
        "ix_trade_case_risk_requests_case_time",
        "trade_case_risk_requests",
        ["trade_case_id", "recorded_at"],
    )
    op.create_index(
        "ix_trade_case_risk_requests_correlation", "trade_case_risk_requests", ["correlation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_case_risk_requests_correlation", table_name="trade_case_risk_requests")
    op.drop_index("ix_trade_case_risk_requests_case_time", table_name="trade_case_risk_requests")
    op.drop_table("trade_case_risk_requests")
