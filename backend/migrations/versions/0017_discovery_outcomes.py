"""Objective outcomes for discovery candidates, and a shared OHLCV bar store.

- `market_ohlcv_fetches` / `market_ohlcv_bars`: closed OHLCV bars per stream
  with the window each read covered. The scout's VECTOR history check records
  its hourly read here; the outcome sampler labels from stored bars first and
  asks the provider only for what is not covered.
- `discovery_outcome_samples`: one row per labelled discovery stream (watched
  or declined), with how it was labelled and liquidity persistence.
- `discovery_stream_outcomes`: per stream and horizon (15m … 72h) return,
  maximum return, maximum drawdown, survival and volume persistence, measured
  against the price of the stream's first observation.

Labels only: nothing reads them for a decision. **Structure only**, no backfill;
prices are unconstrained NUMERIC because young tokens trade far below 1e-9 USD.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

STREAM = ("provider", "chain", "network", "pair_id", "is_fixture")


def _stream_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("provider", sa.String(200), nullable=False),
        sa.Column("chain", sa.String(60), nullable=False),
        sa.Column("network", sa.String(60), nullable=False),
        sa.Column("pair_id", sa.String(512), nullable=False),
        sa.Column("is_fixture", sa.Boolean(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "market_ohlcv_fetches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_stream_columns(),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("aggregate", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("bars_returned", sa.Integer(), nullable=False),
        sa.CheckConstraint("window_end > window_start", name="ohlcv_fetch_window"),
    )
    op.create_index("ix_ohlcv_fetches_stream", "market_ohlcv_fetches", list(STREAM))
    op.create_table(
        "market_ohlcv_bars",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "fetch_id",
            sa.Uuid(),
            sa.ForeignKey("market_ohlcv_fetches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_stream_columns(),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("aggregate", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.UniqueConstraint(*STREAM, "timeframe", "aggregate", "opened_at", name="uq_ohlcv_bar"),
        sa.CheckConstraint("low <= high AND volume >= 0", name="ohlcv_bar_shape"),
    )
    op.create_table(
        "discovery_outcome_samples",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_stream_columns(),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "reference_observation_id",
            sa.Uuid(),
            sa.ForeignKey("market_observations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reference_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price_usd", sa.Numeric(), nullable=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("history_source", sa.String(10), nullable=False),
        sa.Column("provider_requests", sa.Integer(), nullable=False),
        sa.Column("liquidity_at_reference_usd", sa.Numeric(), nullable=True),
        sa.Column("liquidity_at_sample_usd", sa.Numeric(), nullable=True),
        sa.UniqueConstraint(*STREAM, "schema_version", name="uq_outcome_sample_stream"),
        sa.CheckConstraint(
            "status IN ('COMPLETE', 'PARTIAL', 'UNAVAILABLE')", name="outcome_sample_status"
        ),
        sa.CheckConstraint(
            "history_source IN ('REUSED', 'FETCHED', 'NONE')", name="outcome_sample_source"
        ),
    )
    op.create_index("ix_outcome_samples_sampled", "discovery_outcome_samples", ["sampled_at"])
    op.create_table(
        "discovery_stream_outcomes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "sample_id",
            sa.Uuid(),
            sa.ForeignKey("discovery_outcome_samples.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_stream_columns(),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("missing_reason", sa.String(80), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("aggregate", sa.Integer(), nullable=True),
        sa.Column("bars_used", sa.Integer(), nullable=False),
        sa.Column("return_pct", sa.Numeric(), nullable=True),
        sa.Column("max_return_pct", sa.Numeric(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(), nullable=True),
        sa.Column("survived", sa.Boolean(), nullable=True),
        sa.Column("volume_usd", sa.Numeric(), nullable=True),
        sa.Column("second_half_volume_share", sa.Numeric(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            *STREAM, "horizon_minutes", "schema_version", name="uq_stream_outcome_horizon"
        ),
        sa.CheckConstraint("status IN ('LABELLED', 'MISSING')", name="stream_outcome_status"),
        sa.CheckConstraint(
            "(status = 'MISSING') = (missing_reason IS NOT NULL)", name="stream_outcome_missing"
        ),
        sa.CheckConstraint("horizon_minutes > 0", name="stream_outcome_horizon"),
    )
    op.create_index(
        "ix_stream_outcomes_horizon",
        "discovery_stream_outcomes",
        ["horizon_minutes", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_stream_outcomes_horizon", table_name="discovery_stream_outcomes")
    op.drop_table("discovery_stream_outcomes")
    op.drop_index("ix_outcome_samples_sampled", table_name="discovery_outcome_samples")
    op.drop_table("discovery_outcome_samples")
    op.drop_table("market_ohlcv_bars")
    op.drop_index("ix_ohlcv_fetches_stream", table_name="market_ohlcv_fetches")
    op.drop_table("market_ohlcv_fetches")
