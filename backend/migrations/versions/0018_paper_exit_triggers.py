"""Record why an automatic PAPER exit fired, on the exit it produced.

Three nullable columns on `trade_case_exits`: the trigger (STOP_LOSS,
SENTINEL_INVALIDATION, TAKE_PROFIT, TIME_EXIT), the policy version that
decided it, and the numbers it was decided on. Written in the same transaction
as the SELL fill and its ledger postings, so an automatic exit and its reason
cannot come apart. A direct exit keeps all three NULL; a trigger without a
policy version (or the reverse) is refused by the database.

**Structure only.** Existing exits keep NULL.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trade_case_exits", sa.Column("exit_trigger", sa.String(40), nullable=True))
    op.add_column(
        "trade_case_exits", sa.Column("exit_policy_version", sa.String(40), nullable=True)
    )
    op.add_column(
        "trade_case_exits", sa.Column("exit_trigger_basis", postgresql.JSONB(), nullable=True)
    )
    op.create_check_constraint(
        "trade_case_exit_trigger_versioned",
        "trade_case_exits",
        "(exit_trigger IS NULL) = (exit_policy_version IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("trade_case_exit_trigger_versioned", "trade_case_exits", type_="check")
    op.drop_column("trade_case_exits", "exit_trigger_basis")
    op.drop_column("trade_case_exits", "exit_policy_version")
    op.drop_column("trade_case_exits", "exit_trigger")
