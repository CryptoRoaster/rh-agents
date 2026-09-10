"""Read-only recorded market view. New unavailable data never falls back to older values."""

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.tables import MarketObservationRow as Row
from src.markets.models import MarketCandidate, MarketSnapshot


class MarketReader:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
        max_age: timedelta = timedelta(seconds=60),
    ) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self._sessions = sessions
        self._clock = clock if clock is not None else SystemClock()
        self._max_age = max_age

    async def markets(
        self,
        *,
        identity: str | None = None,
        provider: str | None = None,
        chain: str | None = None,
        network: str | None = None,
        include_fixtures: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MarketSnapshot, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid pagination")
        now = self._clock.now()
        ranked = select(
            Row.id,
            func.row_number()
            .over(
                partition_by=(Row.provider, Row.pair_id, Row.is_fixture),
                order_by=(Row.observed_at.desc(), Row.recorded_at.desc(), Row.id.desc()),
            )
            .label("rank"),
        )
        newest = ranked.subquery()
        # Rank the complete streams before applying ANY read filter. An identity
        # change or unavailable latest event must never reveal an older event.
        statement = (
            select(Row)
            .join(newest, Row.id == newest.c.id)
            .where(
                newest.c.rank == 1,
                Row.available.is_(True),
                Row.observed_at <= now,
                Row.freshness_at >= now - self._max_age,
            )
        )
        if identity is not None:
            statement = statement.where(or_(Row.asset_id == identity, Row.pair_id == identity))
        for column, value in ((Row.provider, provider), (Row.chain, chain), (Row.network, network)):
            if value is not None:
                statement = statement.where(column == value)
        if not include_fixtures:
            statement = statement.where(Row.is_fixture.is_(False))
        statement = (
            statement.order_by(Row.observed_at.desc(), Row.recorded_at.desc(), Row.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
            snapshots = tuple(MarketSnapshot.model_validate(row.payload) for row in rows)
        # Also check the immutable payload, not just query metadata.
        checked_at = self._clock.now()
        return tuple(s for s in snapshots if s.is_valid_at(checked_at, self._max_age))

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        snapshots = await self.markets(
            identity=identity, include_fixtures=include_fixtures, limit=1
        )
        return snapshots[0] if snapshots else None

    async def candidates(
        self,
        *,
        include_fixtures: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MarketCandidate, ...]:
        return tuple(
            MarketCandidate.from_snapshot(snapshot)
            for snapshot in await self.markets(
                include_fixtures=include_fixtures, limit=limit, offset=offset
            )
        )
