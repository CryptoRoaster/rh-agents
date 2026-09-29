"""EARLY_SCOUT_V2: record what became of each watch's ORBIT review debt.

`discovery_watches.orbit_state` (+ `orbit_state_at`): NULL while a first review
is pending; `REVIEWED` once taken; `ORBIT_FIRST_REVIEW_SKIPPED_STALE` when no
review was selected within the freshness window (closed without a model call);
`ORBIT_FOLLOW_UPS_DEFERRED` for V1 watches whose time-based follow-ups are
closed rather than taken. Four run counters make the settlement and the budget
pacing visible per scout run.

**Structure only.** No row is changed here: the scout's own run settles
existing review debt, deterministically and without model calls. Historical
assessments are untouched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

COUNTERS = (
    "orbit_fresh_first_reviews_due",
    "orbit_first_reviews_skipped_stale",
    "orbit_follow_ups_deferred",
    "orbit_slots_released",
)


def upgrade() -> None:
    op.add_column("discovery_watches", sa.Column("orbit_state", sa.String(40), nullable=True))
    op.add_column(
        "discovery_watches",
        sa.Column("orbit_state_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "discovery_watch_orbit_state",
        "discovery_watches",
        "orbit_state IS NULL OR orbit_state IN ('REVIEWED', "
        "'ORBIT_FIRST_REVIEW_SKIPPED_STALE', 'ORBIT_FOLLOW_UPS_DEFERRED')",
    )
    op.create_check_constraint(
        "discovery_watch_orbit_state_dated",
        "discovery_watches",
        "(orbit_state IS NULL) = (orbit_state_at IS NULL)",
    )
    for name in COUNTERS:
        op.add_column(
            "scout_runs",
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    for name in reversed(COUNTERS):
        op.drop_column("scout_runs", name)
    op.drop_constraint("discovery_watch_orbit_state_dated", "discovery_watches", type_="check")
    op.drop_constraint("discovery_watch_orbit_state", "discovery_watches", type_="check")
    op.drop_column("discovery_watches", "orbit_state_at")
    op.drop_column("discovery_watches", "orbit_state")
