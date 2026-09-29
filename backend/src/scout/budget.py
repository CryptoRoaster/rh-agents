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

**Pacing.** With `SlotPacing` the day's slots are released evenly: slot *k* of
*n* becomes available at the start of the *k*-th of *n* equal UTC buckets
(fifteen minutes for ninety-six). A reservation needs a released slot that is
not yet taken, so no future slot is ever pulled forward and the budget cannot
be spent in the morning. Unused slots carry over, but at most
`per_bucket` reservations fall into one bucket, so missed scheduler runs never
turn into a burst.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.tables import ScoutOrbitReservationRow

BUDGET_LOCK = "rh-agents:scout-orbit-budget"


def utc_day(instant: datetime) -> date:
    return instant.astimezone(UTC).date()


DAY = timedelta(days=1)


@dataclass(frozen=True)
class SlotPacing:
    """The day's slots released evenly, with a bounded catch-up per bucket."""

    per_bucket: int = 2

    def __post_init__(self) -> None:
        if self.per_bucket < 1:
            raise ValueError("A bucket must admit at least one reservation")

    @staticmethod
    def bucket(now: datetime, cap: int) -> tuple[int, datetime, datetime]:
        """Index, start and end of the bucket `now` falls into (UTC)."""
        instant = now.astimezone(UTC)
        midnight = datetime.combine(instant.date(), time(0), tzinfo=UTC)
        width = DAY / cap
        index = min(cap - 1, int((instant - midnight) / width))
        start = midnight + width * index
        return index, start, start + width

    def released(self, now: datetime, cap: int) -> int:
        """How many of the day's slots exist by `now`: never a future one."""
        if cap <= 0:
            return 0
        index, _, _ = self.bucket(now, cap)
        return index + 1


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

    async def available(self, now: datetime, cap: int, pacing: SlotPacing | None) -> int:
        """Slots a run could take right now, read without reserving anything."""
        async with self.sessions() as session:
            return await self._available(session, now, cap, pacing)

    async def _available(
        self, session: AsyncSession, now: datetime, cap: int, pacing: SlotPacing | None
    ) -> int:
        used = await self._count(session, utc_day(now))
        if pacing is None:
            return max(0, cap - used)
        if cap <= 0:
            return 0
        _, start, end = pacing.bucket(now, cap)
        in_bucket = await session.scalar(
            select(func.count())
            .select_from(ScoutOrbitReservationRow)
            .where(
                ScoutOrbitReservationRow.reserved_at >= start,
                ScoutOrbitReservationRow.reserved_at < end,
            )
        )
        return max(
            0,
            min(
                min(cap, pacing.released(now, cap)) - used,
                pacing.per_bucket - int(in_bucket or 0),
            ),
        )

    async def reserve(
        self,
        watch_id: UUID,
        checkpoint_index: int,
        now: datetime,
        cap: int,
        pacing: SlotPacing | None = None,
    ) -> UUID | None:
        """Take one slot for this watch checkpoint, or None when none is available.

        Committed when this returns: the caller may only call the model after.
        With `pacing`, only a slot already released by `now` and within the
        bucket's catch-up bound can be taken.
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
            if await self._available(session, now, cap, pacing) <= 0:
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
