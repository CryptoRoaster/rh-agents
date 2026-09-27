"""Scout model failures keep their exact reason, not only their category.

A failed scout ORBIT review recorded only its `ReasoningErrorCategory`, so four
failed Codex reviews under the launch agent showed as `PROVIDER_TIMEOUT` and
`PROVIDER_NOT_CONFIGURED` with nothing to tell a refused sandbox gate from a
probe that ran out of time. Two additions close that:

- `discovery_watch_assessments.failure_reason_code`: the provider's sanitised
  reason code beside the existing category (`failure_reason`). Only a failed
  review may carry one.
- `scout_runs.model_failure_reasons`: per run, failures counted by provider,
  category and reason code.

**Structure only.** Earlier rows keep NULL and an empty list: their reason
codes were never recorded and are not reconstructed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovery_watch_assessments",
        sa.Column("failure_reason_code", sa.String(80), nullable=True),
    )
    op.create_check_constraint(
        "discovery_watch_assessment_failure_code",
        "discovery_watch_assessments",
        "failure_reason_code IS NULL OR status = 'FAILED'",
    )
    op.add_column(
        "scout_runs",
        sa.Column(
            "model_failure_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("scout_runs", "model_failure_reasons")
    op.drop_constraint(
        "discovery_watch_assessment_failure_code", "discovery_watch_assessments", type_="check"
    )
    op.drop_column("discovery_watch_assessments", "failure_reason_code")
