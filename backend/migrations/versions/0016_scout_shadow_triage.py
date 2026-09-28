"""Stream declines (the watch limit holds) and JEV shadow fast assessments.

- `discovery_stream_declines`: a discovered stream that the per-run watch
  limit turned away. The recovery bootstrap skips it, so the limit is no
  longer doubled one run later. Observations are untouched.
- `discovery_watch_fast_assessments`: one shadow fast assessment per new watch
  and question set. Reserved before the provider call and settled once;
  provenance, status and failures are columns, the versioned typed input and
  answers are JSONB. Shadow only: nothing reads it for a decision.

**Structure only.** No row is created for existing watches or streams; there
is no backfill.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_stream_declines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("pair_id", sa.String(512), nullable=False),
        sa.Column("is_fixture", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "chain",
            "network",
            "pair_id",
            "is_fixture",
            name="uq_discovery_stream_decline",
        ),
    )
    op.create_table(
        "discovery_watch_fast_assessments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "watch_id",
            sa.Uuid(),
            sa.ForeignKey("discovery_watches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.Uuid(),
            sa.ForeignKey("market_observations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("model_version", sa.String(80), nullable=True),
        sa.Column("question_version", sa.String(40), nullable=False),
        sa.Column("input_schema_version", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("failure_category", sa.String(80), nullable=True),
        sa.Column("failure_reason_code", sa.String(80), nullable=True),
        sa.UniqueConstraint(
            "watch_id", "question_version", name="uq_fast_assessment_watch_questions"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'FAILED')", name="fast_assessment_status"
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (answers IS NOT NULL)", name="fast_assessment_answered"
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_category IS NOT NULL)",
            name="fast_assessment_failure_named",
        ),
        sa.CheckConstraint(
            "failure_reason_code IS NULL OR status = 'FAILED'",
            name="fast_assessment_failure_code",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING') = (assessed_at IS NULL)", name="fast_assessment_settled"
        ),
    )
    op.create_index("ix_fast_assessments_day", "discovery_watch_fast_assessments", ["utc_day"])
    op.create_index(
        "ix_fast_assessments_watch_time",
        "discovery_watch_fast_assessments",
        ["watch_id", "reserved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_fast_assessments_watch_time", table_name="discovery_watch_fast_assessments")
    op.drop_index("ix_fast_assessments_day", table_name="discovery_watch_fast_assessments")
    op.drop_table("discovery_watch_fast_assessments")
    op.drop_table("discovery_stream_declines")
