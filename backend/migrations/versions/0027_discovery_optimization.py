"""Local discovery optimisation: network binding cache, pool age, request accounting.

**`provider_network_bindings`.** One row per (provider, chain): the network id
the scout validated against the paginated `/networks` directory, the platform
it was validated for, and when. Later runs reuse it only within the configured
TTL and only for that same id and platform.

**`discovery_watches.pool_created_at`.** The provider's own pool creation time
from the discovery answer. NULL is UNKNOWN; nothing is backfilled or estimated.

**`scout_runs`.** Four counters — `/networks` pages read, cache hits, cache
entries refused, every HTTP attempt — and the list of reasons a possible gap
stayed open.

**Structure only.** Existing rows read zero, an empty list or NULL.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

COUNTERS = (
    "network_requests",
    "network_cache_hits",
    "network_cache_rejected",
    "provider_http_attempts",
)


def upgrade() -> None:
    op.create_table(
        "provider_network_bindings",
        sa.Column("provider", sa.String(40), primary_key=True),
        sa.Column("chain", sa.String(60), primary_key=True),
        sa.Column("network_id", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(200), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "discovery_watches",
        sa.Column("pool_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name in COUNTERS:
        op.add_column(
            "scout_runs",
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.add_column(
        "scout_runs",
        sa.Column(
            "discovery_gap_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("scout_runs", "discovery_gap_reasons")
    for name in reversed(COUNTERS):
        op.drop_column("scout_runs", name)
    op.drop_column("discovery_watches", "pool_created_at")
    op.drop_table("provider_network_bindings")
