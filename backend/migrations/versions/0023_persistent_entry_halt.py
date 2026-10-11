"""The persistent entry-only halt on the paper account, and its append-only audit.

Two halts on `paper_accounts`, set and cleared apart: an automatic one
(`entry_halt_auto*`) and a manual one (`entry_halt_manual*`). Either stops every
new BUY; neither stops a SELL. `paused` — the full system stop — is a different
control and is **neither read nor written** here, upgrade or downgrade: a paused
account stays paused.

`paper_control_events` records every change and every refused change of a halt.
It is append-only by trigger, like the other audit tables here.

**No unaudited halt change.** Every row a transaction writes to
`paper_control_events` carries that transaction's id (`txid`, a server default).
A deferred constraint trigger on `paper_accounts` runs at commit for every row
whose halt fields changed and refuses the commit unless an event of the *same*
transaction accounts for the change: the same control, the halt state it ended
in, and — for a halt now in force — the same reason. Deferred, so the order in
which a transaction writes the account and the event does not matter; at commit,
so a transaction cannot change a halt and commit without its event. What this
does not stop is a role that may also write events from writing a matching one;
it makes every change *recorded*, not *authorised* (see the operator CLI).

**Structure only.** Both halts arrive cleared, no event is written, and the
existing account row is otherwise untouched.

**Downgrade refuses** while a halt is in force or any event exists: dropping
either would silently lift a halt or erase its history.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

REASON = "^[A-Z][A-Z0-9_]{0,63}$"
REFUSAL = (
    "Downgrade of 0023 refused: an entry halt is in force or its audit history exists. "
    "Dropping either would lift a halt or erase who set it."
)
GUARD = (
    "SELECT (SELECT COUNT(*) FROM paper_accounts"
    " WHERE entry_halt_auto OR entry_halt_manual)"
    " + (SELECT COUNT(*) FROM paper_control_events)"
)


def upgrade() -> None:
    for name, column in (
        ("entry_halt_auto", sa.Boolean()),
        ("entry_halt_manual", sa.Boolean()),
    ):
        op.add_column(
            "paper_accounts",
            sa.Column(name, column, nullable=False, server_default=sa.text("false")),
        )
    op.add_column("paper_accounts", sa.Column("entry_halt_auto_reason", sa.String(64)))
    op.add_column("paper_accounts", sa.Column("entry_halt_auto_at", sa.DateTime(timezone=True)))
    op.add_column("paper_accounts", sa.Column("entry_halt_auto_loss_day", sa.Date()))
    op.add_column("paper_accounts", sa.Column("entry_halt_auto_decision_id", sa.Uuid()))
    op.add_column("paper_accounts", sa.Column("entry_halt_manual_reason", sa.String(64)))
    op.add_column("paper_accounts", sa.Column("entry_halt_manual_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "entry_halt_auto_consistent",
        "paper_accounts",
        "(entry_halt_auto AND entry_halt_auto_reason IS NOT NULL"
        " AND entry_halt_auto_at IS NOT NULL AND entry_halt_auto_loss_day IS NOT NULL)"
        " OR (NOT entry_halt_auto AND entry_halt_auto_reason IS NULL"
        " AND entry_halt_auto_at IS NULL AND entry_halt_auto_loss_day IS NULL"
        " AND entry_halt_auto_decision_id IS NULL)",
    )
    op.create_check_constraint(
        "entry_halt_manual_consistent",
        "paper_accounts",
        "(entry_halt_manual AND entry_halt_manual_reason IS NOT NULL"
        " AND entry_halt_manual_at IS NOT NULL)"
        " OR (NOT entry_halt_manual AND entry_halt_manual_reason IS NULL"
        " AND entry_halt_manual_at IS NULL)",
    )
    op.create_check_constraint(
        "entry_halt_reason_format",
        "paper_accounts",
        f"(entry_halt_auto_reason IS NULL OR entry_halt_auto_reason ~ '{REASON}')"
        f" AND (entry_halt_manual_reason IS NULL OR entry_halt_manual_reason ~ '{REASON}')",
    )

    op.create_table(
        "paper_control_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        # The writing transaction, for the commit-time audit check. Never
        # supplied by a writer: always the database's own.
        sa.Column(
            "txid",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("txid_current()"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("control", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("refusal_code", sa.String(64)),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_ref", sa.String(64)),
        sa.Column("halted_before", sa.Boolean(), nullable=False),
        sa.Column("halted_after", sa.Boolean(), nullable=False),
        sa.Column("loss_day", sa.Date()),
        sa.Column("realized_loss_today_usd", sa.Numeric(38, 18)),
        sa.Column("daily_loss_limit_usd", sa.Numeric(38, 18)),
        sa.Column("risk_decision_id", sa.Uuid()),
        sa.Column("run_id", sa.Uuid()),
        sa.CheckConstraint(
            "control IN ('ENTRY_HALT_AUTO', 'ENTRY_HALT_MANUAL')",
            name="paper_control_event_control",
        ),
        sa.CheckConstraint(
            "action IN ('SET', 'CLEAR', 'REFUSED')", name="paper_control_event_action"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('OPERATOR', 'SENTINEL', 'LEDGER')", name="paper_control_event_actor"
        ),
        sa.CheckConstraint(
            "(action = 'REFUSED') = (refusal_code IS NOT NULL)",
            name="paper_control_event_refusal",
        ),
        sa.CheckConstraint(
            f"reason_code ~ '{REASON}' AND (refusal_code IS NULL OR refusal_code ~ '{REASON}')",
            name="paper_control_event_codes",
        ),
        # A change ends where it says: SET leaves the halt in force, CLEAR lifts
        # it, a refusal leaves it as it was.
        sa.CheckConstraint(
            "(action = 'SET' AND halted_after)"
            " OR (action = 'CLEAR' AND halted_before AND NOT halted_after)"
            " OR (action = 'REFUSED' AND halted_before = halted_after)",
            name="paper_control_event_transition",
        ),
    )
    op.create_index(
        "ix_paper_control_events_time", "paper_control_events", ["occurred_at", "sequence"]
    )
    op.create_index("ix_paper_control_events_txid", "paper_control_events", ["txid"])

    op.execute(
        """CREATE FUNCTION reject_paper_control_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'paper_control_events is append-only'; END $$"""
    )
    op.execute(
        "CREATE TRIGGER paper_control_events_append_only "
        "BEFORE UPDATE OR DELETE OR TRUNCATE ON paper_control_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_paper_control_event_mutation()"
    )
    op.execute(
        """CREATE FUNCTION require_audited_entry_halt()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            auto_before boolean := false;
            manual_before boolean := false;
            auto_changed boolean;
            manual_changed boolean;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                auto_before := OLD.entry_halt_auto;
                manual_before := OLD.entry_halt_manual;
                auto_changed := (OLD.entry_halt_auto, OLD.entry_halt_auto_reason,
                    OLD.entry_halt_auto_at, OLD.entry_halt_auto_loss_day,
                    OLD.entry_halt_auto_decision_id)
                    IS DISTINCT FROM (NEW.entry_halt_auto, NEW.entry_halt_auto_reason,
                    NEW.entry_halt_auto_at, NEW.entry_halt_auto_loss_day,
                    NEW.entry_halt_auto_decision_id);
                manual_changed := (OLD.entry_halt_manual, OLD.entry_halt_manual_reason,
                    OLD.entry_halt_manual_at)
                    IS DISTINCT FROM (NEW.entry_halt_manual, NEW.entry_halt_manual_reason,
                    NEW.entry_halt_manual_at);
            ELSE
                auto_changed := NEW.entry_halt_auto;
                manual_changed := NEW.entry_halt_manual;
            END IF;
            IF auto_changed AND NOT EXISTS (
                SELECT 1 FROM paper_control_events
                WHERE txid = txid_current()
                  AND control = 'ENTRY_HALT_AUTO'
                  AND action IN ('SET', 'CLEAR')
                  AND halted_before = auto_before
                  AND halted_after = NEW.entry_halt_auto
                  AND (NOT NEW.entry_halt_auto OR reason_code = NEW.entry_halt_auto_reason)
            ) THEN
                RAISE EXCEPTION 'entry halt changed without an audit event';
            END IF;
            IF manual_changed AND NOT EXISTS (
                SELECT 1 FROM paper_control_events
                WHERE txid = txid_current()
                  AND control = 'ENTRY_HALT_MANUAL'
                  AND action IN ('SET', 'CLEAR')
                  AND halted_before = manual_before
                  AND halted_after = NEW.entry_halt_manual
                  AND (NOT NEW.entry_halt_manual OR reason_code = NEW.entry_halt_manual_reason)
            ) THEN
                RAISE EXCEPTION 'entry halt changed without an audit event';
            END IF;
            RETURN NULL;
        END $$"""
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER paper_accounts_entry_halt_audited "
        "AFTER INSERT OR UPDATE ON paper_accounts "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION require_audited_entry_halt()"
    )


def downgrade() -> None:
    """Undo 0023 only where nothing would be lost; never touch `paused`."""
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "Offline downgrade of 0023 can only be generated for PostgreSQL. " + REFUSAL
            )
        op.execute(
            sa.text(
                f"DO $$ BEGIN IF ({GUARD}) > 0 THEN RAISE EXCEPTION '{REFUSAL}'; END IF; END $$;"
            )
        )
    elif op.get_bind().execute(sa.text(GUARD)).scalar_one():
        raise RuntimeError(REFUSAL)
    op.execute("DROP TRIGGER paper_accounts_entry_halt_audited ON paper_accounts")
    op.execute("DROP FUNCTION require_audited_entry_halt()")
    op.execute("DROP TRIGGER paper_control_events_append_only ON paper_control_events")
    op.execute("DROP FUNCTION reject_paper_control_event_mutation()")
    op.drop_table("paper_control_events")
    for name in (
        "entry_halt_reason_format",
        "entry_halt_manual_consistent",
        "entry_halt_auto_consistent",
    ):
        op.drop_constraint(name, "paper_accounts", type_="check")
    for column in (
        "entry_halt_manual_at",
        "entry_halt_manual_reason",
        "entry_halt_auto_decision_id",
        "entry_halt_auto_loss_day",
        "entry_halt_auto_at",
        "entry_halt_auto_reason",
        "entry_halt_manual",
        "entry_halt_auto",
    ):
        op.drop_column("paper_accounts", column)
