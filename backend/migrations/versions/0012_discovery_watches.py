"""Discovery watches and their append-only ORBIT assessment history.

The early-discovery scout keeps looking at a young market for days, long after a
TradeCase — a short-lived decision workflow — would have ended. So the watch is
its own record rather than a case held open, and every scout ORBIT review is a
row of its own rather than an overwritten field: the development from T+0 to
T+24h is exactly what early discovery exists to observe.

`discovery_watches` has one row per market stream (provider, chain, network,
pair, fixture flag). `discovery_watch_assessments` is append-only by trigger,
has at most one row per watch and checkpoint, and can only point at an existing
watch.

**Structure only.** This migration materialises no watch from existing market
observations. Adopting streams recorded before the scout existed is a bounded,
idempotent application bootstrap, so the cost of this migration does not grow
with the size of the observation history.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_watches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("pair_id", sa.String(512), nullable=False),
        sa.Column("is_fixture", sa.Boolean(), nullable=False),
        sa.Column("market_payload", postgresql.JSONB(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("next_orbit_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orbit_checkpoint_index", sa.Integer(), nullable=True),
        sa.Column("next_history_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_vector_sufficiency", sa.String(60), nullable=True),
        sa.Column("vector_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column(
            "last_promoted_trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "provider",
            "chain",
            "network",
            "pair_id",
            "is_fixture",
            name="uq_discovery_watch_stream",
        ),
        # The lifecycle is operational only. An ORBIT classification is an
        # assessment, never a watch status.
        sa.CheckConstraint(
            "status IN ('WATCHING', 'PROMOTABLE', 'DORMANT', 'RETIRED')",
            name="discovery_watch_status",
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="discovery_watch_seen_order"),
        sa.CheckConstraint(
            "orbit_checkpoint_index IS NULL OR orbit_checkpoint_index >= 0",
            name="discovery_watch_checkpoint_index",
        ),
    )
    op.create_index(
        "ix_discovery_watches_orbit_due", "discovery_watches", ["status", "next_orbit_review_at"]
    )
    op.create_index(
        "ix_discovery_watches_history_due",
        "discovery_watches",
        ["status", "next_history_review_at"],
    )
    op.create_index(
        "ix_discovery_watches_first_seen", "discovery_watches", ["first_seen_at", "pair_id"]
    )

    op.create_table(
        "discovery_watch_assessments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "watch_id",
            sa.Uuid(),
            sa.ForeignKey("discovery_watches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkpoint_index", sa.Integer(), nullable=False),
        sa.Column("checkpoint_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("failure_reason", sa.String(80), nullable=True),
        sa.Column("classification", sa.String(40), nullable=True),
        sa.Column("strength", sa.String(20), nullable=True),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("data_gaps", postgresql.JSONB(), nullable=False),
        sa.Column("cited_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.String(400), nullable=True),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("output_schema_version", sa.Integer(), nullable=False),
        sa.Column("reasoning_provider", sa.String(200), nullable=True),
        sa.Column("reasoning_model", sa.String(200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        # One review per checkpoint, enforced where it cannot be raced past.
        sa.UniqueConstraint("watch_id", "checkpoint_index", name="uq_discovery_watch_checkpoint"),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'FAILED')", name="discovery_watch_assessment_status"
        ),
        # A failed review carries no classification, so it can never be read
        # back as a valid assessment; a completed one always carries one.
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (classification IS NOT NULL)",
            name="discovery_watch_assessment_classified",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_reason IS NOT NULL)",
            name="discovery_watch_assessment_failure_named",
        ),
        sa.CheckConstraint(
            "checkpoint_index >= 0 AND checkpoint_seconds >= 0",
            name="discovery_watch_assessment_checkpoint",
        ),
    )
    op.create_index(
        "ix_discovery_watch_assessments_watch_time",
        "discovery_watch_assessments",
        ["watch_id", "assessed_at"],
    )
    op.execute("""
        CREATE FUNCTION reject_discovery_watch_assessment_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'discovery_watch_assessments is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER discovery_watch_assessments_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON discovery_watch_assessments
        FOR EACH STATEMENT EXECUTE FUNCTION reject_discovery_watch_assessment_mutation()
    """)


def downgrade() -> None:
    op.drop_table("discovery_watch_assessments")
    op.execute("DROP FUNCTION reject_discovery_watch_assessment_mutation()")
    op.drop_table("discovery_watches")
