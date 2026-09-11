from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.trade_cases import trade_case_service
from src.core.config import Settings
from src.markets.fake import fixture_snapshot


@pytest.fixture
async def workflow_client(monkeypatch, workflow_service, now, trace):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app

    trade_case = await workflow_service.open_trade_case(
        fixture_snapshot(now, trace).pair.market_identity,
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key="api-case",
        expires_at=now + timedelta(hours=1),
    )
    app = create_app(Settings(_env_file=None))

    async def service_override():
        yield workflow_service

    app.dependency_overrides[trade_case_service] = service_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, trade_case


async def test_read_only_trade_case_views(workflow_client):
    client, trade_case = workflow_client
    response = await client.get("/api/trade-cases")
    assert response.status_code == 200
    assert response.json()[0]["id"] == str(trade_case.id)
    assert response.json()[0]["status"] == "EVIDENCE_PENDING"
    for suffix, minimum in (("", 1), ("/timeline", 1), ("/evidence", 1), ("/tasks", 8)):
        response = await client.get(f"/api/trade-cases/{trade_case.id}{suffix}")
        assert response.status_code == 200
        if suffix:
            assert len(response.json()) >= minimum


async def test_trade_case_filters_and_missing(workflow_client):
    client, trade_case = workflow_client
    assert (
        len((await client.get("/api/trade-cases", params={"status": "EVIDENCE_PENDING"})).json())
        == 1
    )
    assert (await client.get("/api/trade-cases", params={"chain": "bsc"})).json() == []
    assert (
        len(
            (
                await client.get("/api/trade-cases", params={"market": trade_case.market.pair_id})
            ).json()
        )
        == 1
    )
    missing = uuid4()
    for suffix in ("", "/timeline", "/evidence", "/tasks"):
        assert (await client.get(f"/api/trade-cases/{missing}{suffix}")).status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_no_trade_case_mutation_routes(workflow_client, method):
    client, trade_case = workflow_client
    for path in ("/api/trade-cases", f"/api/trade-cases/{trade_case.id}"):
        assert (await client.request(method, path, json={})).status_code == 405
