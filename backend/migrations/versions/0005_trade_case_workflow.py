"""Durable deterministic TradeCase workflow and immutable audit history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_version", sa.String(40), nullable=False),
        sa.Column("market_key", sa.String(1200), nullable=False),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("originating_discovery_reference", sa.Uuid(), nullable=False),
        sa.Column("strategy_policy_id", sa.String(200)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("blockers", postgresql.JSONB(), nullable=False),
        sa.Column("risk_input_digest", sa.String(64)),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("open_idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("open_fingerprint", sa.String(64), nullable=False),
        sa.Column("market_payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="trade_case_revision_positive"),
    )
    op.create_index("ix_trade_cases_correlation_id", "trade_cases", ["correlation_id"])
    op.create_index("ix_trade_cases_status_updated", "trade_cases", ["status", "updated_at", "id"])
    op.create_index("ix_trade_cases_market", "trade_cases", ["chain", "network", "market_key"])

    op.create_table(
        "trade_case_tasks",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.UniqueConstraint(
            "trade_case_id", "role", "task_type", "attempt", name="uq_trade_case_task_attempt"
        ),
    )
    op.create_index("ix_trade_case_tasks_correlation_id", "trade_case_tasks", ["correlation_id"])
    op.create_index(
        "ix_trade_case_tasks_case_status", "trade_case_tasks", ["trade_case_id", "status"]
    )

    op.create_table(
        "trade_case_evidence",
        sa.Column("evidence_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("producer_role", sa.String(40), nullable=False),
        sa.Column("evidence_type", sa.String(60), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("trade_case_evidence.evidence_id")),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("submission_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_trade_case_evidence_correlation_id", "trade_case_evidence", ["correlation_id"]
    )
    op.create_index(
        "ix_trade_case_evidence_case_type",
        "trade_case_evidence",
        ["trade_case_id", "evidence_type"],
    )

    op.create_table(
        "trade_case_risk_bindings",
        sa.Column("binding_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("risk_decision_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("risk_input_digest", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("risk_authorization", sa.String(40), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("position_size_limit_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("max_additional_notional_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("max_slippage_bps", sa.Numeric(38, 18), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("trade_case_id", "case_revision", name="uq_trade_case_risk_revision"),
        sa.CheckConstraint(
            "position_size_limit_usd >= 0", name="trade_case_risk_position_limit_nonnegative"
        ),
        sa.CheckConstraint(
            "max_additional_notional_usd >= 0", name="trade_case_risk_notional_nonnegative"
        ),
        sa.CheckConstraint(
            "max_slippage_bps >= 0 AND max_slippage_bps <= 10000",
            name="trade_case_risk_slippage_bounded",
        ),
    )
    op.create_index(
        "ix_trade_case_risk_bindings_correlation_id", "trade_case_risk_bindings", ["correlation_id"]
    )
    op.create_index(
        "ix_trade_case_risk_case_time",
        "trade_case_risk_bindings",
        ["trade_case_id", "evaluated_at"],
    )

    op.create_table(
        "trade_case_transitions",
        sa.Column("transition_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=False),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("blockers", postgresql.JSONB(), nullable=False),
        sa.Column("risk_input_digest", sa.String(64)),
        sa.UniqueConstraint("trade_case_id", "revision", name="uq_trade_case_transition_revision"),
    )
    op.create_index(
        "ix_trade_case_transitions_correlation_id", "trade_case_transitions", ["correlation_id"]
    )
    op.create_index(
        "ix_trade_case_transition_case_time",
        "trade_case_transitions",
        ["trade_case_id", "recorded_at"],
    )

    op.create_table(
        "trade_case_events",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_trade_case_events_correlation_id", "trade_case_events", ["correlation_id"])
    op.create_index(
        "ix_trade_case_events_case_sequence", "trade_case_events", ["trade_case_id", "sequence"]
    )

    op.execute(
        """CREATE FUNCTION reject_trade_case_history_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'TradeCase history is append-only'; END $$"""
    )
    for table in (
        "trade_case_evidence",
        "trade_case_risk_bindings",
        "trade_case_transitions",
        "trade_case_events",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_trade_case_history_mutation()"
        )


def downgrade() -> None:
    for table in (
        "trade_case_events",
        "trade_case_transitions",
        "trade_case_risk_bindings",
        "trade_case_evidence",
        "trade_case_tasks",
        "trade_cases",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_trade_case_history_mutation()")
