from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from src.data.tables import PaperJobRunRow
from src.orchestration.jobruns.models import ExitLivenessGate
from tests.riskrequest.conftest import risk_db  # noqa: F401

# The production defaults: evidence may be three minutes old; an exit run is
# bounded at one minute.
GATE = ExitLivenessGate(max_age_seconds=180, run_max_seconds=60)


async def exit_run(
    sessions,
    finished: datetime | None,
    *,
    ready: bool = True,
    policies=("EARLY", "NORMAL"),
    outcome: str | None = None,
    reason: str | None = None,
    started: datetime | None = None,
    mode: str = "EXITS_ONLY",
    job: str = "PAPER_EXIT_JOB",
):
    """One exit-job run as the recorder would leave it. `finished=None`: still running."""
    if started is None:
        assert finished is not None, "a running row needs its start"
    begun = started if started is not None else finished - timedelta(seconds=20)
    if finished is None:
        outcome = "RUNNING"
    elif outcome is None:
        outcome = "SUCCEEDED"
    async with sessions.begin() as session:
        session.add(
            PaperJobRunRow(
                run_id=uuid4(),
                job=job,
                mode=mode,
                started_at=begun,
                finished_at=finished,
                outcome=outcome,
                exit_code=None if finished is None else 0,
                exit_ready=ready and outcome == "SUCCEEDED",
                exit_ready_reason=None
                if ready and outcome == "SUCCEEDED"
                else (reason or "EXIT_JOB_TECHNICAL_FAILURE"),
                policy_classes=list(policies),
            )
        )


async def job_runs(sessions):
    async with sessions() as session:
        return (
            await session.scalars(select(PaperJobRunRow).order_by(PaperJobRunRow.started_at))
        ).all()
