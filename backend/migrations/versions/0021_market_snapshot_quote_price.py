"""Version-3 market observations: the pool's own quote-asset USD price.

A version-3 payload carries `quote_price` beside `price`, both taken from the
same provider answer for the same pool. The payload is JSONB and needs no new
column; only the version check widens. Versions 1 and 2 are untouched.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("market_observation_version", "market_observations", type_="check")
    op.create_check_constraint(
        "market_observation_version", "market_observations", "schema_version IN (1, 2, 3)"
    )


def downgrade() -> None:
    # Never erase/rewrite append-only observations to make a downgrade succeed.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM market_observations WHERE schema_version = 3) THEN
                RAISE EXCEPTION 'Downgrade blocked: version-3 observations exist';
            END IF;
        END $$;
    """)
    op.drop_constraint("market_observation_version", "market_observations", type_="check")
    op.create_check_constraint(
        "market_observation_version", "market_observations", "schema_version IN (1, 2)"
    )
