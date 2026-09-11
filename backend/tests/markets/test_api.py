from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.api.markets import market_reader
from src.core.config import Settings
from src.markets.fake import fixture_snapshot


@pytest.fixture
async def client(monkeypatch, reader):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app

    app = create_app(Settings(_env_file=None))

    async def recorded_reader():
        yield reader

    app.dependency_overrides[market_reader] = recorded_reader
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_market_api_read_paths(client, recorder, observation):
    await recorder.record(observation)
    assert (await client.get("/api/markets")).json() == []
    assert (await client.get("/api/market-candidates")).json() == []
    response = await client.get("/api/markets", params={"include_fixtures": True})
    assert response.status_code == 200
    (item,) = response.json()
    assert item["id"] == str(observation.id)
    assert item["price"]["value_usd"] == "2345.123456789012345678"
    assert item["is_fixture"] is True
    assert item["provider"] == "fixture:memory"
    for identity in (observation.asset_id, observation.pair.pair_id):
        response = await client.get(f"/api/markets/{identity}", params={"include_fixtures": True})
        assert response.status_code == 200
        assert response.json() == item
    candidates = await client.get("/api/market-candidates", params={"include_fixtures": True})
    assert candidates.status_code == 200
    assert candidates.json()[0]["snapshot_id"] == str(observation.id)
    assert candidates.json()[0]["is_fixture"] is True


async def test_api_filters_pagination_and_absence(client, recorder, observation):
    await recorder.record(observation)
    for filter_name, value in [("chain", "solana"), ("network", "sepolia"), ("provider", "other")]:
        assert (
            await client.get("/api/markets", params={"include_fixtures": True, filter_name: value})
        ).json() == []
    assert (
        await client.get("/api/markets", params={"include_fixtures": True, "offset": 1})
    ).json() == []
    assert (await client.get("/api/markets/missing")).status_code == 404
    for path in ("/api/markets", "/api/market-candidates"):
        assert (await client.get(path, params={"limit": 101})).status_code == 422
        assert (await client.get(path, params={"offset": -1})).status_code == 422


async def test_api_hides_stale_snapshot(client, recorder, now, trace):
    observation = fixture_snapshot(now - timedelta(seconds=61), trace)
    await recorder.record(observation)
    assert (await client.get("/api/markets", params={"include_fixtures": True})).json() == []
    assert (
        await client.get(f"/api/markets/{observation.asset_id}", params={"include_fixtures": True})
    ).status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_no_market_mutation_endpoints(client, method):
    for path in ("/api/markets", "/api/markets/ethereum:mainnet:any", "/api/market-candidates"):
        assert (await client.request(method, path, json={})).status_code == 405


async def test_database_failure_returns_503(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app
    from src.markets.reader import MarketReader

    async def unavailable(self, **kwargs):
        raise OperationalError("SELECT", {}, Exception("unavailable"))

    monkeypatch.setattr(MarketReader, "markets", unavailable)
    app = create_app(Settings(_env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/markets")).status_code == 503


async def test_readiness_requires_new_migration(monkeypatch, market_sessions):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app

    engine = market_sessions.kw["bind"]

    # Use a small proxy to avoid disposing the fixture engine before teardown.
    class EngineView:
        def connect(self):
            return engine.connect()

        async def dispose(self):
            pass

    monkeypatch.setattr("src.api.main.connect", lambda url: (EngineView(), market_sessions))
    async with market_sessions.begin() as session:
        await session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await session.execute(text("INSERT INTO alembic_version VALUES ('0001')"))
    app = create_app(Settings(_env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/ready")).status_code == 503
        async with market_sessions.begin() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = '0005'"))
        assert (await client.get("/ready")).status_code == 200


@pytest.mark.parametrize("max_age,expected_count", [(5, 0), (20, 1)])
async def test_runtime_dependency_uses_configured_freshness(
    monkeypatch, recorder, market_sessions, now, trace, max_age, expected_count
):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app
    from src.core.clock import SystemClock

    monkeypatch.setattr(SystemClock, "now", lambda self: now)
    await recorder.record(fixture_snapshot(now - timedelta(seconds=10), trace))

    class EngineView:
        async def dispose(self):
            pass

    monkeypatch.setattr("src.api.markets.connect", lambda url: (EngineView(), market_sessions))
    app = create_app(Settings(_env_file=None, market_max_age_seconds=max_age))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/markets", params={"include_fixtures": True})
        assert response.status_code == 200
        assert len(response.json()) == expected_count
