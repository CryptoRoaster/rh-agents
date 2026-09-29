"""Scout VECTOR history: retry state apart from the checkpoint, and run counters.

`discovery_watches` gains `history_retry_not_before`, `history_failure_count`
(bounded 0..10) and `history_last_failure` (a safe code, never a payload). A
failed history read sets a bounded backoff there; `next_history_review_at`
keeps meaning the 24/48/72h checkpoint. `scout_runs` gains the history
counters that make the fair queue and its own transport visible.

**Structure only.** Existing rows start with no retry state.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

COUNTERS = (
    "history_eligible_now",
    "history_current_selected",
    "history_catchup_selected",
    "history_provider_requests",
    "history_backoff_set",
    "history_rate_limited",
)


def upgrade() -> None:
    op.add_column(
        "discovery_watches",
        sa.Column("history_retry_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discovery_watches",
        sa.Column(
            "history_failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "discovery_watches", sa.Column("history_last_failure", sa.String(80), nullable=True)
    )
    op.create_check_constraint(
        "discovery_watch_history_failure_count",
        "discovery_watches",
        "history_failure_count >= 0 AND history_failure_count <= 10",
    )
    for name in COUNTERS:
        op.add_column(
            "scout_runs",
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.add_column(
        "scout_runs", sa.Column("oldest_history_due_age_seconds", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("scout_runs", "oldest_history_due_age_seconds")
    for name in reversed(COUNTERS):
        op.drop_column("scout_runs", name)
    op.drop_constraint("discovery_watch_history_failure_count", "discovery_watches", type_="check")
    op.drop_column("discovery_watches", "history_last_failure")
    op.drop_column("discovery_watches", "history_failure_count")
    op.drop_column("discovery_watches", "history_retry_not_before")
