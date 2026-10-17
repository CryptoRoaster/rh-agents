"""The PoolManager-wide V4 pool index (issue #94).

**`v4_pool_index_coverage`.** One row per (chain id, PoolManager): its verified
deployment block and the last block the index has confirmed, with that block's
hash so a reorganisation can be detected before the index advances.

**`v4_pool_index_pools`.** Every Initialize the PoolManager emitted inside the
coverage, of any token: the pool id and the key it hashes from, and its block.
Looked up by either currency.

**Structure only.** No row is written. The index is filled by its own bounded
job, never by a migration, and the census reads it only when it is enabled.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "v4_pool_index_coverage",
        sa.Column("chain_id", sa.Integer(), primary_key=True),
        sa.Column("pool_manager", sa.String(42), primary_key=True),
        sa.Column("deployment_block", sa.BigInteger(), nullable=False),
        sa.Column("through_block", sa.BigInteger(), nullable=False),
        sa.Column("through_hash", sa.String(66), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("deployment_block >= 0", name="v4_index_deployment"),
        sa.CheckConstraint("through_block >= deployment_block - 1", name="v4_index_through"),
    )
    op.create_table(
        "v4_pool_index_pools",
        sa.Column("chain_id", sa.Integer(), primary_key=True),
        sa.Column("pool_manager", sa.String(42), primary_key=True),
        sa.Column("pool_id", sa.String(66), primary_key=True),
        sa.Column("currency0", sa.String(42), nullable=False),
        sa.Column("currency1", sa.String(42), nullable=False),
        sa.Column("fee", sa.Integer(), nullable=False),
        sa.Column("tick_spacing", sa.Integer(), nullable=False),
        sa.Column("hooks", sa.String(42), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_v4_index_currency0", "v4_pool_index_pools", ["chain_id", "pool_manager", "currency0"]
    )
    op.create_index(
        "ix_v4_index_currency1", "v4_pool_index_pools", ["chain_id", "pool_manager", "currency1"]
    )


def downgrade() -> None:
    op.drop_index("ix_v4_index_currency1", table_name="v4_pool_index_pools")
    op.drop_index("ix_v4_index_currency0", table_name="v4_pool_index_pools")
    op.drop_table("v4_pool_index_pools")
    op.drop_table("v4_pool_index_coverage")
