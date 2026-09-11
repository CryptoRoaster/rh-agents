"""GeckoTerminal contract fixtures; no live provider access or credentials in CI."""

import asyncio
import json
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.config import Settings
from src.data.tables import MarketObservationRow
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.dto import PoolAttributes
from src.markets.geckoterminal.errors import (
    AuthenticationError,
    BudgetError,
    ClientError,
    ConfigurationError,
    ConnectivityError,
    ContractError,
    IdentityError,
    RateLimitError,
    UnavailableError,
    UnsupportedNetworkError,
)
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory, selected_chains
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.ingest import ingest_chain
from src.markets.models import Availability

FIXTURES = Path(__file__).parent / "fixtures/geckoterminal"


def fixture(name):
    return (FIXTURES / f"{name}.json").read_text()


def payload(name):
    return json.loads(fixture(name), parse_float=Decimal)


def response(data):
    return httpx.Response(200, text=json.dumps(data, default=str))


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="robinhood,bsc",
    )


@pytest.fixture
def handler():
    def handle(request):
        assert request.headers["Accept"] == "application/json;version=20230203"
        assert request.headers["User-Agent"] == "rh-agents/0.1.0"
        assert "authorization" not in request.headers and "x-cg-pro-api-key" not in request.headers
        assert request.url.path.startswith("/api/v2/networks")
        if request.url.path.endswith("/networks"):
            return httpx.Response(200, text=fixture("networks"))
        assert request.url.params["include"] == "base_token,quote_token,dex"
        chain = request.url.path.split("/")[-2]
        return httpx.Response(200, text=fixture(chain + "_new_pools"))

    return handle


def adapter(transport, settings, now, chain="bsc", directory=None):
    return GeckoTerminalAdapter(
        transport,
        directory or NetworkDirectory(transport, settings),
        CHAINS[chain],
        settings,
        clock=FixedClock(now),
    )


@pytest.mark.parametrize("chain,chain_id", [("robinhood", 4663), ("bsc", 56)])
async def test_complete_chain_transport_postgresql_reader_api(
    settings,
    handler,
    now,
    chain,
    chain_id,
    recorder,
    reader,
    market_sessions,
    monkeypatch,
):
    calls = []

    def handle(request):
        calls.append(request)
        result = handler(request)
        if request.url.path.endswith("new_pools"):
            # Deliberately use a JSON numeric literal instead of the usual documented string.
            return httpx.Response(
                200, text=result.text.replace('"123.123456789012345678"', "123.123456789012345678")
            )
        return result

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now, chain)
        (pair,) = await provider.discover()
        snapshot = await provider.snapshot(pair)
        assert provider.chain.chain_id == chain_id
        assert snapshot.price.value_usd == Decimal("123.123456789012345678")
        assert snapshot.liquidity.value_usd == Decimal("123456.123456789012345678")
        assert snapshot.volume.value_usd == Decimal("654321.987654321098765432")
        assert snapshot.volume.window_seconds == 86400
        assert snapshot.provider == "geckoterminal" and snapshot.is_fixture is False
        assert snapshot.chain == chain and snapshot.network == "mainnet"
        assert snapshot.observed_at == now
        assert pair.asset_id == f"{chain}:mainnet:0x" + "a" * 40
        assert pair.pair_id == f"{chain}:mainnet:contract_address:0x" + "c" * 40
        assert len(calls) == 2  # No per-pool lookups.
        assert await provider.snapshot(pair) is snapshot
        await recorder.record(snapshot)
        await recorder.record(snapshot)
        assert await reader.latest(pair.pair_id) == snapshot
        async with market_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(MarketObservationRow)) == 1
        monkeypatch.setenv("DATABASE_URL", settings.database_url)
        from src.api.main import create_app
        from src.api.markets import market_reader

        app = create_app(settings)

        async def recorded_reader():
            yield reader

        app.dependency_overrides[market_reader] = recorded_reader
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            result = await client.get("/api/markets")
            data = result.json()[0]
            assert data["price"]["value_usd"] == "123.123456789012345678"
            assert data["liquidity"]["value_usd"] == "123456.123456789012345678"
            assert data["is_fixture"] is False
            assert (await client.get("/api/market-candidates")).json()[0]["snapshot_id"] == str(
                snapshot.id
            )
        monkeypatch.setattr(recorder, "_clock", FixedClock(now + timedelta(seconds=1)))
        summary = await ingest_chain(provider, recorder)
        assert summary.safe_text() == (
            f"provider=geckoterminal chain={chain} discovered=1 recorded=1 "
            "readable=1 unavailable=0 rejected=0 failed=0 reasons=none"
        )


async def test_transport_never_materializes_float(settings):
    body = '{"data":{"amount":0.123456789012345678,"count":4}}'
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as transport:
        data = await transport.get("networks", {})
        assert type(data["data"]["amount"]) is Decimal
        assert type(data["data"]["count"]) is int


@pytest.mark.parametrize("field", ["base_token_price_usd", "reserve_in_usd", "volume_usd"])
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_and_null_stay_unknown(settings, handler, now, field, missing):
    data = payload("bsc_new_pools")
    values = data["data"][0]["attributes"]
    if missing:
        del values[field]
    else:
        values[field] = None
    parsed = PoolAttributes.model_validate(values)
    assert (field in parsed.model_fields_set) is not missing

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        snapshot = await provider.snapshot((await provider.discover())[0])
        item = getattr(
            snapshot,
            {
                "base_token_price_usd": "price",
                "reserve_in_usd": "liquidity",
                "volume_usd": "volume",
            }[field],
        )
        assert item.status == Availability.UNKNOWN and item.value_usd is None


async def test_explicit_zeros(settings, handler, now):
    data = payload("bsc_new_pools")
    data["data"][0]["attributes"].update(
        base_token_price_usd="0", reserve_in_usd="0", volume_usd={"h24": "0"}
    )

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        snapshot = await provider.snapshot((await provider.discover())[0])
        assert snapshot.liquidity.value_usd == 0 and snapshot.volume.value_usd == 0
        assert snapshot.liquidity.status == Availability.AVAILABLE
        assert snapshot.price.status == Availability.UNKNOWN  # Existing positive-price invariant.


@pytest.mark.parametrize("value", [True, False, 1.25, -1, "NaN", "Infinity", "1e-1001", [], {}])
def test_unsafe_numeric_types_reject(value):
    data = payload("bsc_new_pools")["data"][0]["attributes"]
    data["reserve_in_usd"] = value
    with pytest.raises(ValidationError):
        PoolAttributes.model_validate(data)


@pytest.mark.parametrize(
    "defect",
    [
        "pool_missing",
        "pool_malformed",
        "pool_zero",
        "token_missing",
        "token_malformed",
        "token_zero",
        "base_missing",
        "quote_missing",
        "same_asset",
        "wrong_type",
        "wrong_network",
        "wrong_resource",
        "wrong_provider",
        "wrong_chain",
        "wrong_chain_id",
        "fixture",
    ],
)
async def test_malformed_pool_is_counted(settings, handler, now, defect):
    data = payload("bsc_new_pools")
    pool = data["data"][0]
    token = data["included"][0]
    if defect == "pool_missing":
        del pool["attributes"]["address"]
    elif defect in ("pool_malformed", "pool_zero"):
        pool["attributes"]["address"] = "bad" if defect.endswith("malformed") else "0x" + "0" * 40
    elif defect == "token_missing":
        del token["attributes"]["address"]
    elif defect in ("token_malformed", "token_zero"):
        token["attributes"]["address"] = "bad" if defect.endswith("malformed") else "0x" + "0" * 40
    elif defect in ("base_missing", "quote_missing"):
        del pool["relationships"]["base_token" if defect.startswith("base") else "quote_token"]
    elif defect == "same_asset":
        pool["relationships"]["quote_token"] = pool["relationships"]["base_token"]
    elif defect == "wrong_type":
        pool["relationships"]["base_token"]["data"]["type"] = "dex"
    elif defect == "wrong_network":
        pool["relationships"]["network"] = {"data": {"type": "network", "id": "wrong"}}
    elif defect == "wrong_resource":
        pool["id"] = "wrong_" + pool["attributes"]["address"]
    else:
        key, value = {
            "wrong_provider": ("provider", "wrong"),
            "wrong_chain": ("chain", "robinhood"),
            "wrong_chain_id": ("chain_id", 4663),
            "fixture": ("is_fixture", True),
        }[defect]
        pool[key] = value

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        assert await provider.discover() == ()
        assert provider.failed == 1


async def test_multiple_pools_deduplicated_and_bounded(settings, handler, now):
    data = payload("bsc_new_pools")
    first = data["data"][0]
    second = json.loads(json.dumps(first))
    second["attributes"]["address"] = "0x" + "d" * 40
    second["id"] = "bsc_" + second["attributes"]["address"]
    data["data"] = [first, first, second, first]

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        assert len(await provider.discover()) == 2
        assert provider.discovered == 2
        assert transport.logical_requests == 2


async def test_conflicting_duplicate_aborts(settings, handler, now):
    data = payload("bsc_new_pools")
    second = json.loads(json.dumps(data["data"][0]))
    second["attributes"]["reserve_in_usd"] = "10"
    data["data"].append(second)

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        with pytest.raises(IdentityError):
            await adapter(transport, settings, now).discover()


async def test_market_identity_binding(settings, handler, now):
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handler)
    ) as transport:
        provider = adapter(transport, settings, now)
        pair = (await provider.discover())[0]
        with pytest.raises(IdentityError):
            await provider.snapshot(pair.model_copy(update={"venue": "other"}))


async def test_case_canonicalization(settings, handler, now):
    data = payload("bsc_new_pools")
    data["data"][0]["attributes"]["address"] = "0x" + "C" * 40
    data["included"][0]["attributes"]["address"] = "0x" + "A" * 40

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        pair = (await adapter(transport, settings, now).discover())[0]
        assert pair.asset_id.endswith("a" * 40) and pair.pair_id.endswith("c" * 40)


@pytest.mark.parametrize("mode", ["missing", "wrong_platform"])
async def test_unsupported_network_no_substitution(settings, mode):
    data = payload("networks")
    if mode == "missing":
        data["data"] = data["data"][:1]
    else:
        data["data"][1]["attributes"]["coingecko_asset_platform_id"] = "other"
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: response(data))
    ) as transport:
        with pytest.raises(UnsupportedNetworkError):
            await NetworkDirectory(transport, settings).resolve(CHAINS["robinhood"])


async def test_network_pages_shared(settings):
    data = payload("networks")
    calls = []

    def handle(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return response(
            {"data": [data["data"][page - 1]], "links": {"next": "ignored" if page == 1 else None}}
        )

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        directory = NetworkDirectory(transport, settings)
        assert await directory.resolve(CHAINS["robinhood"]) == "robinhood"
        assert await directory.resolve(CHAINS["bsc"]) == "bsc"
        assert calls == [1, 2]


async def test_network_scan_bound_is_not_false_absence(settings):
    data = payload("networks")
    data["data"] = data["data"][:1]
    data["links"] = {"next": "ignored"}
    settings = settings.model_copy(update={"geckoterminal_network_pages": 1})
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: response(data))
    ) as transport:
        with pytest.raises(BudgetError):
            await NetworkDirectory(transport, settings).resolve(CHAINS["robinhood"])


@pytest.mark.parametrize(
    "status,error,retries",
    [
        (400, ClientError, 0),
        (401, AuthenticationError, 0),
        (403, AuthenticationError, 0),
        (404, ClientError, 0),
        (302, ClientError, 0),
        (429, RateLimitError, 1),
        (500, UnavailableError, 1),
        (502, UnavailableError, 1),
        (503, UnavailableError, 1),
        (504, UnavailableError, 1),
        (501, UnavailableError, 0),
    ],
)
async def test_http_errors_retries_safe(settings, status, error, retries, caplog):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, text="sensitive upstream body", headers={"Retry-After": "999999"}
        )

    async def sleep(delay):
        waits.append(delay)

    caplog.set_level(logging.DEBUG)
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handle), sleep=sleep
    ) as transport:
        with pytest.raises(error) as caught:
            await transport.get("networks", {})
        assert transport.http_attempts == retries + 1
    assert len(calls) == retries + 1
    assert waits == [5] * retries
    assert "sensitive upstream body" not in str(caught.value) + repr(caught.value) + caplog.text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout])
async def test_connectivity_bounded(settings, error):
    calls, waits = [], []

    def handle(request):
        calls.append(request)
        raise error("sensitive detail")

    async def sleep(delay):
        waits.append(delay)

    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handle), sleep=sleep
    ) as transport:
        with pytest.raises(ConnectivityError) as caught:
            await transport.get("networks", {})
    assert len(calls) == 2 and waits == [2]
    assert "sensitive detail" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        "bad-json",
        "[]",
        '{"errors":[{"detail":"secret"}]}',
        '{"data":NaN}',
        '{"data":1e999999999999999999999999}',
        '{"data":1,"data":2}',
    ],
)
async def test_bad_json_no_retry(settings, body):
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as transport:
        with pytest.raises(ContractError):
            await transport.get("networks", {})
        assert transport.http_attempts == 1


@pytest.mark.parametrize("data", [{}, {"data": None}, {"data": "wrong"}])
async def test_incompatible_schema_no_empty_success(settings, now, data):
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: response(data))
    ) as transport:
        with pytest.raises(ContractError):
            await adapter(transport, settings, now).discover()
        assert transport.http_attempts == 1


async def test_retry_after_dates_and_success(settings, now):
    waits, calls = [], []

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "Wed, 09 Sep 2026 12:01:00 GMT"})
            if len(calls) == 1
            else response({"data": []})
        )

    async def sleep(delay):
        waits.append(delay)

    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handle), sleep=sleep, clock=FixedClock(now)
    ) as transport:
        assert await transport.get("networks", {}) == {"data": []}
        assert waits == [5]
        assert transport.retry_delay("invalid", 0) == 2
        assert transport.retry_delay("0", 0) == 0
        assert transport.retry_delay("Wed, 09 Sep 2026 11:59:00 GMT", 0) == 0


async def test_hard_request_and_attempt_budget(settings, handler):
    settings = settings.model_copy(update={"geckoterminal_max_requests": 1})
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handler)
    ) as transport:
        await transport.get("networks", {})
        with pytest.raises(BudgetError):
            await transport.get("networks", {})
    settings = settings.model_copy(update={"geckoterminal_max_http_attempts": 1})

    async def sleep(delay):
        pass

    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(503)), sleep=sleep
    ) as transport:
        with pytest.raises(BudgetError):
            await transport.get("networks", {})
        assert transport.http_attempts == 1


async def test_runtime_complete_both_chains(settings, handler, now, market_sessions, monkeypatch):
    from src.markets import ingest

    class EngineView:
        async def dispose(self):
            pass

    monkeypatch.setattr(ingest, "connect", lambda url: (EngineView(), market_sessions))
    monkeypatch.setattr(ingest, "SystemClock", lambda: FixedClock(now))

    def factory(settings, **kwargs):
        return GeckoTerminalTransport(settings, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ingest, "GeckoTerminalTransport", factory)
    results = await ingest.run(settings)
    assert [result.safe_text() for result in results] == [
        f"provider=geckoterminal chain={chain} discovered=1 recorded=1 "
        "readable=1 unavailable=0 rejected=0 failed=0 reasons=none"
        for chain in ("robinhood", "bsc")
    ]


async def test_latest_unavailable_does_not_resurrect(
    settings, handler, now, recorder, market_sessions
):
    from src.markets.reader import MarketReader

    reader = MarketReader(market_sessions, clock=FixedClock(now + timedelta(seconds=1)))
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handler)
    ) as transport:
        provider = adapter(transport, settings, now)
        pair = (await provider.discover())[0]
        old = await provider.snapshot(pair)
        await recorder.record(old)
    data = payload("bsc_new_pools")
    data["data"][0]["attributes"]["reserve_in_usd"] = None

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now + timedelta(seconds=1))
        await recorder.record(await provider.snapshot((await provider.discover())[0]))
    assert await reader.latest(pair.asset_id) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("market_chains", "bsc,bsc"),
        ("market_chains", "unknown"),
        ("geckoterminal_pools_per_chain", 21),
        ("geckoterminal_max_requests", 0),
        ("geckoterminal_max_http_attempts", 11),
        ("geckoterminal_max_concurrency", 0),
        ("geckoterminal_max_detail_lookups", 1),
        ("geckoterminal_api_version", "future"),
        ("geckoterminal_base_url", "http://api.geckoterminal.com/api/v2"),
        ("geckoterminal_base_url", "https://secret@api.geckoterminal.com/api/v2"),
        ("geckoterminal_bsc_network_id", "../wrong"),
    ],
)
def test_configuration_bounds(settings, field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=settings.database_url, **{field: value})


def test_chain_limit(settings):
    with pytest.raises(ConfigurationError):
        selected_chains(settings.model_copy(update={"geckoterminal_max_chains": 1}))


def test_cli_summary(monkeypatch, capsys):
    from src.markets import ingest

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test@localhost/test")
    monkeypatch.setattr(
        "sys.argv", ["ingest", "--provider", "geckoterminal", "--chain", "bsc", "--once"]
    )

    async def run(settings):
        assert settings.market_chains == "bsc"
        return (ingest.IngestionSummary("bsc", discovered=3, recorded=3, readable=3),)

    monkeypatch.setattr(ingest, "run", run)
    assert ingest.main() == 0
    assert (
        capsys.readouterr().out.strip()
        == "provider=geckoterminal chain=bsc discovered=3 recorded=3 "
        "readable=3 unavailable=0 rejected=0 failed=0 reasons=none"
    )


@pytest.mark.parametrize("limit", [1, 2])
async def test_transport_concurrency_bound(settings, limit):
    settings = settings.model_copy(update={"geckoterminal_max_concurrency": limit})
    active, peak = 0, 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == limit:
            entered.set()
        await release.wait()
        active -= 1
        return response({"data": []})

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        tasks = [asyncio.create_task(transport.get("networks", {})) for _ in range(3)]
        await entered.wait()
        release.set()
        await asyncio.gather(*tasks)
    assert peak <= limit


@pytest.mark.parametrize("failure", ["unsupported", "outage"])
async def test_runtime_network_failure_scope(
    settings, handler, now, market_sessions, monkeypatch, failure
):
    from src.markets import ingest

    class EngineView:
        async def dispose(self):
            pass

    data = payload("networks")
    data["data"] = data["data"][:1]

    def handle(request):
        if failure == "outage":
            return httpx.Response(503)
        return response(data) if request.url.path.endswith("/networks") else handler(request)

    async def sleep(delay):
        pass

    monkeypatch.setattr(ingest, "connect", lambda url: (EngineView(), market_sessions))
    monkeypatch.setattr(ingest, "SystemClock", lambda: FixedClock(now))

    def factory(settings, **kwargs):
        return GeckoTerminalTransport(
            settings, transport=httpx.MockTransport(handle), sleep=sleep, **kwargs
        )

    monkeypatch.setattr(ingest, "GeckoTerminalTransport", factory)
    first, second = await ingest.run(settings)
    if failure == "unsupported":
        assert first.error == "unsupported_network"
        assert second.recorded == 1 and second.failed == 0
    else:
        assert first.error == "provider_unavailable"
        assert second.error == "pass_aborted" and second.recorded == 0


async def test_failed_rediscovery_cannot_return_cached_observation(settings, handler, now):
    calls = 0

    def handle(request):
        nonlocal calls
        if request.url.path.endswith("new_pools"):
            calls += 1
            if calls > 1:
                return httpx.Response(400)
        return handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        pair = (await provider.discover())[0]
        with pytest.raises(ClientError):
            await provider.discover()
        with pytest.raises(IdentityError):
            await provider.snapshot(pair)


async def test_wrong_configured_mapping_rejects(settings, handler):
    settings = settings.model_copy(update={"geckoterminal_bsc_network_id": "robinhood"})
    async with GeckoTerminalTransport(
        settings, transport=httpx.MockTransport(handler)
    ) as transport:
        with pytest.raises(UnsupportedNetworkError):
            await NetworkDirectory(transport, settings).resolve(CHAINS["bsc"])


def test_no_future_credentials_or_rpc_required(settings, monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "future-secret-not-enabled")
    monkeypatch.setenv("BSC_RPC_HTTP_URL", "not-used")
    result = Settings(
        _env_file=None, database_url=settings.database_url, market_provider="geckoterminal"
    )
    assert "future-secret-not-enabled" not in result.model_dump_json()
    assert "not-used" not in result.model_dump_json()


def test_cli_failure_exit_is_safe(monkeypatch, capsys):
    from src.markets import ingest

    monkeypatch.setenv("DATABASE_URL", "secret-invalid-url")
    monkeypatch.setattr("sys.argv", ["ingest", "--once"])
    assert ingest.main() == 1
    assert "secret-invalid-url" not in capsys.readouterr().out


async def test_pool_identifiers_are_preserved_as_bytes32(settings, handler, now):
    data = payload("bsc_new_pools")
    data["data"][0]["attributes"]["address"] = "0x" + "c" * 64
    data["data"][0]["id"] = "bsc_" + data["data"][0]["attributes"]["address"]

    def handle(request):
        return response(data) if request.url.path.endswith("new_pools") else handler(request)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        provider = adapter(transport, settings, now)
        pair = (await provider.discover())[0]
        assert pair.pool_locator.kind.value == "BYTES32_POOL_ID"
        assert pair.pool_locator.value == "0x" + "c" * 64
        assert provider.failed == 0
