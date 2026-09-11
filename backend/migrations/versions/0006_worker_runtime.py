"""Durable capability-restricted worker runtime: instances, leases and attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TASK_LEASE_COLUMNS = (
    "lease_id",
    "worker_instance_id",
    "lease_started_at",
    "lease_expires_at",
    "last_heartbeat_at",
    "lease_renewals",
    "next_eligible_at",
    "max_attempts",
    "failure_category",
)


def upgrade() -> None:
    op.create_table(
        "worker_instances",
        sa.Column("worker_instance_id", sa.Uuid(), primary_key=True),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("runtime_version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registration_key", sa.String(200), nullable=False, unique=True),
        sa.Column("registration_fingerprint", sa.String(64), nullable=False),
    )
    op.create_index("ix_worker_instances_role_status", "worker_instances", ["role", "status"])

    # Current lease and retry scheduling live on the task aggregate, so "at most one
    # active lease per task" holds by construction rather than by constraint.
    op.add_column("trade_case_tasks", sa.Column("lease_id", sa.Uuid()))
    op.add_column("trade_case_tasks", sa.Column("worker_instance_id", sa.Uuid()))
    op.add_column("trade_case_tasks", sa.Column("lease_started_at", sa.DateTime(timezone=True)))
    op.add_column("trade_case_tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("trade_case_tasks", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column(
        "trade_case_tasks",
        sa.Column("lease_renewals", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("trade_case_tasks", sa.Column("next_eligible_at", sa.DateTime(timezone=True)))
    op.add_column(
        "trade_case_tasks",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
    )
    op.add_column("trade_case_tasks", sa.Column("failure_category", sa.String(40)))
    op.create_foreign_key(
        "fk_trade_case_tasks_worker_instance",
        "trade_case_tasks",
        "worker_instances",
        ["worker_instance_id"],
        ["worker_instance_id"],
    )
    op.create_unique_constraint(
        "uq_trade_case_task_slot", "trade_case_tasks", ["trade_case_id", "role", "task_type"]
    )
    op.create_check_constraint(
        "trade_case_task_renewals_nonnegative", "trade_case_tasks", "lease_renewals >= 0"
    )
    op.create_check_constraint(
        "trade_case_task_attempts_positive", "trade_case_tasks", "max_attempts >= 1"
    )
    op.create_check_constraint(
        "trade_case_task_lease_window",
        "trade_case_tasks",
        "lease_expires_at IS NULL OR lease_started_at IS NULL"
        " OR lease_expires_at > lease_started_at",
    )
    op.create_check_constraint(
        "trade_case_task_lease_pairing",
        "trade_case_tasks",
        "(lease_id IS NULL) = (worker_instance_id IS NULL)",
    )
    op.create_index(
        "ix_trade_case_tasks_claim",
        "trade_case_tasks",
        ["role", "status", "next_eligible_at", "created_at"],
    )
    op.create_index(
        "ix_trade_case_tasks_lease_expiry", "trade_case_tasks", ["status", "lease_expires_at"]
    )

    op.create_table(
        "worker_task_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trade_case_id",
            sa.Uuid(),
            sa.ForeignKey("trade_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("trade_case_tasks.task_id"), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column(
            "worker_instance_id",
            sa.Uuid(),
            sa.ForeignKey("worker_instances.worker_instance_id"),
            nullable=False,
        ),
        sa.Column("lease_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(40)),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("failure_category", sa.String(40)),
        sa.Column("runtime_version", sa.String(40), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_worker_attempt_number"),
        sa.CheckConstraint("attempt_number >= 1", name="worker_attempt_number_positive"),
        sa.CheckConstraint("lease_expires_at > started_at", name="worker_attempt_lease_window"),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)", name="worker_attempt_outcome_pairing"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="worker_attempt_ordering"
        ),
    )
    op.create_index(
        "ix_worker_task_attempts_correlation_id", "worker_task_attempts", ["correlation_id"]
    )
    op.create_index(
        "ix_worker_attempts_task", "worker_task_attempts", ["task_id", "attempt_number"]
    )
    op.create_index(
        "ix_worker_attempts_instance", "worker_task_attempts", ["worker_instance_id", "started_at"]
    )

    # An attempt is written once on claim and once when it finishes. Heartbeats
    # never touch this table, so "immutable once finished" is exact rather than a
    # convenient label.
    op.execute(
        """CREATE FUNCTION reject_finished_worker_attempt_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.finished_at IS NOT NULL THEN
            RAISE EXCEPTION 'Finished worker task attempts are immutable';
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        """CREATE FUNCTION reject_worker_attempt_removal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Worker attempt history is append-only'; END $$"""
    )
    op.execute(
        "CREATE TRIGGER worker_task_attempts_immutable_once_finished BEFORE UPDATE "
        "ON worker_task_attempts FOR EACH ROW "
        "EXECUTE FUNCTION reject_finished_worker_attempt_mutation()"
    )
    op.execute(
        "CREATE TRIGGER worker_task_attempts_append_only BEFORE DELETE OR TRUNCATE "
        "ON worker_task_attempts FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_worker_attempt_removal()"
    )


def downgrade() -> None:
    op.drop_table("worker_task_attempts")
    op.execute("DROP FUNCTION reject_worker_attempt_removal()")
    op.execute("DROP FUNCTION reject_finished_worker_attempt_mutation()")

    op.drop_index("ix_trade_case_tasks_lease_expiry", table_name="trade_case_tasks")
    op.drop_index("ix_trade_case_tasks_claim", table_name="trade_case_tasks")
    op.drop_constraint("trade_case_task_lease_pairing", "trade_case_tasks", type_="check")
    op.drop_constraint("trade_case_task_lease_window", "trade_case_tasks", type_="check")
    op.drop_constraint("trade_case_task_attempts_positive", "trade_case_tasks", type_="check")
    op.drop_constraint("trade_case_task_renewals_nonnegative", "trade_case_tasks", type_="check")
    op.drop_constraint("uq_trade_case_task_slot", "trade_case_tasks", type_="unique")
    op.drop_constraint(
        "fk_trade_case_tasks_worker_instance", "trade_case_tasks", type_="foreignkey"
    )
    for column in TASK_LEASE_COLUMNS:
        op.drop_column("trade_case_tasks", column)

    op.drop_index("ix_worker_instances_role_status", table_name="worker_instances")
    op.drop_table("worker_instances")
