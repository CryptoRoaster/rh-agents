"""Durable append-only ingestion with database-enforced event identity."""

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.tables import MarketObservationRow
from src.markets.models import MarketPair, MarketSnapshot
from src.markets.providers import MarketProvider, normalize_snapshot
from src.markets.reader import MarketReader


class ObservationConflict(ValueError):
    """An event UUID was reused with different content or provenance."""


@dataclass(frozen=True)
class RecordedObservation:
    """One durable write, and whether *this* caller is the one that made it.

    The second field is the difference between "this pass observed the market"
    and "this event was already stored". Both leave the same row behind, and a
    caller that reported them as one would credit itself with a write somebody
    else — an earlier attempt, a concurrent run — had already committed.
    """

    observation: MarketSnapshot
    inserted: bool


class MarketRecorder:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Clock | None = None
    ) -> None:
        self._sessions = sessions
        self._clock = clock if clock is not None else SystemClock()

    async def record(self, observation: MarketSnapshot) -> MarketSnapshot:
        """One durable observation. The write, without the write's provenance."""
        return (await self.record_reporting(observation)).observation

    async def record_reporting(self, observation: MarketSnapshot) -> RecordedObservation:
        """The same single write, saying whether this call is what performed it.

        One implementation, two surfaces: everything that only needs the stored
        observation keeps calling `record`, and a caller that has to account for
        what it did — rather than for what it found — reads `inserted` here.
        """
        # Revalidate nested Python models too; model_copy can bypass validation.
        observation = MarketSnapshot.model_validate(observation.model_dump())
        payload = observation.model_dump(mode="json")
        if observation.pair.pool_locator is None:
            # Preserve the exact version-1 serialization for legacy replay.
            payload["pair"].pop("pool_locator", None)
        async with self._sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert: PGInsert | SQLiteInsert
            if dialect == "postgresql":
                insert = pg_insert(MarketObservationRow)
            elif dialect == "sqlite":  # Directly constructed lightweight test engines only.
                insert = sqlite_insert(MarketObservationRow)
            else:
                raise ValueError("Unsupported recorder database")
            statement = insert.values(
                id=observation.id,
                schema_version=observation.schema_version,
                provider=observation.provider,
                chain=observation.chain,
                network=observation.network,
                asset_id=observation.asset_id,
                pair_id=observation.pair.pair_id,
                correlation_id=observation.correlation_id,
                observed_at=observation.observed_at,
                recorded_at=self._clock.now(),
                freshness_at=observation.freshness_at,
                available=observation.available,
                is_fixture=observation.is_fixture,
                payload=payload,
            ).on_conflict_do_nothing(index_elements=["id"])
            # `RETURNING` yields nothing when the conflict clause skipped the
            # insert, which is the only reliable way to tell an event this call
            # wrote from one it merely found. A second `SELECT` could not: the
            # row is there either way.
            written = await session.scalar(statement.returning(MarketObservationRow.id))
            row = await session.get(MarketObservationRow, observation.id)
            if row is None or row.payload != payload:
                raise ObservationConflict("Conflicting market observation event identity")
        return RecordedObservation(observation=observation, inserted=written is not None)

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        return await MarketReader(self._sessions, clock=self._clock).latest(
            identity, include_fixtures=include_fixtures
        )


async def record_provider(provider: MarketProvider, recorder: MarketRecorder) -> int:
    """One explicit ingestion pass. No polling loop, strategy, HTTP or agent code."""
    recorded = 0
    for pair in await provider.discover():
        await record_pair(provider, pair, recorder)
        recorded += 1
    return recorded


async def record_pair(
    provider: MarketProvider,
    pair: MarketPair,
    recorder: MarketRecorder,
) -> MarketSnapshot:
    """Shared immutable discovery binding for one-shot and provider ingestion."""
    return (await record_pair_reporting(provider, pair, recorder)).observation


async def record_pair_reporting(
    provider: MarketProvider,
    pair: MarketPair,
    recorder: MarketRecorder,
) -> RecordedObservation:
    """The same binding, reporting whether this call wrote the event.

    Identical checks in identical order — provenance, identity, normalization —
    because a caller that needs the extra fact must not get a second, laxer
    path to the recorder.
    """
    pair = MarketPair.model_validate(pair.model_dump())
    if pair.provider != provider.provider or pair.is_fixture != provider.is_fixture:
        raise ValueError("Discovery provenance does not match adapter")
    snapshot = await provider.snapshot(pair)
    normalized = normalize_snapshot(
        snapshot.model_dump(), provider=provider.provider, is_fixture=provider.is_fixture
    )
    if normalized.pair.market_identity != pair.market_identity:
        raise ValueError("Provider returned a different market than requested")
    return await recorder.record_reporting(normalized)
