"""Scout assessments record the reasoning effort: requested, and as reported.

`reasoning_effort` is the effort the review asked for. `reported_effort` is the
effort the provider said it used, and stays NULL when the provider says
nothing. Codex 0.153.4 does not report one, so only the requested value is
known for its reviews; neither column pretends otherwise.

**Structure only.** Earlier assessments keep NULL in both: what they asked for
was not recorded and is not reconstructed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovery_watch_assessments", sa.Column("reasoning_effort", sa.String(40), nullable=True)
    )
    op.add_column(
        "discovery_watch_assessments", sa.Column("reported_effort", sa.String(40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("discovery_watch_assessments", "reported_effort")
    op.drop_column("discovery_watch_assessments", "reasoning_effort")
