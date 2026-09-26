"""Cockpit read APIs over a real database: GET only, honest about what is recorded."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from src.api.paper import paper_reader
from src.api.scout import scout_reader
from src.core.config import Settings
from src.data.tables import DiscoveryWatchAssessmentRow, PnLRow, PositionRow, TradeCaseRow
from src.ledger.read import PaperReadService
from src.reasoning.models import ReasoningErrorCategory
from src.scout.policy import WatchStatus
from src.scout.read import ScoutReadService
from src.scout.repository import WatchRepository
from tests.scout.conftest import (
    POOLS,
    EchoOrbit,
    MarketProvider,
    pair_id,
    scout,
    scout_settings,
    young,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)


@pytest.fixture
async def client(db, monkeypatch):
    _, sessions = db
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://cockpit@localhost/cockpit")
    from src.api.main import create_app

    app = create_app(Settings(_env_file=None))

    async def scout_override():
        yield ScoutReadService(sessions)

    async def paper_override():
        yield PaperReadService(sessions)

    app.dependency_overrides[scout_reader] = scout_override
    app.dependency_overrides[paper_reader] = paper_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, sessions, app


async def watch_id(sessions, index):
    return (await WatchRepository(sessions).by_pair(pair_id(POOLS[index]))).id


# ------------------------------------------------------------------- read-only


async def test_every_cockpit_route_is_get_only(client):
    _, _, app = client
    paths = app.openapi()["paths"]
    cockpit = {
        path: set(methods)
        for path, methods in paths.items()
        if path.startswith(("/api/scout", "/api/paper"))
    }
    assert len(cockpit) >= 6
    for path, methods in cockpit.items():
        assert methods == {"get"}, path


async def test_a_write_to_a_cockpit_route_is_refused(client):
    http, _, _ = client
    for path in ("/api/scout/watches", "/api/scout/runs", "/api/paper/portfolio"):
        assert (await http.post(path, json={})).status_code == 405
        assert (await http.delete(path)).status_code == 405


# --------------------------------------------------------------------- watches


async def test_stale_and_unpriced_watches_are_listed_honestly(client):
    http, sessions, _ = client
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0, price=None), young(1)]),
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=0),
    )
    page = (await http.get("/api/scout/watches")).json()
    assert page["total"] == 2
    unpriced = next(item for item in page["items"] if item["pair_id"] == pair_id(POOLS[0]))
    snapshot = unpriced["latest_snapshot"]
    # Hours old by now, and still listed: nothing is hidden for being stale.
    assert snapshot["observed_at"].startswith("2026-09-26T06:00:00")
    assert snapshot["price_status"] == "UNKNOWN" and snapshot["price_usd"] is None
    assert snapshot["liquidity_status"] == "AVAILABLE"
    assert snapshot["base_symbol"] and snapshot["quote_symbol"]
    assert unpriced["is_fixture"] is False
    assert unpriced["venue"] == "uniswap-v3"
    assert unpriced["age_seconds"] > 0


async def test_watch_list_order_filters_and_pages(client):
    http, sessions, _ = client
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0)]),
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=0),
    )
    await scout(
        sessions,
        T0 + timedelta(hours=1),
        provider=MarketProvider(discovery=[young(1), young(2)]),
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=0),
    )
    listed = [item["pair_id"] for item in (await http.get("/api/scout/watches")).json()["items"]]
    # Newest discovery first, pair as the tiebreak.
    assert listed == [pair_id(POOLS[1]), pair_id(POOLS[2]), pair_id(POOLS[0])]
    page = (await http.get("/api/scout/watches", params={"limit": 1, "offset": 1})).json()
    assert [item["pair_id"] for item in page["items"]] == [pair_id(POOLS[2])]
    assert page["total"] == 3
    retired = await watch_id(sessions, 2)
    await WatchRepository(sessions).retire(retired, "MARKET_IDENTITY_MISMATCH", T0)
    only = (await http.get("/api/scout/watches", params={"status": "RETIRED"})).json()
    assert [item["watch_id"] for item in only["items"]] == [str(retired)]
    assert only["items"][0]["reason_code"] == "MARKET_IDENTITY_MISMATCH"
    none = (await http.get("/api/scout/watches", params={"has_assessment": "true"})).json()
    assert none["total"] == 0
    assert (await http.get("/api/scout/watches", params={"venue": "uniswap-v3"})).json()[
        "total"
    ] == 3


async def test_a_promotable_watch_links_its_trade_case(client):
    http, sessions, _ = client
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0)]),
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=0),
    )
    repository = WatchRepository(sessions)
    watch = await repository.by_pair(pair_id(POOLS[0]))
    await repository.settle_history(
        watch.id,
        expected_next=watch.next_history_review_at,
        verdict="SUFFICIENT",
        status=WatchStatus.PROMOTABLE,
        next_at=None,
        now=T0 + timedelta(hours=24),
    )
    case_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            TradeCaseRow(
                id=case_id,
                workflow_version="trade-case-v1",
                market_key=watch.pair_id,
                chain=watch.chain,
                network=watch.network,
                status="EVIDENCE_PENDING",
                opened_at=T0 + timedelta(hours=25),
                updated_at=T0 + timedelta(hours=25),
                expires_at=T0 + timedelta(hours=26),
                originating_discovery_reference=uuid4(),
                strategy_policy_id=None,
                revision=1,
                reason_code="CASE_OPENED",
                blockers=[],
                risk_input_digest=None,
                correlation_id=uuid4(),
                open_idempotency_key="cockpit-case",
                open_fingerprint="c" * 64,
                market_payload=watch.market.model_dump(mode="json"),
            )
        )
    await repository.mark_promoted(watch.pair_id, case_id, T0 + timedelta(hours=25))
    promotable = (await http.get("/api/scout/watches", params={"promotable": "true"})).json()
    assert promotable["items"][0]["last_promoted_trade_case_id"] == str(case_id)
    detail = (await http.get(f"/api/scout/watches/{watch.id}")).json()
    assert detail["trade_case"] == {
        "trade_case_id": str(case_id),
        "status": "EVIDENCE_PENDING",
        "opened_at": detail["trade_case"]["opened_at"],
    }


async def test_an_unknown_watch_is_not_found(client):
    http, _, _ = client
    assert (await http.get(f"/api/scout/watches/{uuid4()}")).status_code == 404
    assert (await http.get(f"/api/scout/watches/{uuid4()}/assessments")).status_code == 404


# -------------------------------------------------------------------- timeline


async def test_the_timeline_is_chronological_and_shows_failures_and_coalescing(client):
    http, sessions, _ = client
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider, orbit=EchoOrbit())
    provider.discovery = []
    await scout(
        sessions,
        T0 + timedelta(hours=4),
        provider=provider,
        orbit=EchoOrbit(failure=ReasoningErrorCategory.PROVIDER_TIMEOUT),
    )
    watch = await watch_id(sessions, 0)
    async with sessions() as session:
        before = await session.scalar(select(func.count()).select_from(DiscoveryWatchAssessmentRow))
    timeline = (await http.get(f"/api/scout/watches/{watch}/assessments")).json()
    assert [item["checkpoint_seconds"] for item in timeline] == [0, 10800]
    assert timeline[0]["status"] == "COMPLETED" and timeline[0]["classification"]
    assert timeline[1]["status"] == "FAILED"
    assert timeline[1]["failure_reason"] == "PROVIDER_TIMEOUT"
    assert timeline[1]["classification"] is None
    assert "prompt" not in timeline[0] and "instructions" not in timeline[0]
    detail = (await http.get(f"/api/scout/watches/{watch}")).json()
    states = [item["state"] for item in detail["checkpoints"]]
    assert states[:3] == ["ASSESSED", "COALESCED", "FAILED"]
    assert all(state in ("DUE", "PENDING") for state in states[3:])
    async with sessions() as session:
        after = await session.scalar(select(func.count()).select_from(DiscoveryWatchAssessmentRow))
    assert before == after


# ------------------------------------------------------------------------ runs


async def test_runs_newest_first_with_rates_and_backlog(client):
    http, sessions, _ = client
    await scout(sessions, T0, provider=MarketProvider())
    await scout(
        sessions,
        T0 + timedelta(minutes=15),
        provider=MarketProvider(discovery=[young(0), young(1)]),
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=1),
    )
    page = (await http.get("/api/scout/runs")).json()
    assert page["total"] == 2
    newest, oldest = page["items"]
    assert newest["run"]["discovered"] == 2
    assert newest["identity_acceptance_rate"] == 1.0
    assert newest["run"]["orbit_backlog_before"] == 2
    assert newest["run"]["orbit_backlog_after"] == 1
    assert oldest["run"]["discovered"] == 0
    assert oldest["identity_acceptance_rate"] is None
    assert oldest["watch_creation_rate"] is None
    overview = (await http.get("/api/scout/overview")).json()
    assert overview["watches"] == 2
    assert overview["by_status"]["WATCHING"] == 2
    assert overview["latest_run"]["run"]["id"] == newest["run"]["id"]


async def test_the_overview_of_an_empty_scout(client):
    http, _, _ = client
    overview = (await http.get("/api/scout/overview")).json()
    assert overview["watches"] == 0
    assert overview["orbit_backlog"] == 0
    assert overview["oldest_orbit_due_age_seconds"] is None
    assert overview["latest_run"] is None
    assert (await http.get("/api/scout/runs")).json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


# ------------------------------------------------------------------- portfolio


async def test_an_empty_paper_ledger_is_empty_not_invented(client):
    http, _, _ = client
    body = (await http.get("/api/paper/portfolio")).json()
    assert body["positions"] == [] and body["fills"] == [] and body["pnl"] == []
    assert body["account"]["cash_usd"] is not None


async def test_booked_paper_state_is_shown_as_recorded(client):
    http, sessions, _ = client
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            PositionRow(
                id=uuid4(),
                asset_id="bsc:mainnet:0x" + "ab" * 20,
                market_pair_id="bsc:mainnet:contract_address:0x" + "cd" * 20,
                market_chain="bsc",
                market_network="mainnet",
                market_provider="geckoterminal",
                quantity=Decimal("12.5"),
                cost_basis_usd=Decimal("25"),
                realized_pnl_usd=Decimal("0"),
                created_at=now,
                updated_at=now,
                source="test",
                correlation_id=uuid4(),
            )
        )
        session.add(
            PnLRow(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                source="ledger",
                correlation_id=uuid4(),
                schema_version=1,
                payload={
                    "id": str(uuid4()),
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "source": "ledger",
                    "correlation_id": str(uuid4()),
                    "cash_usd": "9975",
                    "market_value_usd": "26",
                    "equity_usd": "10001",
                    "realized_pnl_usd": "0",
                    "unrealized_pnl_usd": "1",
                    "total_pnl_usd": "1",
                    "fees_paid_usd": "0",
                },
            )
        )
    body = (await http.get("/api/paper/portfolio")).json()
    (position,) = body["positions"]
    assert position["open"] is True
    assert Decimal(position["quantity"]) == Decimal("12.5")
    (pnl,) = body["pnl"]
    assert Decimal(pnl["snapshot"]["unrealized_pnl_usd"]) == Decimal("1")
