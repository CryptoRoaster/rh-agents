"""Read-only gathering for the supervisor's judgements: the service manager and the database.

The service manager is launchd on macOS and systemd on Linux
(`RH_AGENTS_SUPERVISOR_BACKEND` overrides the platform's default). Either way
the answer is one `ServiceStatus`; one that cannot be established is
`loaded=None`, never "not loaded".

Nothing here writes. The database is read in one transaction that PostgreSQL
is told is read-only, through the same queries the exit gate itself uses
(`exit_evidence`, `coverage`), so the health verdict and a BUY's verdict rest
on the same rows.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import AccountRow, PaperJobRunRow
from src.orchestration.jobruns.models import RUNNING_GRACE_SECONDS, JobName, JobOutcome
from src.orchestration.jobruns.service import coverage, exit_evidence
from src.supervisor.models import (
    LABEL,
    SYSTEMD_SERVICE,
    SYSTEMD_TIMER,
    DatabaseFacts,
    ServiceStatus,
)

BACKEND_ENV = "RH_AGENTS_SUPERVISOR_BACKEND"
LAUNCHD = "launchd"
SYSTEMD = "systemd"
LAUNCHCTL_ENV = "RH_AGENTS_LAUNCHCTL"
SYSTEMCTL_ENV = "RH_AGENTS_SYSTEMCTL"
# `launchctl print` for a label launchd does not know.
LAUNCHCTL_NOT_FOUND = 113


def launchctl_executable() -> str | None:
    configured = os.environ.get(LAUNCHCTL_ENV, "").strip()
    return configured or shutil.which("launchctl")


def service_backend() -> str | None:
    """`launchd` or `systemd`: configured, else the platform's own. `None`: neither."""
    configured = os.environ.get(BACKEND_ENV, "").strip().lower()
    if configured:
        return configured if configured in (LAUNCHD, SYSTEMD) else None
    if sys.platform == "darwin":
        return LAUNCHD
    if sys.platform.startswith("linux"):
        return SYSTEMD
    return None


def service_status(*, launchctl: str | None = None, uid: int | None = None) -> ServiceStatus:
    """The supervisor service as its platform's service manager reports it."""
    backend = LAUNCHD if launchctl is not None else service_backend()
    if backend == LAUNCHD:
        return launchd_service_status(launchctl=launchctl, uid=uid)
    if backend == SYSTEMD:
        return systemd_service_status()
    return ServiceStatus(loaded=None)


def launchd_service_status(
    *, launchctl: str | None = None, uid: int | None = None
) -> ServiceStatus:
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


def systemctl_executable() -> str | None:
    configured = os.environ.get(SYSTEMCTL_ENV, "").strip()
    return configured or shutil.which("systemctl")


TIMER_PROPERTIES = "LoadState,ActiveState,UnitFileState"
SERVICE_PROPERTIES = "LoadState,ActiveState,SubState,Result,ExecMainStatus"
# A oneshot tick is `activating` while it runs.
RUNNING_STATES = frozenset({"activating", "active", "deactivating", "reloading"})


def _show(executable: str, unit: str, properties: str) -> dict[str, str] | None:
    try:
        completed = subprocess.run(
            [executable, "show", unit, f"--property={properties}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return parse_properties(completed.stdout)


def parse_properties(printed: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in printed.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            found[key.strip()] = value.strip()
    return found


def systemd_service_status(*, systemctl: str | None = None) -> ServiceStatus:
    """The supervisor as systemd runs it: a timer that starts a oneshot service.

    Supervising means the timer is armed (`active`). Not installed or stopped
    (`inactive`) is not loaded. Anything else — a failed or half-started timer,
    a systemctl that cannot answer — is unknown, never "fine".
    """
    executable = systemctl if systemctl is not None else systemctl_executable()
    if not executable:
        return ServiceStatus(loaded=None)
    timer = _show(executable, SYSTEMD_TIMER, TIMER_PROPERTIES)
    service = _show(executable, SYSTEMD_SERVICE, SERVICE_PROPERTIES)
    if timer is None or service is None:
        return ServiceStatus(loaded=None)
    return parse_systemd(timer, service)


def parse_systemd(timer: dict[str, str], service: dict[str, str]) -> ServiceStatus:
    load_state = timer.get("LoadState")
    active_state = timer.get("ActiveState")
    if load_state == "not-found":
        return ServiceStatus(loaded=False)
    if load_state != "loaded":
        return ServiceStatus(loaded=None)
    running = service.get("ActiveState") in RUNNING_STATES
    raw_status = service.get("ExecMainStatus", "")
    last_exit_status = int(raw_status) if raw_status.lstrip("-").isdigit() else None
    if active_state == "active":
        return ServiceStatus(loaded=True, running=running, last_exit_status=last_exit_status)
    if active_state == "inactive":
        return ServiceStatus(loaded=False, running=running, last_exit_status=last_exit_status)
    return ServiceStatus(loaded=None, running=running, last_exit_status=last_exit_status)


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


__all__ = [
    "database_facts",
    "launchctl_executable",
    "launchd_service_status",
    "parse_properties",
    "parse_service",
    "parse_systemd",
    "service_backend",
    "service_status",
    "systemctl_executable",
    "systemd_service_status",
]
