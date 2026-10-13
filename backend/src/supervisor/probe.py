"""Read-only gathering for the supervisor's judgements: launchd and the database.

Nothing here writes. The database is read in one transaction that PostgreSQL
is told is read-only, through the same queries the exit gate itself uses
(`exit_evidence`, `coverage`), so the health verdict and a BUY's verdict rest
on the same rows.
"""

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import AccountRow, PaperJobRunRow
from src.orchestration.jobruns.models import RUNNING_GRACE_SECONDS, JobName, JobOutcome
from src.orchestration.jobruns.service import coverage, exit_evidence
from src.supervisor.models import LABEL, DatabaseFacts, ServiceStatus

LAUNCHCTL_ENV = "RH_AGENTS_LAUNCHCTL"
# `launchctl print` for a label launchd does not know.
LAUNCHCTL_NOT_FOUND = 113


def launchctl_executable() -> str | None:
    configured = os.environ.get(LAUNCHCTL_ENV, "").strip()
    return configured or shutil.which("launchctl")


def service_status(*, launchctl: str | None = None, uid: int | None = None) -> ServiceStatus:
    """Whether launchd has the supervisor job loaded, running, and how it last exited."""
    executable = launchctl if launchctl is not None else launchctl_executable()
    if not executable:
        return ServiceStatus(loaded=None)
    target = f"gui/{os.getuid() if uid is None else uid}/{LABEL}"
    try:
        completed = subprocess.run(
            [executable, "print", target],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ServiceStatus(loaded=None)
    if completed.returncode == LAUNCHCTL_NOT_FOUND or "Could not find service" in (
        completed.stdout + completed.stderr
    ):
        return ServiceStatus(loaded=False)
    if completed.returncode != 0:
        return ServiceStatus(loaded=None)
    return parse_service(completed.stdout)


def parse_service(printed: str) -> ServiceStatus:
    state = re.search(r"^\s*state = (.+)$", printed, re.MULTILINE)
    status = re.search(r"^\s*last exit code = (-?\d+)", printed, re.MULTILINE)
    return ServiceStatus(
        loaded=True,
        running=state is not None and state.group(1).strip() == "running",
        last_exit_status=int(status.group(1)) if status else None,
    )


def _row(row: PaperJobRunRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "outcome": row.outcome,
        "stop_code": row.stop_code,
        "exit_code": row.exit_code,
        "exit_ready": row.exit_ready,
        "exit_ready_reason": row.exit_ready_reason,
    }


async def _active(session: AsyncSession, job: JobName, since: datetime) -> bool:
    found = await session.scalar(
        select(func.count())
        .select_from(PaperJobRunRow)
        .where(
            PaperJobRunRow.job == job.value,
            PaperJobRunRow.outcome == JobOutcome.RUNNING.value,
            PaperJobRunRow.started_at >= since,
        )
    )
    return bool(found)


async def database_facts(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    exit_run_max_seconds: int,
    entry_run_max_seconds: int,
) -> DatabaseFacts:
    """Everything the judgements read from the database, in one read-only transaction."""
    async with sessions() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(text("SET TRANSACTION READ ONLY"))
        evidence = await exit_evidence(session)
        book = await coverage(session)
        latest = await session.scalar(
            select(PaperJobRunRow)
            .where(PaperJobRunRow.job == JobName.EXIT.value)
            .order_by(PaperJobRunRow.started_at.desc(), PaperJobRunRow.run_id.desc())
            .limit(1)
        )
        ready_at = await session.scalar(
            select(func.max(PaperJobRunRow.finished_at)).where(
                PaperJobRunRow.job == JobName.EXIT.value,
                PaperJobRunRow.exit_ready.is_(True),
            )
        )
        account = await session.get(AccountRow, 1)
        grace = timedelta(seconds=RUNNING_GRACE_SECONDS)
        exit_active = await _active(
            session, JobName.EXIT, now - timedelta(seconds=exit_run_max_seconds) - grace
        )
        entry_active = await _active(
            session, JobName.ENTRY, now - timedelta(seconds=entry_run_max_seconds) - grace
        )
    return DatabaseFacts(
        evidence=evidence,
        coverage=book,
        last_exit_run=_row(latest),
        last_ready_finished_at=None if ready_at is None else aware(ready_at),
        # No account row is no evidence of a block: read as not blocked.
        entry_halt_manual=bool(account and account.entry_halt_manual),
        entry_halt_auto=bool(account and account.entry_halt_auto),
        paused=bool(account and account.paused),
        exit_run_active=exit_active,
        entry_run_active=entry_active,
    )


__all__ = ["database_facts", "launchctl_executable", "parse_service", "service_status"]
