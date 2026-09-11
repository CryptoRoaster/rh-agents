from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from src.core.clock import FixedClock
from src.data.tables import MarketObservationRow
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.dto import TokenAttributes
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import Availability, MarketSnapshot, PoolLocator, PoolLocatorKind
from src.markets.recorder import ObservationConflict
from tests.markets import test_geckoterminal as fixtures

payload = fixtures.payload
response = fixtures.response


@pytest.fixture(name="settings")
def provider_settings():
    return fixtures.settings.__wrapped__()


@pytest.fixture(name="handler")
def provider_handler():
    return fixtures.handler.__wrapped__()


@pytest.mark.parametrize(
    "kind,size", [(PoolLocatorKind.CONTRACT_ADDRESS, 40), (PoolLocatorKind.BYTES32_POOL_ID, 64)]
)
def test_explicit_valid_locator(kind, size):
    locator = PoolLocator(kind=kind, value="0x" + "A" * size, venue="venue")
    assert locator.value == "0x" + "a" * size
    assert locator.manager_status == Availability.UNKNOWN
    assert locator.pool_manager_address is None


@pytest.mark.parametrize(
    "kind,size",
    [
        (PoolLocatorKind.CONTRACT_ADDRESS, 38),
        (PoolLocatorKind.CONTRACT_ADDRESS, 42),
        (PoolLocatorKind.BYTES32_POOL_ID, 62),
        (PoolLocatorKind.BYTES32_POOL_ID, 66),
        (PoolLocatorKind.CONTRACT_ADDRESS, 64),
        (PoolLocatorKind.BYTES32_POOL_ID, 40),
    ],
)
def test_invalid_lengths_reject(kind, size):
    with pytest.raises(ValidationError):
        PoolLocator(kind=kind, value="0x" + "a" * size, venue="venue")


@pytest.mark.parametrize(
    "kind,value",
    [
        (PoolLocatorKind.CONTRACT_ADDRESS, "0x" + "0" * 40),
        (PoolLocatorKind.BYTES32_POOL_ID, "0x" + "0" * 64),
        (PoolLocatorKind.BYTES32_POOL_ID, "0x" + "g" * 64),
        (PoolLocatorKind.BYTES32_POOL_ID, "arbitrary"),
    ],
)
def test_invalid_values_reject(kind, value):
    with pytest.raises(ValidationError):
        PoolLocator(kind=kind, value=value, venue="venue")


def test_manager_enrichment_preserves_identity_and_venue_separates():
    base = dict(kind=PoolLocatorKind.BYTES32_POOL_ID, value="0x" + "a" * 64)
    first = PoolLocator(**base, venue="first")
    second = PoolLocator(**base, venue="second")
    managed = PoolLocator(
        **base,
        venue="first",
        pool_manager_address="0x" + "b" * 40,
        manager_status=Availability.AVAILABLE,
    )
    assert first.pair_id("bsc", "mainnet") == managed.pair_id("bsc", "mainnet")
    assert first.identity == managed.identity
    assert len({x.pair_id("bsc", "mainnet") for x in (first, second, managed)}) == 2
    with pytest.raises(ValidationError):
        PoolLocator(**base, venue="first", pool_manager_address="0x" + "b" * 40)


@pytest.mark.parametrize("value", ["0x" + "a" * 64, "0x" + "a" * 38, "0x" + "a" * 42])
def test_tokens_do_not_accept_pool_ids(value):
    with pytest.raises(ValidationError):
        TokenAttributes(address=value, symbol="TEST")


@pytest.mark.parametrize("chain", ["bsc", "robinhood"])
@pytest.mark.parametrize(
    "value",
    ["1.1234567890123456789", "0.000000000012345678901234567890123456789", "1.23456789e-80"],
)
async def test_precise_pool_roundtrip(
    settings, handler, now, chain, value, recorder, reader, market_sessions, monkeypatch
):
    data = payload(chain + "_new_pools")
    data["included"][0]["attributes"]["symbol"] = "測試🙌"
    raw = data["data"][0]
    raw["attributes"]["address"] = "0x" + "c" * 64
    raw["id"] = chain + "_" + raw["attributes"]["address"]
    raw["attributes"]["base_token_price_usd"] = value

    def handle(request):
        if request.url.path.endswith("new_pools"):
            result = response(data)
            return httpx.Response(200, text=result.text.replace('"' + value + '"', value))
        return handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS[chain],
            settings,
            clock=FixedClock(now),
        )
        pair = (await provider.discover())[0]
        snapshot = await provider.snapshot(pair)
        assert snapshot.schema_version == 2
        assert snapshot.pair.pool_locator.kind == PoolLocatorKind.BYTES32_POOL_ID
        assert snapshot.price.value_usd == Decimal(value)
        await recorder.record(snapshot)
        await recorder.record(snapshot)
        assert (await reader.latest(pair.pair_id)).price.value_usd == Decimal(value)
        async with market_sessions() as session:
            row = await session.get(MarketObservationRow, snapshot.id)
            assert isinstance(row.payload["price"]["value_usd"], str)
            assert Decimal(row.payload["price"]["value_usd"]) == Decimal(value)
        changed = snapshot.model_dump()
        changed["price"]["value_usd"] = Decimal("2")
        with pytest.raises(ObservationConflict):
            await recorder.record(MarketSnapshot.model_validate(changed))
        monkeypatch.setenv("DATABASE_URL", settings.database_url)
        from src.api.main import create_app
        from src.api.markets import market_reader

        app = create_app(settings)

        async def dependency():
            yield reader

        app.dependency_overrides[market_reader] = dependency
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            item = (await client.get("/api/markets")).json()[0]
            assert Decimal(item["price"]["value_usd"]) == Decimal(value)
            assert item["pair"]["pool_locator"]["kind"] == "BYTES32_POOL_ID"


@pytest.mark.parametrize("value", ["1e-1001", "1e1001", "1." + "2" * 100])
def test_market_decimal_resource_bounds(observation, value):
    data = observation.model_dump()
    data["price"]["value_usd"] = value
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def test_legacy_payload_replay_unchanged(recorder, market_sessions, observation):
    await recorder.record(observation)
    async with market_sessions() as session:
        row = await session.get(MarketObservationRow, observation.id)
        assert "pool_locator" not in row.payload["pair"]
        before = row.recorded_at
        legacy = row.payload
    await recorder.record(MarketSnapshot.model_validate(legacy))
    async with market_sessions() as session:
        row = await session.get(MarketObservationRow, observation.id)
        assert row.recorded_at == before and row.payload == legacy


@pytest.mark.parametrize(
    "value,expected",
    [(None, Availability.UNKNOWN), (0, Availability.AVAILABLE), ("0", Availability.AVAILABLE)],
)
async def test_null_distinct_from_zero(settings, handler, now, recorder, value, expected):
    data = payload("bsc_new_pools")
    data["data"][0]["attributes"]["reserve_in_usd"] = value

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(now),
        )
        snapshot = await provider.snapshot((await provider.discover())[0])
        assert snapshot.liquidity.status == expected
        assert snapshot.liquidity.value_usd == (None if value is None else Decimal("0"))
        await recorder.record(snapshot)


async def test_same_bytes32_different_venues_are_distinct(settings, handler, now):
    from copy import deepcopy

    data = payload("bsc_new_pools")
    first = data["data"][0]
    first["attributes"]["address"] = "0x" + "c" * 64
    first["id"] = "bsc_" + first["attributes"]["address"]
    second = deepcopy(first)
    second["relationships"]["dex"]["data"]["id"] = "another-venue"
    data["data"] = [first, second]

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(now),
        )
        pairs = await provider.discover()
        assert len(pairs) == 2
        assert pairs[0].pair_id != pairs[1].pair_id


@pytest.mark.parametrize("version_two", [False, True])
async def test_migration_downgrade_preserves_history(
    market_sessions, recorder, observation, version_two
):
    import importlib.util
    import os
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Native PostgreSQL migration required")
    if version_two:
        data = observation.model_dump()
        locator = PoolLocator(
            kind=PoolLocatorKind.BYTES32_POOL_ID, value="0x" + "a" * 64, venue="fixture"
        )
        data["pair"].update(
            pool_locator=locator,
            venue="fixture",
            pair_id=locator.pair_id(observation.chain, observation.network),
        )
        data["schema_version"] = 2
        observation = MarketSnapshot.model_validate(data)
    await recorder.record(observation)
    path = Path(__file__).parents[2] / "migrations/versions/0003_pool_locator.py"
    spec = importlib.util.spec_from_file_location("locator_migration_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            module.upgrade()

    async with market_sessions() as session:
        if version_two:
            with pytest.raises(DBAPIError, match="Downgrade blocked"):
                await (await session.connection()).run_sync(migrate)
            await session.rollback()
        else:
            await (await session.connection()).run_sync(migrate)
            await session.commit()
        row = await session.get(MarketObservationRow, observation.id)
        assert row.schema_version == observation.schema_version
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("DELETE FROM market_observations"))
        await session.rollback()
    await recorder.record(observation)


@pytest.mark.parametrize(
    "kind,size", [(PoolLocatorKind.CONTRACT_ADDRESS, 40), (PoolLocatorKind.BYTES32_POOL_ID, 64)]
)
def test_stable_locator_namespaces(kind, size):
    locator = PoolLocator(kind=kind, value="0x" + "a" * size, venue="first")
    known = PoolLocator(
        **{
            **locator.model_dump(),
            "pool_manager_address": "0x" + "b" * 40,
            "manager_status": Availability.AVAILABLE,
        }
    )
    assert locator.identity == known.identity
    assert locator.pair_id("bsc", "mainnet") == known.pair_id("bsc", "mainnet")
    assert locator.pair_id("bsc", "mainnet") != locator.pair_id("robinhood", "mainnet")
    different = PoolLocator(kind=kind, value="0x" + "c" * size, venue="first")
    assert locator.pair_id("bsc", "mainnet") != different.pair_id("bsc", "mainnet")
    if kind == PoolLocatorKind.CONTRACT_ADDRESS:
        assert locator.pair_id("bsc", "mainnet") == "bsc:mainnet:contract_address:0x" + "a" * 40
    else:
        assert (
            locator.pair_id("bsc", "mainnet") == "bsc:mainnet:bytes32_pool_id:first:0x" + "a" * 64
        )


async def test_manager_enrichment_one_stream_with_binding_and_replay(
    settings, handler, now, market_sessions, recorder, reader
):
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import func, select

    from src.markets.recorder import MarketRecorder, record_pair

    data = payload("bsc_new_pools")
    raw = data["data"][0]
    raw["attributes"]["address"] = "0x" + "c" * 64
    raw["id"] = "bsc_" + raw["attributes"]["address"]

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(now),
        )
        pair = (await provider.discover())[0]
        original = await provider.snapshot(pair)
        await record_pair(provider, pair, recorder)
        await record_pair(provider, pair, recorder)
        enriched_data = original.model_dump()
        enriched_data["id"] = uuid4()
        enriched_data["pair"]["pool_locator"].update(
            pool_manager_address="0x" + "b" * 40, manager_status=Availability.AVAILABLE
        )
        enriched = MarketSnapshot.model_validate(enriched_data)
        assert enriched.pair.pair_id == pair.pair_id
        assert enriched.pair.market_identity == pair.market_identity
        assert (
            "pool_manager_address" not in enriched.pair.market_identity.model_dump()["pool_locator"]
        )
        assert await provider.snapshot(enriched.pair) == original

        class ResolvedProvider:
            provider = "geckoterminal"
            is_fixture = False

            async def snapshot(self, pair):
                return enriched

            async def discover(self):
                return (pair,)

        later = MarketRecorder(market_sessions, clock=FixedClock(now + timedelta(seconds=1)))
        await record_pair(ResolvedProvider(), pair, later)
        await record_pair(ResolvedProvider(), pair, later)
        latest = await reader.latest(pair.pair_id)
        assert latest.id == enriched.id
        assert latest.pair.pool_locator.manager_status == Availability.AVAILABLE
        async with market_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(MarketObservationRow)) == 2
            assert (
                await session.scalar(
                    select(func.count(func.distinct(MarketObservationRow.pair_id)))
                )
                == 1
            )
        enriched_data["id"] = original.id
        with pytest.raises(ObservationConflict):
            await recorder.record(MarketSnapshot.model_validate(enriched_data))


async def test_ingestion_counts_unknown_and_rejections(settings, handler, now, recorder):
    from copy import deepcopy

    from src.markets.ingest import ingest_chain

    data = payload("bsc_new_pools")
    first = data["data"][0]
    unknown = deepcopy(first)
    unknown["attributes"].update(address="0x" + "d" * 40, reserve_in_usd=None)
    unknown["id"] = "bsc_" + unknown["attributes"]["address"]
    invalid = deepcopy(first)
    invalid["attributes"]["address"] = "invalid"
    data["data"] = [first, unknown, invalid]

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(now),
        )
        result = await ingest_chain(provider, recorder)
        assert (result.discovered, result.recorded, result.readable, result.unavailable) == (
            3,
            2,
            1,
            1,
        )
        assert (result.rejected, result.failed) == (1, 1)
        assert result.error is None
        assert result.reasons == {"provider_contract": 1}
        assert "reasons=provider_contract:1" in result.safe_text()


async def test_readback_error_does_not_reclassify_persistence(
    settings, handler, now, recorder, monkeypatch
):
    from src.markets.ingest import ingest_chain

    async def broken(identity):
        raise OSError("sensitive internal connection diagnostic")

    monkeypatch.setattr(recorder, "latest", broken)
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handler)
    ) as transport:
        provider = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(now),
        )
        result = await ingest_chain(provider, recorder)
        assert result.recorded == 1
        assert result.readable == result.unavailable == result.rejected == 0
        assert result.failed == 1
        assert result.reasons == {"readback_failed": 1}
        assert "sensitive" not in result.safe_text()
