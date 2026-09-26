"""Durable scout run history: one terminal row per scout execution."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import ScoutRunRow
from src.scout.models import ScoutRun, ScoutSummary

# Every counter the summary carries and the table stores, by the same name.
COUNTERS = (
    "discovered",
    "valid_markets",
    "provider_identity_rejects",
    "other_provider_rejects",
    "watches_created",
    "watches_updated",
    "bootstrapped",
    "refreshed",
    "watches_due_orbit",
    "orbit_reviews_started",
    "orbit_reviews_completed",
    "interesting",
    "not_interesting",
    "insufficient_data",
    "watches_due_history",
    "history_checks",
    "vector_sufficient",
    "promotable_new",
    "dormant_new",
    "retired_new",
    "provider_failures",
    "model_failures",
    "provider_requests",
    "orbit_backlog_before",
    "orbit_backlog_after",
    "new_watches_without_orbit_assessment",
)


def run_status(summary: ScoutSummary) -> str:
    """COMPLETED, STOPPED (a durable stop) or FAILED (a technical fault)."""
    if summary.errors:
        return "FAILED"
    if summary.stop == "SYSTEM_STOPPED":
        return "STOPPED"
    return "COMPLETED"


def _run(row: ScoutRunRow) -> ScoutRun:
    return ScoutRun(
        id=row.id,
        started_at=aware(row.started_at),
        completed_at=aware(row.completed_at),
        status=row.status,  # type: ignore[arg-type]
        stop=row.stop,
        errors=tuple(row.errors),
        policy_version=row.policy_version,
        oldest_orbit_due_age_seconds=row.oldest_orbit_due_age_seconds,
        **{name: getattr(row, name) for name in COUNTERS},
    )


@dataclass(frozen=True)
class ScoutRunRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self, run_id: UUID, started: datetime, completed: datetime, summary: ScoutSummary
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                ScoutRunRow(
                    id=run_id,
                    started_at=started,
                    completed_at=completed,
                    status=run_status(summary),
                    stop=summary.stop,
                    errors=list(summary.errors),
                    policy_version=summary.policy_version,
                    oldest_orbit_due_age_seconds=summary.oldest_orbit_due_age_seconds,
                    created_at=completed,
                    **{name: getattr(summary, name) for name in COUNTERS},
                )
            )

    async def recent(self, limit: int, offset: int = 0) -> tuple[ScoutRun, ...]:
        """Newest first."""
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ScoutRunRow)
                    .order_by(ScoutRunRow.started_at.desc(), ScoutRunRow.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return tuple(_run(row) for row in rows)

    async def count(self) -> int:
        async with self.sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(ScoutRunRow)) or 0)
