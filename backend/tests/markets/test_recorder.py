import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from src.core.clock import FixedClock
from src.data.tables import MarketObservationRow as Row
from src.markets.fake import InMemoryProvider, fixture_snapshot
from src.markets.models import MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder, ObservationConflict, record_provider


async def test_persist_replay_and_restart(recorder, reader, market_sessions, observation, now):
    assert await recorder.record(observation) == observation
    assert await recorder.record(observation) == observation
    restarted = MarketRecorder(market_sessions, clock=FixedClock(now))
    assert await restarted.latest(observation.asset_id, include_fixtures=True) == observation
    async with market_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Row)) == 1
        row = await session.get(Row, observation.id)
        assert row.payload["price"]["value_usd"] == "2345.123456789012345678"
        assert row.provider == observation.provider
        assert row.chain == "ethereum"
        assert row.network == "mainnet"
        assert row.schema_version == 1
        assert row.correlation_id == observation.correlation_id
    assert await reader.markets() == ()
    assert await reader.latest(observation.pair.pair_id, include_fixtures=True) == observation


async def test_conflicting_identity_never_rewrites(recorder, reader, observation):
    await recorder.record(observation)
    data = observation.model_dump()
    data["price"]["value_usd"] = "1"
    with pytest.raises(ObservationConflict):
        await recorder.record(MarketSnapshot.model_validate(data))
    assert await reader.latest(observation.asset_id, include_fixtures=True) == observation


async def test_model_copy_cannot_bypass_validation(recorder, market_sessions, observation):
    broken = observation.model_copy(
        update={"price": observation.price.model_copy(update={"value_usd": Decimal("-1")})}
    )
    with pytest.raises(ValidationError):
        await recorder.record(broken)
    async with market_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Row)) == 0


async def test_latest_selected_by_observation_time_not_arrival(
    recorder, reader, observation, now, trace
):
    older = fixture_snapshot(now - timedelta(seconds=10), trace)
    await recorder.record(observation)
    await recorder.record(older)
    assert await reader.latest(observation.asset_id, include_fixtures=True) == observation


@pytest.mark.parametrize("newest_state", ["unknown", "unavailable", "stale_nested", "future"])
async def test_newest_invalid_snapshot_does_not_reveal_old_good_value(
    recorder, reader, observation, now, trace, newest_state
):
    await recorder.record(fixture_snapshot(now - timedelta(seconds=10), trace))
    data = observation.model_dump()
    if newest_state in ("unknown", "unavailable"):
        data["price"].update(status=newest_state.upper(), value_usd=None)
    elif newest_state == "stale_nested":
        data["liquidity"]["observed_at"] = now - timedelta(seconds=61)
    else:
        data["observed_at"] = now + timedelta(seconds=1)
    await recorder.record(MarketSnapshot.model_validate(data))
    assert await reader.latest(observation.asset_id, include_fixtures=True) is None
    assert await reader.candidates(include_fixtures=True) == ()


async def test_stale_snapshots_remain_recorded_but_not_returned(
    recorder, reader, now, trace, market_sessions
):
    stale = fixture_snapshot(now - timedelta(seconds=61), trace)
    await recorder.record(stale)
    assert await reader.markets(include_fixtures=True) == ()
    async with market_sessions() as session:
        assert await session.get(Row, stale.id) is not None


async def test_unknown_volume_is_retained_not_invented(recorder, reader, observation):
    data = observation.model_dump()
    data["volume"].update(status="UNKNOWN", value_usd=None)
    await recorder.record(MarketSnapshot.model_validate(data))
    latest = await reader.latest(observation.asset_id, include_fixtures=True)
    assert latest.volume.value_usd is None
    assert (await reader.candidates(include_fixtures=True))[0].snapshot_id == latest.id


async def test_provider_ingestion_pass(recorder, reader, observation):
    provider = InMemoryProvider((observation,))
    assert await record_provider(provider, recorder) == 1
    assert await record_provider(provider, recorder) == 1
    assert len(await reader.markets(include_fixtures=True)) == 1


async def test_tie_order_is_deterministic(recorder, reader, observation):
    other = observation.model_copy(update={"id": uuid4()})
    await recorder.record(other)
    await recorder.record(observation)
    result = await reader.latest(observation.asset_id, include_fixtures=True)
    assert result.id == max(observation.id, other.id)


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL concurrency required"
)
async def test_concurrent_identical_ingestion_is_idempotent(recorder, market_sessions, observation):
    results = await asyncio.gather(*(recorder.record(observation) for _ in range(8)))
    assert all(result == observation for result in results)
    async with market_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Row)) == 1


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL concurrency required"
)
async def test_concurrent_conflict_has_one_winner(recorder, market_sessions, observation):
    data = observation.model_dump()
    data["price"]["value_usd"] = "99"
    changed = MarketSnapshot.model_validate(data)
    results = await asyncio.gather(
        recorder.record(observation), recorder.record(changed), return_exceptions=True
    )
    assert sum(isinstance(result, ObservationConflict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, MarketSnapshot))
    async with market_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Row)) == 1
        assert (await session.get(Row, observation.id)).payload == winner.model_dump(
            mode="json", exclude={"pair": {"pool_locator"}}
        )


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL migration trigger required"
)
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE market_observations SET provider = 'changed'",
        "DELETE FROM market_observations",
        "TRUNCATE market_observations",
    ],
)
async def test_database_enforces_append_only(recorder, market_sessions, observation, sql):
    await recorder.record(observation)
    with pytest.raises(DBAPIError, match="append-only"):
        async with market_sessions.begin() as session:
            await session.execute(text(sql))
    async with market_sessions() as session:
        assert (await session.get(Row, observation.id)).payload == observation.model_dump(
            mode="json", exclude={"pair": {"pool_locator"}}
        )


def provider_observation(observation, provider, *, is_fixture=True):
    data = observation.model_dump()

    def replace(value):
        if isinstance(value, dict):
            if "provider" in value:
                value["provider"] = provider
                value["is_fixture"] = is_fixture
            for item in value.values():
                replace(item)

    replace(data)
    return MarketSnapshot.model_validate(data)


async def test_provider_provenance_conflict_rejected(recorder, observation):
    await recorder.record(observation)
    with pytest.raises(ObservationConflict):
        await recorder.record(provider_observation(observation, "test:other"))


async def test_provider_streams_are_not_merged(recorder, reader, observation):
    other = provider_observation(observation, "test:other").model_copy(update={"id": uuid4()})
    await recorder.record(other)
    await recorder.record(observation)
    results = await reader.markets(include_fixtures=True)
    assert len(results) == 2
    assert {snapshot.provider for snapshot in results} == {"fixture:memory", "test:other"}
    filtered = await reader.markets(provider="test:other", include_fixtures=True)
    assert filtered == (other,)


async def test_fixture_stream_cannot_mask_nonfixture_stream(recorder, reader, observation):
    nonfixture = provider_observation(observation, "test:adapter", is_fixture=False)
    await recorder.record(nonfixture)
    fixture = provider_observation(observation, "test:adapter").model_copy(update={"id": uuid4()})
    await recorder.record(fixture)
    assert await reader.markets() == (nonfixture,)
    assert len(await reader.markets(include_fixtures=True)) == 2


async def test_reader_rechecks_freshness_after_database_read(
    recorder, market_sessions, observation, now
):
    await recorder.record(observation)

    class DelayedClock:
        def __init__(self):
            self.instants = iter([now, now + timedelta(seconds=61)])

        def now(self):
            return next(self.instants)

    reader = MarketReader(market_sessions, clock=DelayedClock())
    assert await reader.markets(include_fixtures=True) == ()


async def test_provider_cannot_return_unrequested_pair(recorder, observation):
    class WrongPairProvider(InMemoryProvider):
        async def snapshot(self, pair):
            snapshot = await super().snapshot(pair)
            return snapshot.model_copy(
                update={"pair": pair.model_copy(update={"pair_id": "ethereum:mainnet:wrong"})}
            )

    with pytest.raises(ValueError, match="different market"):
        await record_provider(WrongPairProvider((observation,)), recorder)


async def test_orbit_port_reads_only_recorded_candidates(recorder, reader, observation):
    from src.agents.orbit import OrbitMarketInput

    async def consume(port: OrbitMarketInput):
        candidates = await port.candidates(include_fixtures=True)
        return await port.latest(candidates[0].pair_id, include_fixtures=True)

    await recorder.record(observation)
    assert await consume(reader) == observation


def changed_base(observation):
    data = observation.model_dump()
    original = observation.asset_id

    def replace(value):
        if isinstance(value, dict):
            if value.get("asset_id") == original:
                value["asset_id"] = "ethereum:mainnet:other-base"
            for item in value.values():
                replace(item)

    replace(data)
    return MarketSnapshot.model_validate(data)


@pytest.mark.parametrize("changed_field", ["quote", "venue", "base"])
async def test_discovery_binds_immutable_market_identity(
    recorder, market_sessions, observation, changed_field
):
    data = observation.model_dump()
    if changed_field == "quote":
        data["pair"]["quote"]["asset_id"] = "ethereum:mainnet:other-quote"
    elif changed_field == "venue":
        data["pair"]["venue"] = "fixture:other-venue"
    else:
        data = changed_base(observation).model_dump()
    replacement = MarketSnapshot.model_validate(data)
    assert replacement.pair.pair_id == observation.pair.pair_id

    class ChangedMarketProvider(InMemoryProvider):
        async def snapshot(self, pair):
            return replacement

    with pytest.raises(ValueError, match="different market"):
        await record_provider(ChangedMarketProvider((observation,)), recorder)
    async with market_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Row)) == 0


async def test_discovery_and_snapshot_event_metadata_may_differ(recorder, reader, observation, now):
    later = fixture_snapshot(now + timedelta(seconds=1), uuid4())
    assert later.pair.id != observation.pair.id
    assert later.pair.observed_at != observation.pair.observed_at
    assert later.pair.correlation_id != observation.pair.correlation_id
    assert later.pair.market_identity == observation.pair.market_identity

    class LaterSnapshotProvider(InMemoryProvider):
        async def snapshot(self, pair):
            return later

    assert await record_provider(LaterSnapshotProvider((observation,)), recorder) == 1
    assert await recorder.record(later) == later


@pytest.mark.parametrize(
    "older_available,newer_available", [(True, False), (False, True), (True, True)]
)
async def test_recording_time_beats_uuid_for_equal_observation_times(
    market_sessions, observation, now, older_available, newer_available
):
    from uuid import UUID

    def event(identity, available):
        data = observation.model_dump()
        data["id"] = identity
        if not available:
            data["price"].update(status="UNKNOWN", value_usd=None)
        return MarketSnapshot.model_validate(data)

    older = event(UUID(int=2), older_available)
    newer = event(UUID(int=1), newer_available)
    assert older.id > newer.id  # UUID order would select the wrong event.
    await MarketRecorder(market_sessions, clock=FixedClock(now)).record(older)
    await MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=1))).record(
        newer
    )
    reader = MarketReader(market_sessions, clock=FixedClock(now + timedelta(seconds=2)))
    snapshots = await reader.markets(include_fixtures=True)
    assert snapshots == ((newer,) if newer_available else ())
    assert await reader.latest(observation.asset_id, include_fixtures=True) == (
        newer if newer_available else None
    )
    candidates = await reader.candidates(include_fixtures=True)
    assert [candidate.snapshot_id for candidate in candidates] == (
        [newer.id] if newer_available else []
    )


async def test_identity_filter_cannot_resurrect_older_stream_event(
    market_sessions, observation, now
):
    await MarketRecorder(market_sessions, clock=FixedClock(now)).record(observation)
    changed = changed_base(observation).model_copy(update={"id": uuid4()})
    await MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=1))).record(
        changed
    )
    reader = MarketReader(market_sessions, clock=FixedClock(now + timedelta(seconds=2)))
    assert await reader.markets(identity=observation.asset_id, include_fixtures=True) == ()
    assert await reader.latest(observation.asset_id, include_fixtures=True) is None
    assert await reader.latest(changed.asset_id, include_fixtures=True) == changed
    assert await reader.latest(observation.pair.pair_id, include_fixtures=True) == changed


async def test_replay_retains_original_recording_time_and_does_not_reorder(
    market_sessions, observation, now
):
    from src.data.repository import aware

    first_recorder = MarketRecorder(market_sessions, clock=FixedClock(now))
    await first_recorder.record(observation)
    newer = observation.model_copy(update={"id": uuid4()})
    await MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=1))).record(
        newer
    )
    await MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=2))).record(
        observation
    )
    async with market_sessions() as session:
        assert aware((await session.get(Row, observation.id)).recorded_at) == now
        assert await session.scalar(select(func.count()).select_from(Row)) == 2
    reader = MarketReader(market_sessions, clock=FixedClock(now + timedelta(seconds=3)))
    assert await reader.latest(observation.asset_id, include_fixtures=True) == newer


async def test_cross_stream_results_use_recording_time_before_uuid(
    market_sessions, observation, now
):
    from uuid import UUID

    older = observation.model_copy(update={"id": UUID(int=2)})
    newer = provider_observation(observation, "test:other").model_copy(update={"id": UUID(int=1)})
    await MarketRecorder(market_sessions, clock=FixedClock(now)).record(older)
    await MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=1))).record(
        newer
    )
    reader = MarketReader(market_sessions, clock=FixedClock(now + timedelta(seconds=2)))
    assert await reader.markets(include_fixtures=True) == (newer, older)
    assert await reader.latest(observation.asset_id, include_fixtures=True) == newer
    assert await reader.markets(provider=older.provider, include_fixtures=True) == (older,)
