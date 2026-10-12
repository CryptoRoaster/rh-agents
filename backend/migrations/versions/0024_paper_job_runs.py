"""PAPER job runs, and the audited lifting of a durable pause.

**`paper_job_runs`.** One row per started run of a PAPER job (`--once`,
`--entries-once`, `--exits-once`): written `RUNNING` when the run holds its job
lock, and finished exactly once with what it ended as. A row still `RUNNING`
long after the job's own runtime bound is a run that never finished — a crash.
A second start that found the lock held is recorded as `ALREADY_RUNNING`, and
finished at once.

For the exit job the row also carries whether it is *evidence that exits work*
(`exit_ready`), and if not, why (`exit_ready_reason`): the entry side's liveness
gate (`EXIT_JOB_STALE`) reads it before a BUY. Only a successful exit-job run
can be ready — a check here, not just in code. Codes, counts and instants only;
no provider payload, address or credential is stored.

A finished row is final: an update is refused once `finished_at` is set, and the
identity of a run (`run_id`, `job`, `mode`, `started_at`) never changes.

**The durable pause, cleared only with its event.** `paper_control_events` may
now also record `SYSTEM_PAUSE` — an operator's request to lift `paused` — and a
deferred constraint trigger refuses any commit that turns `paused` from true to
false without a `SYSTEM_PAUSE` `CLEAR` event of the same transaction. Setting the
pause is unchanged and needs no event: SENTINEL's verdict is its record.

**Structure only.** No row is written and `paused` is neither read nor changed.

**Downgrade refuses** while any `SYSTEM_PAUSE` event exists: the previous
schema cannot hold it, and dropping it would erase who lifted a pause.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

CODE = "^[A-Z][A-Z0-9_]{0,63}$"
OUTCOMES = (
    "'RUNNING', 'SUCCEEDED', 'REFUSED', 'TECHNICAL_FAILURE', 'TIMED_OUT',"
    " 'ALREADY_RUNNING', 'EXIT_COVERAGE_MISSING'"
)
REFUSAL = (
    "Downgrade of 0024 refused: a SYSTEM_PAUSE audit event exists. "
    "The previous schema cannot hold it, and dropping it would erase who lifted a pause."
)
GUARD = "SELECT COUNT(*) FROM paper_control_events WHERE control = 'SYSTEM_PAUSE'"


def upgrade() -> None:
    op.create_table(
        "paper_job_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("job", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("stop_code", sa.String(64)),
        sa.Column("exit_code", sa.SmallInteger()),
        sa.Column("lock_status", sa.String(16)),
        sa.Column("acquisition_status", sa.String(64)),
        sa.Column("exit_phase_status", sa.String(64)),
        sa.Column("exit_ready", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("exit_ready_reason", sa.String(64)),
        sa.Column(
            "policy_classes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "result_codes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "metrics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint(
            "job IN ('PAPER_EXIT_JOB', 'PAPER_ENTRY_JOB')", name="paper_job_run_job"
        ),
        sa.CheckConstraint(
            "mode IN ('FULL', 'EXITS_ONLY', 'ENTRIES_ONLY')", name="paper_job_run_mode"
        ),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name="paper_job_run_outcome"),
        # Running exactly while unfinished; a finished run never goes back.
        sa.CheckConstraint(
            "(outcome = 'RUNNING') = (finished_at IS NULL)", name="paper_job_run_finished"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="paper_job_run_order"
        ),
        # Only a successful exit-job run is evidence that exits work, and then
        # without a reason against it; anything else names why it is not.
        sa.CheckConstraint(
            "(exit_ready AND job = 'PAPER_EXIT_JOB' AND mode = 'EXITS_ONLY'"
            " AND outcome = 'SUCCEEDED' AND exit_ready_reason IS NULL)"
            " OR NOT exit_ready",
            name="paper_job_run_exit_ready",
        ),
        sa.CheckConstraint(
            f"(stop_code IS NULL OR stop_code ~ '{CODE}')"
            f" AND (exit_ready_reason IS NULL OR exit_ready_reason ~ '{CODE}')"
            f" AND (acquisition_status IS NULL OR acquisition_status ~ '{CODE}')"
            f" AND (exit_phase_status IS NULL OR exit_phase_status ~ '{CODE}')",
            name="paper_job_run_codes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_classes) = 'array' AND jsonb_typeof(result_codes) = 'array'"
            " AND jsonb_typeof(metrics) = 'object'",
            name="paper_job_run_documents",
        ),
    )
    op.create_index("ix_paper_job_runs_job_time", "paper_job_runs", ["job", "started_at"])
    op.execute(
        """CREATE FUNCTION protect_finished_paper_job_run()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.finished_at IS NOT NULL THEN
                RAISE EXCEPTION 'a finished paper job run is final';
            END IF;
            IF (OLD.run_id, OLD.job, OLD.mode, OLD.started_at)
                IS DISTINCT FROM (NEW.run_id, NEW.job, NEW.mode, NEW.started_at) THEN
                RAISE EXCEPTION 'a paper job run keeps its identity';
            END IF;
            RETURN NEW;
        END $$"""
    )
    op.execute(
        "CREATE TRIGGER paper_job_runs_final "
        "BEFORE UPDATE ON paper_job_runs "
        "FOR EACH ROW EXECUTE FUNCTION protect_finished_paper_job_run()"
    )

    op.drop_constraint("paper_control_event_control", "paper_control_events", type_="check")
    op.create_check_constraint(
        "paper_control_event_control",
        "paper_control_events",
        "control IN ('ENTRY_HALT_AUTO', 'ENTRY_HALT_MANUAL', 'SYSTEM_PAUSE')",
    )
    # Lifting the pause is an operator's alone; nothing else ever writes it.
    op.create_check_constraint(
        "paper_control_event_pause_actor",
        "paper_control_events",
        "control <> 'SYSTEM_PAUSE' OR (actor_kind = 'OPERATOR' AND action IN ('CLEAR', 'REFUSED'))",
    )
    op.execute(
        """CREATE FUNCTION require_audited_pause_clear()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.paused IS DISTINCT FROM true OR NEW.paused IS NOT DISTINCT FROM true THEN
                RETURN NULL;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM paper_control_events
                WHERE txid = txid_current()
                  AND control = 'SYSTEM_PAUSE'
                  AND action = 'CLEAR'
                  AND halted_before
                  AND NOT halted_after
            ) THEN
                RAISE EXCEPTION 'system pause cleared without an audit event';
            END IF;
            RETURN NULL;
        END $$"""
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER paper_accounts_pause_clear_audited "
        "AFTER UPDATE ON paper_accounts "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION require_audited_pause_clear()"
    )


def downgrade() -> None:
    """Undo 0024 only where nothing would be lost; never touch `paused`."""
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "Offline downgrade of 0024 can only be generated for PostgreSQL. " + REFUSAL
            )
        op.execute(
            sa.text(
                f"DO $$ BEGIN IF ({GUARD}) > 0 THEN RAISE EXCEPTION '{REFUSAL}'; END IF; END $$;"
            )
        )
    elif op.get_bind().execute(sa.text(GUARD)).scalar_one():
        raise RuntimeError(REFUSAL)
    op.execute("DROP TRIGGER paper_accounts_pause_clear_audited ON paper_accounts")
    op.execute("DROP FUNCTION require_audited_pause_clear()")
    op.drop_constraint("paper_control_event_pause_actor", "paper_control_events", type_="check")
    op.drop_constraint("paper_control_event_control", "paper_control_events", type_="check")
    op.create_check_constraint(
        "paper_control_event_control",
        "paper_control_events",
        "control IN ('ENTRY_HALT_AUTO', 'ENTRY_HALT_MANUAL')",
    )
    op.execute("DROP TRIGGER paper_job_runs_final ON paper_job_runs")
    op.execute("DROP FUNCTION protect_finished_paper_job_run()")
    op.drop_index("ix_paper_job_runs_job_time", table_name="paper_job_runs")
    op.drop_table("paper_job_runs")
