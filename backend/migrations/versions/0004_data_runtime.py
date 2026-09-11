"""Durable data runtime audit, cursors and separate EVM observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_audit",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(80), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_runtime_audit_stream_time", "runtime_audit", ["stream", "recorded_at", "id"]
    )
    op.create_index("ix_runtime_audit_run_id", "runtime_audit", ["run_id"])
    op.create_table(
        "evm_chain_cursors",
        sa.Column("chain", sa.String(60), primary_key=True),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("last_seen_head", sa.BigInteger(), nullable=False),
        sa.Column("last_safe_block", sa.BigInteger(), nullable=False),
        sa.Column("last_processed_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "chain IN ('robinhood', 'bsc') AND network = 'mainnet'", name="evm_cursor_chain"
        ),
        sa.CheckConstraint(
            "last_processed_block >= 0 AND last_safe_block >= 0 AND last_seen_head >= 0",
            name="evm_cursor_nonnegative",
        ),
    )
    op.create_table(
        "evm_log_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_evm_logs_chain_block", "evm_log_observations", ["chain", "block_number"])
    op.execute("""CREATE FUNCTION reject_runtime_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Runtime history is append-only'; END $$""")
    for table in ("runtime_audit", "evm_log_observations"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_runtime_mutation()"
        )


def downgrade() -> None:
    op.drop_table("evm_log_observations")
    op.drop_table("evm_chain_cursors")
    op.drop_table("runtime_audit")
    op.execute("DROP FUNCTION reject_runtime_mutation()")
