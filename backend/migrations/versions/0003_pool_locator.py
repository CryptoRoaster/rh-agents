"""Version-2 explicit pool locators; exact market decimals remain JSONB strings.

Manager resolution remains observation metadata in JSONB, not indexed identity.
The stable pair ID format needs no further schema change.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "market_observations", "pair_id", existing_type=sa.String(200), type_=sa.String(512)
    )
    op.drop_constraint("market_observation_version", "market_observations", type_="check")
    op.create_check_constraint(
        "market_observation_version", "market_observations", "schema_version IN (1, 2)"
    )


def downgrade() -> None:
    # Never erase/rewrite append-only observations to make a downgrade succeed.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM market_observations
                       WHERE schema_version <> 1 OR length(pair_id) > 200) THEN
                RAISE EXCEPTION 'Downgrade blocked: incompatible observations exist';
            END IF;
        END $$;
    """)
    op.drop_constraint("market_observation_version", "market_observations", type_="check")
    op.create_check_constraint(
        "market_observation_version", "market_observations", "schema_version = 1"
    )
    op.alter_column(
        "market_observations", "pair_id", existing_type=sa.String(512), type_=sa.String(200)
    )
