"""Read-only recorded market view. New unavailable data never falls back to older values."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.tables import MarketObservationRow as Row
from src.markets.models import MarketCandidate, MarketIdentity, MarketSnapshot


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

    async def identities(
        self,
        identifiers: Sequence[str],
        *,
        include_fixtures: bool = False,
        limit: int = 50,
    ) -> tuple[MarketIdentity, ...]:
        """Which markets were ever recorded under these identifiers.

        Deliberately **not** a market reading, and the difference is the whole
        point of keeping it here rather than widening `markets`. What comes back
        is coordinates — provider, chain, network, pair, both assets, venue and
        pool locator — and never a price, a liquidity figure or an availability
        claim. It carries no freshness and cannot be mistaken for current data,
        so asking it costs nothing the freshness contract protects.

        That is exactly why it ignores age: the one caller that needs it is
        about to ask a provider to observe these markets *again*, and a market
        whose last reading aged out is the one most in need of that. Answering
        only for fresh rows would make re-acquisition possible only where it was
        unnecessary.

        The newest row per stream decides, ranked before any identifier filter
        for the same reason `markets` ranks first: a later event that renamed
        nothing must still be what answers for its market. An `asset_id` may
        legitimately match several pairs — a token trades in more than one pool
        — and every one of them comes back, because choosing between them is the
        caller's decision to make explicitly rather than the database's to make
        by ordering.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Invalid identity limit")
        wanted = [item for item in dict.fromkeys(identifiers) if item]
        if not wanted:
            return ()
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
        statement = (
            select(Row)
            .join(newest, Row.id == newest.c.id)
            .where(
                newest.c.rank == 1,
                or_(Row.asset_id.in_(wanted), Row.pair_id.in_(wanted)),
            )
        )
        if not include_fixtures:
            statement = statement.where(Row.is_fixture.is_(False))
        statement = statement.order_by(
            Row.observed_at.desc(), Row.recorded_at.desc(), Row.id.desc()
        ).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(
            MarketSnapshot.model_validate(row.payload).pair.market_identity for row in rows
        )

    async def observations(
        self,
        identity: str,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        include_fixtures: bool = False,
    ) -> tuple[MarketSnapshot, ...]:
        """Every recorded observation for one market inside a bounded window.

        Distinct from :meth:`markets`, which answers "what is the current state
        of each market" and therefore keeps only the newest row per stream. That
        is the right answer for a dashboard and the wrong one for a monitor: a
        price that crossed a level and came back would be invisible, and the
        system would report that nothing happened when it had durably recorded
        that something did.

        Ordered oldest first by the market's **own** observation time, so a
        caller asking "when did we first see this?" gets a stable answer.
        ``recorded_at`` and ``id`` break ties only — a row inserted late never
        becomes recent, because insertion time cannot reorder the window.

        The window is closed at both ends and the result is capped. One extra row
        beyond ``limit`` is fetched so a caller can tell a full window from a
        truncated one rather than silently receiving part of the picture.
        """
        if limit < 1 or limit > 500:
            raise ValueError("Observation windows must stay bounded")
        if since.utcoffset() is None or until.utcoffset() is None:
            raise ValueError("Observation windows require timezone-aware bounds")
        statement = (
            select(Row)
            .where(
                or_(Row.asset_id == identity, Row.pair_id == identity),
                Row.available.is_(True),
                Row.observed_at >= since,
                Row.observed_at <= until,
            )
            .order_by(Row.observed_at, Row.recorded_at, Row.id)
            .limit(limit + 1)
        )
        if not include_fixtures:
            statement = statement.where(Row.is_fixture.is_(False))
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(MarketSnapshot.model_validate(row.payload) for row in rows)

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
