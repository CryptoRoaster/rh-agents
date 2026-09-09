import asyncio
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from src.core.config import Settings
from src.core.models import AgentDecision, AgentRole, IntentProposal, Observation, TradingMode
from src.orchestration.bus import DecisionBus


def decision(now, trace):
    return AgentDecision(
        source="ORBIT",
        correlation_id=trace,
        agent=AgentRole.ORBIT,
        decision_at=now,
        confidence=Decimal("0.7"),
        rationale="Synthetic opportunity",
        latency_ms=12,
        payload=Observation(asset_id="paper:DEMO", assessment="WATCH", evidence_ids=(trace,)),
    )


async def test_typed_decision_async_roundtrip(now, trace):
    bus = DecisionBus(capacity=1)
    item = decision(now, trace)
    await bus.publish(item)
    received = await asyncio.wait_for(bus.receive(), timeout=1)
    assert received == item
    assert isinstance(received.payload, Observation)
    bus.acknowledge()
    await asyncio.wait_for(bus.join(), timeout=1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent", "SENTINEL"),
        ("agent", "EXECUTOR"),
        ("confidence", "1.1"),
        ("confidence", "NaN"),
        ("latency_ms", -1),
        ("private_key", "not-a-key"),
        ("payload", {"kind": "untyped", "instruction": "buy"}),
    ],
)
def test_decision_validation(now, trace, field, value):
    data = decision(now, trace).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(data)


def test_trade_proposal_requires_correct_role_and_trace(intent, now, trace):
    data = decision(now, trace).model_dump()
    data["payload"] = IntentProposal(intent=intent)
    with pytest.raises(ValidationError, match="Only FUSE"):
        AgentDecision.model_validate(data)
    data["agent"] = AgentRole.COMMANDER
    assert AgentDecision.model_validate(data).agent == AgentRole.COMMANDER


def test_live_configuration_refused():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://test_user@localhost/test_database",
            trading_mode=TradingMode.LIVE_AUTONOMOUS,
        )


async def test_api_is_read_only_and_observe_by_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test_user@localhost/test_database")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    from src.api.main import create_app

    app = create_app(Settings(_env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).json()["mode"] == "OBSERVE"
        data = (await client.get("/api/system")).json()
        assert data["live_enabled"] is False
        assert data["controls_writable"] is False
        assert len(data["agents"]) == 11
        assert (await client.post("/api/trade", json={})).status_code == 404
