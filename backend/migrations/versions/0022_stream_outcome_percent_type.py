"""Stream outcome percents: the canonical NUMERIC(24, 6), on every installation.

Migration 0017 created `return_pct`, `max_return_pct` and `max_drawdown_pct`
as unbounded NUMERIC while the ORM declares NUMERIC(24, 6), so an installation
built from the chain and one built from the models disagreed — and the ORM's
bind cast, not the column, decided what fit. This makes the column the
contract: eighteen integer digits, six decimal places, as the sampler stores.

**Structure only.** Existing values are checked first and never rewritten: if
any value would not convert exactly (too large, or more than six decimal
places), the upgrade refuses instead of rounding it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

COLUMNS = ("return_pct", "max_return_pct", "max_drawdown_pct")


def upgrade() -> None:
    unconvertible = " OR ".join(
        f"({name} IS NOT NULL AND (abs({name}) >= 1e18 OR {name} <> round({name}, 6)))"
        for name in COLUMNS
    )
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM discovery_stream_outcomes WHERE {unconvertible}) THEN
                RAISE EXCEPTION 'Upgrade blocked: stream outcome percents not exactly convertible';
            END IF;
        END $$;
    """)
    for name in COLUMNS:
        op.alter_column(
            "discovery_stream_outcomes",
            name,
            existing_type=sa.Numeric(),
            type_=sa.Numeric(24, 6),
            existing_nullable=True,
        )


def downgrade() -> None:
    for name in COLUMNS:
        op.alter_column(
            "discovery_stream_outcomes",
            name,
            existing_type=sa.Numeric(24, 6),
            type_=sa.Numeric(),
            existing_nullable=True,
        )
