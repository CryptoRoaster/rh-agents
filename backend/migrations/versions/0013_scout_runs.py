"""Scout run history: one terminal row per scout execution.

Until now a scout run's coverage and backlog existed only as the summary it
printed. The cockpit needs them over time: how many pools were discovered, how
many had a valid identity, how many watches were created, and whether ORBIT is
keeping up with the checkpoints that fall due, and how much of the day's
paid-review budget was used.

`scout_orbit_reservations` makes the daily paid-ORBIT budget a hard bound: a
slot is reserved and committed before every scout model call, so a process that
dies mid-call still leaves its slot counted.

**Structure only.** No row is reconstructed from old output: runs before this
migration simply have no history. Existing watches and assessments are
untouched.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scout_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stop", sa.String(80), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("discovered", sa.Integer(), nullable=False),
        sa.Column("valid_markets", sa.Integer(), nullable=False),
        sa.Column("provider_identity_rejects", sa.Integer(), nullable=False),
        sa.Column("other_provider_rejects", sa.Integer(), nullable=False),
        sa.Column("watches_created", sa.Integer(), nullable=False),
        sa.Column("watches_updated", sa.Integer(), nullable=False),
        sa.Column("bootstrapped", sa.Integer(), nullable=False),
        sa.Column("refreshed", sa.Integer(), nullable=False),
        sa.Column("watches_due_orbit", sa.Integer(), nullable=False),
        sa.Column("orbit_reviews_started", sa.Integer(), nullable=False),
        sa.Column("orbit_reviews_completed", sa.Integer(), nullable=False),
        sa.Column("interesting", sa.Integer(), nullable=False),
        sa.Column("not_interesting", sa.Integer(), nullable=False),
        sa.Column("insufficient_data", sa.Integer(), nullable=False),
        sa.Column("watches_due_history", sa.Integer(), nullable=False),
        sa.Column("history_checks", sa.Integer(), nullable=False),
        sa.Column("vector_sufficient", sa.Integer(), nullable=False),
        sa.Column("promotable_new", sa.Integer(), nullable=False),
        sa.Column("dormant_new", sa.Integer(), nullable=False),
        sa.Column("retired_new", sa.Integer(), nullable=False),
        sa.Column("provider_failures", sa.Integer(), nullable=False),
        sa.Column("model_failures", sa.Integer(), nullable=False),
        sa.Column("provider_requests", sa.Integer(), nullable=False),
        sa.Column("orbit_backlog_before", sa.Integer(), nullable=False),
        sa.Column("orbit_backlog_after", sa.Integer(), nullable=False),
        sa.Column("oldest_orbit_due_age_seconds", sa.Integer(), nullable=True),
        sa.Column("new_watches_without_orbit_assessment", sa.Integer(), nullable=False),
        # The persistent daily bound on paid ORBIT calls, before and after the run.
        sa.Column("orbit_daily_budget", sa.Integer(), nullable=False),
        sa.Column("orbit_daily_used_before", sa.Integer(), nullable=False),
        sa.Column("orbit_daily_remaining_before", sa.Integer(), nullable=False),
        sa.Column("orbit_daily_used_after", sa.Integer(), nullable=False),
        sa.Column("orbit_daily_remaining_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # A row is written once, when the run is over, so it is always terminal.
        sa.CheckConstraint("status IN ('COMPLETED', 'STOPPED', 'FAILED')", name="scout_run_status"),
        sa.CheckConstraint("completed_at >= started_at", name="scout_run_ordering"),
    )
    op.create_index("ix_scout_runs_started", "scout_runs", ["started_at"])

    op.create_table(
        "scout_orbit_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "watch_id",
            sa.Uuid(),
            sa.ForeignKey("discovery_watches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checkpoint_index", sa.Integer(), nullable=False),
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(80), nullable=True),
        # One slot per watch checkpoint: a checkpoint is reviewed at most once.
        sa.UniqueConstraint("watch_id", "checkpoint_index", name="uq_scout_orbit_reservation"),
        # Every status counts toward the day; RESERVED is what a crash leaves.
        sa.CheckConstraint(
            "status IN ('RESERVED', 'COMPLETED', 'FAILED')",
            name="scout_orbit_reservation_status",
        ),
    )
    op.create_index("ix_scout_orbit_reservations_day", "scout_orbit_reservations", ["utc_day"])


def downgrade() -> None:
    op.drop_table("scout_orbit_reservations")
    op.drop_table("scout_runs")
