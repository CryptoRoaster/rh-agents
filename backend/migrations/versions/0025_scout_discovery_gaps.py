"""Early discovery page coverage on the scout run history (issue #77).

`scout_runs` gains three counters:

- `discovery_no_overlap_polls`: chain polls whose full page 1 shared no pool
  with anything this system had recorded before the run;
- `discovery_page2_requests`: page-2 `new_pools` reads made; the UTC day's sum
  is the durable basis of the daily page-2 budget;
- `discovery_possible_gaps`: polls that still reached no recorded pool, so
  pools between this poll and the previous one may have been missed.

The overlap reference itself needs no table: it is the recorded market
observations, so a restart neither loses it nor re-creates a watch.

**Structure only.** Existing rows read zero; nothing is reconstructed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

COUNTERS = (
    "discovery_no_overlap_polls",
    "discovery_page2_requests",
    "discovery_possible_gaps",
)


def upgrade() -> None:
    for name in COUNTERS:
        op.add_column(
            "scout_runs",
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    for name in reversed(COUNTERS):
        op.drop_column("scout_runs", name)
