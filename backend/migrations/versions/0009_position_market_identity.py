"""Record which market a PAPER position was acquired in.

A position names an asset, and an asset is not a market. Valuing one needs a
recorded observation, and observations are indexed by pair — a token can trade
in several pools, so picking "a" market for an asset would resolve an ambiguity
silently, in the one place where guessing wrong prices the whole portfolio.

Additive and nullable on purpose. Positions written before this exist and by
definition carry no market; they are reported as unvaluable rather than
back-filled with a guess.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

COLUMNS = ("market_pair_id", "market_chain", "market_network", "market_provider")


def upgrade() -> None:
    op.add_column("positions", sa.Column("market_pair_id", sa.String(512), nullable=True))
    op.add_column("positions", sa.Column("market_chain", sa.String(60), nullable=True))
    op.add_column("positions", sa.Column("market_network", sa.String(60), nullable=True))
    op.add_column("positions", sa.Column("market_provider", sa.String(200), nullable=True))
    op.create_index("ix_positions_market_pair", "positions", ["market_pair_id"])


def downgrade() -> None:
    op.drop_index("ix_positions_market_pair", table_name="positions")
    for column in reversed(COLUMNS):
        op.drop_column("positions", column)
