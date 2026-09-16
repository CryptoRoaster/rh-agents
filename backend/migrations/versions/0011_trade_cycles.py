"""Name the trading cycle a position, entry and exit belong to.

Until now "one position per asset" and "one trade per market" were the same
sentence. A position row reached zero and stayed, and every record that pointed
at it pointed at the only entry there had ever been. Re-entry breaks that: the
row is reused, a second entry and a second exit appear beside the first, and
"which entry does this holding come from?" stops having a single answer.

`trade_cycles` is that answer. One row per trading cycle — a case that opened a
position and the exit that closed it — carrying its market, its sequence, and
the exit it succeeds. `predecessor_exit_id` is unique, so a completed cycle has
at most one successor; `trade_case_id` is unique, so a case belongs to one
cycle.

The existing tables gain a `cycle_id` each, and `trade_case_exits` moves its
"one exit per position" uniqueness onto the cycle. Selling one holding twice
inside a cycle stays impossible; selling it once per cycle becomes possible,
which is the whole point.

**The backfill is a mapping, not a guess.** Every `trade_case_executions` row
already names exactly one case, and the case names exactly one market, so each
existing entry becomes cycle 1 of its own market with no predecessor. Each
existing exit inherits the cycle of the entry it already references. Open
positions are matched to their cycle only where exactly one executed entry
accounts for them; where none or several do, `cycle_id` stays NULL and the exit
path refuses exactly as it refuses today. Nothing is invented to fill a column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_cycles",
        sa.Column("cycle_id", sa.Uuid(), primary_key=True),
        # The case that owns this cycle. One case opens at most one position, so
        # the two identify each other.
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("asset_id", sa.String(200), nullable=False),
        sa.Column("market_pair_id", sa.String(512), nullable=False),
        # 1 for a market's first cycle, and one more for each explicit re-entry.
        sa.Column("sequence", sa.Integer(), nullable=False),
        # The exit this cycle succeeds, NULL for a first cycle. Unique, so a
        # completed cycle can be followed once and only once — a second request
        # is refused rather than opening a parallel successor.
        #
        # Deliberately not a foreign key: an exit already points at its cycle,
        # and pointing back would make the two tables mutually dependent, which
        # no schema tool can order and no fresh database can create. The value
        # is read under the account and case locks from the exit row itself, so
        # it is checked where it is used rather than asserted where it cannot be.
        sa.Column("predecessor_exit_id", sa.Uuid(), nullable=True, unique=True),
        # The re-entry key that opened this cycle, NULL for a first cycle.
        sa.Column("request_key", sa.String(200), nullable=True, unique=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="trade_cycle_sequence_positive"),
        # A first cycle has neither a predecessor nor a request key; every later
        # one has both. Half of either would be a cycle nobody could place.
        sa.CheckConstraint(
            "(predecessor_exit_id IS NULL) = (request_key IS NULL)",
            name="trade_cycle_succession_complete",
        ),
        sa.CheckConstraint(
            "(predecessor_exit_id IS NULL) = (sequence = 1)",
            name="trade_cycle_first_has_no_predecessor",
        ),
        sa.UniqueConstraint("market_pair_id", "sequence", name="uq_trade_cycle_market_sequence"),
    )
    op.create_index("ix_trade_cycles_asset", "trade_cycles", ["asset_id"])
    op.create_index("ix_trade_cycles_market", "trade_cycles", ["market_pair_id", "sequence"])

    op.add_column("trade_case_executions", sa.Column("cycle_id", sa.Uuid(), nullable=True))
    op.add_column("trade_case_exits", sa.Column("cycle_id", sa.Uuid(), nullable=True))
    op.add_column("positions", sa.Column("cycle_id", sa.Uuid(), nullable=True))

    _backfill()

    # Every existing row now has one, so the column is required from here on.
    op.alter_column("trade_case_executions", "cycle_id", nullable=False)
    op.alter_column("trade_case_exits", "cycle_id", nullable=False)
    op.create_unique_constraint(
        "uq_trade_case_execution_cycle", "trade_case_executions", ["cycle_id"]
    )
    op.create_foreign_key(
        "fk_trade_case_execution_cycle",
        "trade_case_executions",
        "trade_cycles",
        ["cycle_id"],
        ["cycle_id"],
        ondelete="RESTRICT",
    )
    # One exit per *cycle* rather than per position: the position row outlives
    # the cycle and is reused by the next one. Selling one holding twice inside
    # a cycle stays impossible.
    op.drop_constraint("trade_case_exits_position_id_key", "trade_case_exits", type_="unique")
    op.create_unique_constraint("uq_trade_case_exit_cycle", "trade_case_exits", ["cycle_id"])
    op.create_foreign_key(
        "fk_trade_case_exit_cycle",
        "trade_case_exits",
        "trade_cycles",
        ["cycle_id"],
        ["cycle_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_positions_cycle", "positions", ["cycle_id"])


def _backfill() -> None:
    """Give every existing entry, exit and open position the cycle it is in.

    Set-based on purpose: a row-by-row migration cannot be rendered as offline
    SQL, and an upgrade script a reviewer cannot read is not one this project
    ships. Every statement below is a mapping the existing rows already contain.

    A historical cycle takes the identity of the case that opened it, which is
    exactly how a new one is identified too — see `cycle_of`. The only
    dialect-dependent part is reading the base asset out of the case's market
    payload, which PostgreSQL and SQLite spell differently.
    """
    dialect = op.get_context().dialect.name
    asset = (
        "c.market_payload ->> 'base_asset_id'"
        if dialect == "postgresql"
        else "json_extract(c.market_payload, '$.base_asset_id')"
    )
    # One cycle per executed entry: sequence 1, no predecessor, no request key.
    op.execute(
        sa.text(
            "INSERT INTO trade_cycles (cycle_id, trade_case_id, asset_id, market_pair_id,"
            " sequence, predecessor_exit_id, request_key, opened_at, correlation_id)"
            f" SELECT e.trade_case_id, e.trade_case_id, {asset}, c.market_key, 1, NULL, NULL,"
            " e.recorded_at, e.correlation_id"
            " FROM trade_case_executions e JOIN trade_cases c ON c.id = e.trade_case_id"
        )
    )
    op.execute(sa.text("UPDATE trade_case_executions SET cycle_id = trade_case_id"))
    # An exit already names the case of the entry it closed, which is that
    # entry's cycle.
    op.execute(sa.text("UPDATE trade_case_exits SET cycle_id = trade_case_id"))
    # A holding belongs to a cycle only where exactly one executed entry accounts
    # for it. Where none or several do, the column stays NULL and the exit path
    # refuses for the same reason it refuses today, rather than being handed a
    # cycle nobody established.
    op.execute(
        sa.text(
            "UPDATE positions SET cycle_id = ("
            " SELECT c.cycle_id FROM trade_cycles c WHERE c.asset_id = positions.asset_id"
            " LIMIT 1)"
            " WHERE (SELECT COUNT(*) FROM trade_cycles c"
            " WHERE c.asset_id = positions.asset_id) = 1"
        )
    )


SUCCESSORS = (
    "SELECT COUNT(*) FROM trade_cycles WHERE sequence > 1 OR predecessor_exit_id IS NOT NULL"
)

REFUSAL = (
    "Downgrading 0011 is not supported once an explicit re-entry has opened a "
    "successor cycle. Two cycles share one reused position row, so the old "
    "one-exit-per-position uniqueness is no longer true of the data and cannot "
    "be restored; and the predecessor and idempotency links that say which "
    "cycle followed which exit live only in trade_cycles, so dropping the table "
    "would silently lose them. Deleting, merging or renumbering exits to make "
    "the old shape fit would destroy booked history, so this refuses instead. "
    "Restore from a backup taken before the re-entry if the older schema is "
    "genuinely required."
)


def _refuse_after_reentry() -> None:
    """Stop before any schema change if a successor cycle exists.

    A successor counts from the moment it is *opened*, filled or not: the row is
    what records which exit it followed and under which key, and that mapping
    cannot be rebuilt from anything the older schema keeps.

    Offline generation is covered rather than exempted. A script that quietly
    dropped the table because nobody could run the check would be exactly the
    silent bypass this guard exists to prevent, so the emitted SQL carries the
    same condition and fails loudly in the database that runs it.
    """
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "Offline downgrade of 0011 can only be generated for PostgreSQL, "
                "because the re-entry guard has no portable form. " + REFUSAL
            )
        op.execute(
            sa.text(
                "DO $$ BEGIN IF EXISTS ("
                " SELECT 1 FROM trade_cycles"
                " WHERE sequence > 1 OR predecessor_exit_id IS NOT NULL"
                f") THEN RAISE EXCEPTION '{REFUSAL}'; END IF; END $$;"
            )
        )
        return
    found = op.get_bind().execute(sa.text(SUCCESSORS)).scalar_one()
    if found:
        raise RuntimeError(REFUSAL)


def downgrade() -> None:
    # First, and before anything is altered: with successor cycles on file this
    # downgrade cannot be performed without losing records, so it is refused
    # rather than begun and abandoned half-way.
    _refuse_after_reentry()
    op.drop_index("ix_positions_cycle", table_name="positions")
    op.drop_constraint("fk_trade_case_exit_cycle", "trade_case_exits", type_="foreignkey")
    op.drop_constraint("uq_trade_case_exit_cycle", "trade_case_exits", type_="unique")
    op.create_unique_constraint(
        "trade_case_exits_position_id_key", "trade_case_exits", ["position_id"]
    )
    op.drop_constraint("fk_trade_case_execution_cycle", "trade_case_executions", type_="foreignkey")
    op.drop_constraint("uq_trade_case_execution_cycle", "trade_case_executions", type_="unique")
    op.drop_column("positions", "cycle_id")
    op.drop_column("trade_case_exits", "cycle_id")
    op.drop_column("trade_case_executions", "cycle_id")
    op.drop_index("ix_trade_cycles_market", table_name="trade_cycles")
    op.drop_index("ix_trade_cycles_asset", table_name="trade_cycles")
    op.drop_table("trade_cycles")
