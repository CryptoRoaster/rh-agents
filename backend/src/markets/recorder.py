"""Durable append-only ingestion with database-enforced event identity."""

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


class MarketRecorder:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Clock | None = None
    ) -> None:
        self._sessions = sessions
        self._clock = clock if clock is not None else SystemClock()

    async def record(self, observation: MarketSnapshot) -> MarketSnapshot:
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
            await session.execute(statement)
            row = await session.get(MarketObservationRow, observation.id)
            if row is None or row.payload != payload:
                raise ObservationConflict("Conflicting market observation event identity")
        return observation

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
    pair = MarketPair.model_validate(pair.model_dump())
    if pair.provider != provider.provider or pair.is_fixture != provider.is_fixture:
        raise ValueError("Discovery provenance does not match adapter")
    snapshot = await provider.snapshot(pair)
    normalized = normalize_snapshot(
        snapshot.model_dump(), provider=provider.provider, is_fixture=provider.is_fixture
    )
    if normalized.pair.market_identity != pair.market_identity:
        raise ValueError("Provider returned a different market than requested")
    return await recorder.record(normalized)
