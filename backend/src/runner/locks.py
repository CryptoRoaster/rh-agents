"""One run of each PAPER job at a time, across processes and hosts.

Two jobs, two locks: the fast exit job (`--exits-once`) and the entry job
(`--once`). Each is a PostgreSQL session-level advisory lock taken without
waiting, on a connection held for the whole run. A second start of the same job
finds it held and ends as `ALREADY_RUNNING` without calling anybody or writing
anything. A process that dies takes its connection with it, and PostgreSQL
releases the lock — there is no lease row to expire and nothing to clean up.

The two jobs do not exclude each other. Everything they could both write is
already serialised where it is written: every fill and every exit takes the
paper account row `FOR UPDATE` first, an order is keyed by its request, and the
database allows one exit per cycle. A shared lock on top would add a second
lock order without protecting anything that is not already protected, and would
let a slow entry run hold up a stop.

The metadata-only SQLite test engine has no advisory locks; there the lock is
reported as `NOT_SUPPORTED` and the run proceeds — a configuration that cannot
exist in a deployment, whose settings require PostgreSQL.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class RunJob(StrEnum):
    PAPER_EXIT_JOB = "rh-agents:paper-exit-job"
    PAPER_ENTRY_JOB = "rh-agents:paper-entry-job"


class LockOutcome(StrEnum):
    ACQUIRED = "ACQUIRED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    NOT_SUPPORTED = "NOT_SUPPORTED"


def _engine(sessions: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    bind = sessions.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise TypeError("A run's sessions must be bound to an engine")
    return bind


@asynccontextmanager
async def job_lock(
    sessions: async_sessionmaker[AsyncSession], job: RunJob
) -> AsyncIterator[LockOutcome]:
    """Hold this job's lock for the duration of the block, or say who has it."""
    engine = _engine(sessions)
    if engine.dialect.name != "postgresql":
        yield LockOutcome.NOT_SUPPORTED
        return
    async with engine.connect() as connection:
        held = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": job.value}
            )
        )
        # Committed so the connection is not left idle in a transaction for the
        # whole run; a session-level lock outlives the transaction it was taken in.
        await connection.commit()
        try:
            yield LockOutcome.ACQUIRED if held else LockOutcome.ALREADY_RUNNING
        finally:
            if held:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": job.value}
                    )
                    await connection.commit()
                except BaseException:
                    # A connection that cannot say whether it unlocked is closed
                    # rather than returned to the pool: closing releases the lock.
                    await connection.invalidate()
                    raise


__all__ = ["LockOutcome", "RunJob", "job_lock"]
