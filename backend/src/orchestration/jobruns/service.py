"""Recording PAPER job runs, and reading the exit job's evidence back.

The recorder writes one `paper_job_runs` row per started run in a short
transaction of its own — never inside a trading transaction, and never under
the paper account lock — so recording a run can neither hold up a fill or a
sale nor take one down with it. A row that could not be finished stays
`RUNNING`, which the gate reads as a run that never finished: failing to record
fails closed.

The gate's read (`exit_liveness`) runs inside the caller's transaction, after
the caller has taken the paper account lock. It takes no lock of its own: the
exit job writes its rows without the account lock, so there is no second lock
order. What a BUY reads is the latest *committed* evidence at that instant; a
verdict about the exit job committed a moment later applies from the next BUY.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.tables import PaperJobRunRow, PositionRow, TradeCaseExecutionRow, TradeCaseRow
from src.orchestration.jobruns.models import (
    EVIDENCE_DEPTH,
    Coverage,
    EvidenceRow,
    ExitLivenessGate,
    JobName,
    JobOutcome,
    PolicyClass,
    bounded_codes,
    judge_exit_liveness,
)
from src.orchestration.strategy.early import is_early


def policy_class(strategy_policy_id: str | None) -> PolicyClass:
    """The exit contract a BUY under this strategy opens a position for."""
    return PolicyClass.EARLY if is_early(strategy_policy_id) else PolicyClass.NORMAL


def _aware(value: Any) -> Any:
    """A stored instant as an aware UTC datetime; SQLite hands them back naive."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def exit_evidence(session: AsyncSession) -> list[EvidenceRow]:
    """The latest exit-job runs, newest first, without starts that found the lock held."""
    rows = (
        await session.scalars(
            select(PaperJobRunRow)
            .where(
                PaperJobRunRow.job == JobName.EXIT.value,
                PaperJobRunRow.mode == "EXITS_ONLY",
                PaperJobRunRow.outcome != JobOutcome.ALREADY_RUNNING.value,
            )
            .order_by(PaperJobRunRow.started_at.desc(), PaperJobRunRow.run_id.desc())
            .limit(EVIDENCE_DEPTH)
        )
    ).all()
    return [
        EvidenceRow(
            outcome=row.outcome,
            started_at=_aware(row.started_at),
            finished_at=_aware(row.finished_at),
            exit_ready=row.exit_ready,
            exit_ready_reason=row.exit_ready_reason,
            policy_classes=row.policy_classes,
        )
        for row in rows
    ]


async def exit_liveness(
    session: AsyncSession,
    gate: ExitLivenessGate,
    *,
    at: datetime,
    policy: PolicyClass | None = None,
) -> str | None:
    """`None` when the exit job's evidence covers a BUY at `at`, else the reason."""
    return judge_exit_liveness(await exit_evidence(session), at=at, gate=gate, policy=policy)


async def coverage(session: AsyncSession) -> Coverage:
    """Every open position, by the exit contract that may close it.

    The same tests the two exit policies apply: a position is the normal
    policy's when a case-bound entry opened its cycle, the early policy's when
    that case was an early one, and nobody's without a case-bound entry.
    """
    positions = (await session.scalars(select(PositionRow).where(PositionRow.quantity > 0))).all()
    cycles = [item.cycle_id for item in positions if item.cycle_id is not None]
    strategies: dict[Any, str | None] = {}
    if cycles:
        rows = (
            await session.execute(
                select(TradeCaseExecutionRow.cycle_id, TradeCaseRow.strategy_policy_id)
                .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExecutionRow.trade_case_id)
                .where(TradeCaseExecutionRow.cycle_id.in_(cycles))
            )
        ).all()
        strategies = {cycle: strategy for cycle, strategy in rows}
    normal = early = unknown = 0
    for position in positions:
        if position.cycle_id is None or position.cycle_id not in strategies:
            unknown += 1
        elif is_early(strategies[position.cycle_id]):
            early += 1
        else:
            normal += 1
    return Coverage(normal=normal, early=early, unknown=unknown)


@dataclass(frozen=True)
class JobRunFinish:
    """What a finished run records. Codes, counts and instants only."""

    outcome: JobOutcome
    finished_at: datetime
    stop_code: str | None
    exit_code: int
    lock_status: str | None
    acquisition_status: str | None = None
    exit_phase_status: str | None = None
    exit_ready: bool = False
    exit_ready_reason: str | None = None
    policy_classes: tuple[str, ...] = ()
    result_codes: tuple[str, ...] = ()
    metrics: dict[str, int] | None = None


@dataclass(frozen=True)
class JobRunRecorder:
    """Writes `paper_job_runs`, each write its own short transaction."""

    sessions: async_sessionmaker[AsyncSession]

    async def start(self, run_id: UUID, job: JobName, mode: str, at: datetime) -> None:
        async with self.sessions.begin() as session:
            session.add(
                PaperJobRunRow(
                    run_id=run_id,
                    job=job.value,
                    mode=mode,
                    started_at=at,
                    outcome=JobOutcome.RUNNING.value,
                )
            )

    async def already_running(self, run_id: UUID, job: JobName, mode: str, at: datetime) -> None:
        """A start that found its job's lock held: recorded, and finished at once."""
        async with self.sessions.begin() as session:
            session.add(
                PaperJobRunRow(
                    run_id=run_id,
                    job=job.value,
                    mode=mode,
                    started_at=at,
                    finished_at=at,
                    outcome=JobOutcome.ALREADY_RUNNING.value,
                    stop_code="ALREADY_RUNNING",
                    exit_code=0,
                    lock_status="ALREADY_RUNNING",
                )
            )

    async def finish(self, run_id: UUID, finish: JobRunFinish) -> bool:
        """Finish a running row once. Returns whether exactly that row was finished."""
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(PaperJobRunRow)
                .where(
                    PaperJobRunRow.run_id == run_id,
                    PaperJobRunRow.outcome == JobOutcome.RUNNING.value,
                )
                .values(
                    outcome=finish.outcome.value,
                    finished_at=finish.finished_at,
                    stop_code=finish.stop_code,
                    exit_code=finish.exit_code,
                    lock_status=finish.lock_status,
                    acquisition_status=finish.acquisition_status,
                    exit_phase_status=finish.exit_phase_status,
                    exit_ready=finish.exit_ready,
                    exit_ready_reason=finish.exit_ready_reason,
                    policy_classes=list(finish.policy_classes),
                    result_codes=bounded_codes(finish.result_codes),
                    metrics=dict(finish.metrics or {}),
                )
                .execution_options(synchronize_session=False)
            )
            return getattr(result, "rowcount", 0) == 1


__all__ = [
    "JobRunFinish",
    "JobRunRecorder",
    "coverage",
    "exit_evidence",
    "exit_liveness",
    "policy_class",
]
