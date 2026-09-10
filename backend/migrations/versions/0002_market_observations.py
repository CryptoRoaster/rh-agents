"""Append-only normalized market observations, separate from trading evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("asset_id", sa.String(200), nullable=False),
        sa.Column("pair_id", sa.String(200), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshness_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("is_fixture", sa.Boolean(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="market_observation_version"),
    )
    op.create_index(
        "ix_observation_pair_time",
        "market_observations",
        ["provider", "pair_id", "observed_at", "id"],
    )
    op.create_index("ix_observation_asset_time", "market_observations", ["asset_id", "observed_at"])
    op.create_index("ix_observation_observed_at", "market_observations", ["observed_at"])
    op.execute("""
        CREATE FUNCTION reject_market_observation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'market_observations is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER market_observations_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON market_observations
        FOR EACH STATEMENT EXECUTE FUNCTION reject_market_observation_mutation()
    """)


def downgrade() -> None:
    op.drop_table("market_observations")
    op.execute("DROP FUNCTION reject_market_observation_mutation()")
