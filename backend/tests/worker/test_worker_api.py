from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.workers import worker_service
from src.core.config import Settings
from src.core.models import AgentRole
from src.orchestration.worker.models import WorkerRegistration
from tests.worker.conftest import open_case


@pytest.fixture
async def worker_client(monkeypatch, runtime, now, trace):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    from src.api.main import create_app

    trade_case = await open_case(runtime.cases, now, trace, "api-worker-case")
    instance = await runtime.register_worker(
        WorkerRegistration(
            registration_key="api-atlas",
            role=AgentRole.ATLAS,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(instance.worker_instance_id)
    app = create_app(Settings(_env_file=None))

    async def service_override():
        yield runtime

    app.dependency_overrides[worker_service] = service_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, instance, lease, trade_case


async def test_worker_runtime_reports_a_disabled_posture_without_inventing_agents(worker_client):
    client, _, _, _ = worker_client
    body = (await client.get("/api/worker-runtime")).json()
    assert body["enabled"] is False
    # No reasoning worker exists, and the API must never claim one does.
    assert body["reasoning_workers_implemented"] is False
    assert body["policy_version"] == "worker-runtime-v1"
    assert body["role_evidence"]["ATLAS"] == "ONCHAIN_EVIDENCE"
    assert "SENTINEL" not in body["role_evidence"]
    # FUSE gained a synthesis evidence type in Phase 2K; COMMANDER has none and
    # SENTINEL is not a worker role at all.
    assert body["role_evidence"]["FUSE"] == "SYNTHESIS_EVIDENCE"
    assert "COMMANDER" not in body["role_evidence"]


async def test_read_only_worker_views(worker_client):
    client, instance, lease, trade_case = worker_client
    listed = (await client.get("/api/workers")).json()
    assert [item["worker_instance_id"] for item in listed] == [str(instance.worker_instance_id)]
    assert listed[0]["status"] == "ACTIVE"

    detail = (await client.get(f"/api/workers/{instance.worker_instance_id}")).json()
    assert detail["role"] == "ATLAS"

    attempts = (
        await client.get("/api/worker-attempts", params={"trade_case_id": str(trade_case.id)})
    ).json()
    assert len(attempts) == 1
    assert attempts[0]["task_id"] == str(lease.task_id)
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["outcome"] is None


async def test_worker_filters_and_missing(worker_client):
    client, instance, _, _ = worker_client
    assert len((await client.get("/api/workers", params={"role": "ATLAS"})).json()) == 1
    assert (await client.get("/api/workers", params={"role": "SIGNAL"})).json() == []
    assert (await client.get(f"/api/workers/{uuid4()}")).status_code == 404
    assert (
        await client.get("/api/worker-attempts", params={"worker_instance_id": str(uuid4())})
    ).json() == []


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_no_public_worker_mutation_routes(worker_client, method):
    client, instance, lease, _ = worker_client
    # Claim, heartbeat, complete and fail stay internal application capabilities.
    paths = (
        "/api/workers",
        f"/api/workers/{instance.worker_instance_id}",
        "/api/worker-attempts",
        "/api/worker-runtime",
        f"/api/worker-tasks/{lease.task_id}/claim",
        f"/api/worker-tasks/{lease.task_id}/complete",
        "/api/evidence",
    )
    for path in paths:
        assert (await client.request(method, path, json={})).status_code in (404, 405)


def test_router_exposes_only_get_routes():
    from src.api.workers import router

    for route in router.routes:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}
