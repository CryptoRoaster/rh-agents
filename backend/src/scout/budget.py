"""The daily paid-ORBIT budget as durable reservations.

A slot is reserved and committed *before* a scout model call. Counting and
reserving happen in one transaction under a transaction-scoped advisory lock,
so two paths can never both read "95 of 96 used" and both reserve. The scout's
own run lock already serialises scout runs; this lock makes the budget hold for
any caller of `reserve`, not only for runs that respected the other one.

Every status counts toward the UTC day: RESERVED (including one a crashed
process left behind), COMPLETED and FAILED. The failure mode is deliberately
conservative — a slot may be spent for a call that never reached the provider,
never the reverse.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.tables import ScoutOrbitReservationRow

BUDGET_LOCK = "rh-agents:scout-orbit-budget"


def utc_day(instant: datetime) -> date:
    return instant.astimezone(UTC).date()


@dataclass(frozen=True)
class OrbitBudget:
    sessions: async_sessionmaker[AsyncSession]

    async def used(self, day: date) -> int:
        """Slots taken on this UTC day, whatever became of them."""
        async with self.sessions() as session:
            return await self._count(session, day)

    @staticmethod
    async def _count(session: AsyncSession, day: date) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(ScoutOrbitReservationRow)
            .where(ScoutOrbitReservationRow.utc_day == day)
        )
        return int(count or 0)

    async def reserve(
        self, watch_id: UUID, checkpoint_index: int, now: datetime, cap: int
    ) -> UUID | None:
        """Take one slot for this watch checkpoint, or None when the day is spent.

        Committed when this returns: the caller may only call the model after.
        """
        day = utc_day(now)
        async with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": BUDGET_LOCK}
                )
            existing = await session.scalar(
                select(ScoutOrbitReservationRow).where(
                    ScoutOrbitReservationRow.watch_id == watch_id,
                    ScoutOrbitReservationRow.checkpoint_index == checkpoint_index,
                )
            )
            if existing is not None:
                # A slot is reserved before its checkpoint is claimed, and the
                # model is only called after the claim. A still-RESERVED slot
                # for a checkpoint that is still due was therefore left by a run
                # that stopped before claiming: no call was made for it, and it
                # is reused rather than counted twice. Anything else refuses.
                return existing.id if existing.status == "RESERVED" else None
            if await self._count(session, day) >= cap:
                return None
            reservation = uuid4()
            session.add(
                ScoutOrbitReservationRow(
                    id=reservation,
                    watch_id=watch_id,
                    checkpoint_index=checkpoint_index,
                    utc_day=day,
                    reserved_at=now,
                    status="RESERVED",
                    assessment_id=None,
                    completed_at=None,
                    failure_reason=None,
                )
            )
        return reservation

    async def settle(
        self,
        reservation: UUID,
        *,
        status: str,
        now: datetime,
        assessment_id: UUID | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Mark what became of a slot. The slot stays counted either way."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(ScoutOrbitReservationRow)
                .where(
                    ScoutOrbitReservationRow.id == reservation,
                    ScoutOrbitReservationRow.status == "RESERVED",
                )
                .values(
                    status=status,
                    assessment_id=assessment_id,
                    completed_at=now,
                    failure_reason=failure_reason,
                )
            )
