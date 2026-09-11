import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from src.api.runtime import chain_health, watcher_health
from src.core.clock import FixedClock
from src.core.config import Settings
from src.data.tables import EvmLogRow, RuntimeAuditRow
from src.markets.ingest import IngestionSummary
from src.runtime.models import ErrorCode, Head, Log, RuntimeFailure, SubscriptionSpec
from src.runtime.recovery import Recovery
from src.runtime.store import ownership
from src.runtime.watcher import MarketWatcher
from src.runtime.websocket import EvmWebSocketRuntime


def block(number, fork=0):
    return Head(
        number=number,
        hash="0x" + f"{number + 1 + fork:064x}",
        parent_hash="0x" + f"{number + fork:064x}",
        timestamp=1000 + number,
    )


class Rpc:
    verified = True

    def __init__(self, chain_id):
        self.chain_id = chain_id
        self.height = 102
        self.fail = None
        self.calls = []
        self.fork_at = 10000

    async def verify_chain(self):
        return self.chain_id

    async def block_number(self):
        return self.height

    async def block(self, number):
        if number == self.fail:
            raise RuntimeFailure(ErrorCode.UNAVAILABLE)
        result = block(number, 10000 if number >= self.fork_at else 0)
        if number == self.fork_at:
            result = result.model_copy(update={"parent_hash": block(number - 1).hash})
        return result

    async def logs(self, spec, start, end):
        self.calls.append((start, end))
        return tuple(
            [
                Log(
                    block_number=n,
                    block_hash=(await self.block(n)).hash,
                    transaction_hash="0x" + f"{n + 100:064x}",
                    transaction_index=0,
                    log_index=0,
                    address=spec.addresses[0],
                    topics=spec.topics,
                    data="0x",
                )
                for n in range(start, end + 1)
            ]
        )


def spec(chain):
    return SubscriptionSpec(
        chain=chain, addresses=("0x" + "11" * 20,), topics=("0x" + "22" * 32,), decoder="fixture"
    )


async def test_recovery_restart_gap_and_dedupe(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(
        chain_config, settings, rpc, store, FixedClock(now), uuid4(), (spec(chain_config.chain),)
    )
    await recovery.accept()
    assert (await store.cursor(chain_config.chain)).last_processed_block == 100
    rpc.height = 110
    settings.evm_recovery_chunk_size = 3
    recovery = Recovery(
        chain_config, settings, rpc, store, FixedClock(now), uuid4(), (spec(chain_config.chain),)
    )
    await recovery.accept()
    assert rpc.calls == [(100, 100), (101, 103), (104, 106), (107, 108)]
    assert (await store.cursor(chain_config.chain)).last_processed_block == 108
    await recovery.accept()
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EvmLogRow)) == 9
    assert recovery.gap is None


async def test_failed_range_never_advances(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    await recovery.accept()
    rpc.height = 110
    rpc.fail = 105
    with pytest.raises(RuntimeFailure):
        await recovery.accept()
    assert (await store.cursor(chain_config.chain)).last_processed_block == 100
    assert recovery.gap == (101, 108)
    rpc.fail = None
    await recovery.accept()
    assert (await store.cursor(chain_config.chain)).last_processed_block == 108
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EvmLogRow)) == 0


async def test_reorg_rewinds_and_replays(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(
        chain_config, settings, rpc, store, FixedClock(now), uuid4(), (spec(chain_config.chain),)
    )
    await recovery.accept()
    rpc.height = 106
    await recovery.accept()
    rpc.fork_at = 103
    await recovery.accept()
    assert recovery.reorg
    assert (await store.cursor(chain_config.chain)).last_processed_block == 104
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EvmLogRow)) == 7
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RuntimeAuditRow)
                .where(RuntimeAuditRow.kind == "REWIND")
            )
            == 1
        )


async def test_deep_reorg_stops(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    await recovery.accept()
    rpc.fork_at = 1
    with pytest.raises(RuntimeFailure) as caught:
        await recovery.accept()
    assert caught.value.code == ErrorCode.DEEP_REORG
    assert (await store.cursor(chain_config.chain)).last_processed_block == 100


async def test_log_conflict_and_original_timestamp(runtime_db, now):
    _, store = runtime_db
    log = Log(
        block_number=1,
        block_hash=block(1).hash,
        transaction_hash=block(2).hash,
        transaction_index=0,
        log_index=0,
        address="0x" + "11" * 20,
        topics=(),
        data="0x",
    )
    async with store.sessions.begin() as session:
        await store.save_log(session, "bsc", log, now, uuid4(), ("test",))
    store.clock = FixedClock(now + timedelta(seconds=30))
    async with store.sessions.begin() as session:
        await store.save_log(session, "bsc", log, now, uuid4(), ("test",))
    async with store.sessions() as session:
        row = await session.scalar(select(EvmLogRow))
        assert row.recorded_at.replace(tzinfo=now.tzinfo) == now
    with pytest.raises(RuntimeFailure):
        async with store.sessions.begin() as session:
            await store.save_log(
                session, "bsc", log.model_copy(update={"data": "0x11"}), now, uuid4(), ("test",)
            )


async def test_pg_ownership_release(runtime_db):
    engine, _ = runtime_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL ownership")
    async with ownership(engine, 912345) as first:
        assert first
        async with ownership(engine, 912345) as second:
            assert not second
    with pytest.raises(ValueError):
        async with ownership(engine, 912345) as acquired:
            assert acquired
            raise ValueError("test")
    async with ownership(engine, 912345) as acquired:
        assert acquired


async def test_append_only(runtime_db, now):
    engine, store = runtime_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL trigger")
    await store.audit("bsc", "evm_rpc", "TEST", uuid4(), {})
    for statement in (
        "DELETE FROM runtime_audit",
        "UPDATE runtime_audit SET kind='X'",
        "TRUNCATE runtime_audit",
    ):
        with pytest.raises(SQLAlchemyError):
            async with engine.begin() as connection:
                await connection.execute(text(statement))


async def test_watcher_results_and_overlap(runtime_db, settings, now):
    engine, store = runtime_db
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ingestion():
        entered.set()
        await release.wait()
        return (
            IngestionSummary("bsc", discovered=2, recorded=2, readable=1, unavailable=1),
            IngestionSummary(
                "robinhood", discovered=1, rejected=1, failed=1, reasons={"provider_identity": 1}
            ),
        )

    watcher = MarketWatcher(settings, engine, store, FixedClock(now), ingestion=ingestion)
    task = asyncio.create_task(watcher.pass_once())
    await entered.wait()
    with pytest.raises(RuntimeFailure):
        await watcher.pass_once()
    release.set()
    results = await task
    assert results[0].failed == 0
    row = await store.latest("market-watcher", "COMPLETED")
    assert row.payload["state"] == "DEGRADED"
    assert row.payload["chains"][1]["reasons"] == {"provider_identity": 1}
    assert watcher.failures == 1


async def test_watcher_disabled(runtime_db, settings, now):
    engine, store = runtime_db

    async def fail():
        raise AssertionError("must not run")

    await MarketWatcher(settings, engine, store, FixedClock(now), ingestion=fail).serve()
    assert await store.latest("market-watcher") is None


async def test_health_disabled_and_stale(runtime_db, settings, now):
    _, store = runtime_db
    result = await chain_health(settings, store)
    assert all(row["error_category"] == "NOT_CONFIGURED" for row in result)
    settings.evm_runtime_enabled = True
    settings.bsc_chain_enabled = True
    await store.audit(
        "bsc", "evm_ws", "HEALTH", uuid4(), {"state": "HEALTHY", "wss_connected": True}
    )
    store.clock = FixedClock(now + timedelta(seconds=90))
    result = await chain_health(settings, store)
    assert result[1]["state"] == "STALE" and not result[1]["wss_connected"]
    assert (await watcher_health(settings, store))["state"] == "STOPPED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"market_watcher_enabled": True},
        {"evm_runtime_enabled": True, "bsc_chain_enabled": True},
        {"rh_chain_id": 1},
        {"bsc_chain_id": 1},
        {"market_watch_interval_seconds": 1},
        {"evm_queue_size": 0},
    ],
)
def test_invalid_settings(overrides):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, database_url="postgresql+asyncpg://test@localhost/test", **overrides
        )


async def test_bounded_queue(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    settings.evm_queue_size = 1
    recovery = Recovery(
        chain_config, settings, Rpc(chain_config.chain_id), store, FixedClock(now), uuid4()
    )
    runtime = ObservedRuntime(chain_config, settings, recovery, store, FixedClock(now), uuid4())
    runtime.enqueue(block(1))
    with pytest.raises(RuntimeFailure) as caught:
        runtime.enqueue(block(2))
    assert caught.value.code == ErrorCode.OVERFLOW
    assert runtime.queue.qsize() == 1


class FakeSocket:
    def __init__(self, chain_id, rpc, processed):
        self.chain_id, self.rpc, self.processed = chain_id, rpc, processed
        self.responses = asyncio.Queue(maxsize=10)
        self.sent = []

    async def send(self, message):
        request = json.loads(message)
        self.sent.append(request["method"])
        result = hex(self.chain_id) if request["method"] == "eth_chainId" else "subscription-1"
        await self.responses.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )
        if request["method"] == "eth_subscribe":
            head = await self.rpc.block(self.rpc.height)
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "eth_subscription",
                        "params": {
                            "subscription": result,
                            "result": {
                                "number": hex(head.number),
                                "hash": head.hash,
                                "parentHash": head.parent_hash,
                                "timestamp": hex(head.timestamp),
                            },
                        },
                    }
                )
            )

    async def recv(self):
        return await self.responses.get()


async def test_ws_reconnect_shutdown_and_gap(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    sockets = []
    processed = asyncio.Event()

    @asynccontextmanager
    async def connector():
        socket = FakeSocket(chain_config.chain_id, rpc, processed)
        sockets.append(socket)
        yield socket

    runtime = ObservedRuntime(
        chain_config, settings, recovery, store, FixedClock(now), uuid4(), connector=connector
    )
    task = asyncio.create_task(runtime.serve())
    try:
        async with asyncio.timeout(5):
            await runtime.processed.wait()
        assert (await store.cursor(chain_config.chain)).last_processed_block == 100
        assert sockets[0].sent == ["eth_chainId", "eth_subscribe"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not runtime.connected
    rpc.height = 110
    runtime = ObservedRuntime(
        chain_config, settings, recovery, store, FixedClock(now), uuid4(), connector=connector
    )
    task = asyncio.create_task(runtime.serve())
    try:
        async with asyncio.timeout(5):
            await runtime.processed.wait()
        assert (await store.cursor(chain_config.chain)).last_processed_block == 108
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_reconnect_bounded(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    calls = []
    waits = []

    @asynccontextmanager
    async def connector():
        calls.append(1)
        raise OSError("secret-token")
        yield

    async def sleep(seconds):
        waits.append(seconds)

    runtime = ObservedRuntime(
        chain_config,
        settings,
        recovery,
        store,
        FixedClock(now),
        uuid4(),
        connector=connector,
        sleep=sleep,
    )
    with pytest.raises(RuntimeFailure) as caught:
        await runtime.serve()
    assert len(calls) == settings.evm_reconnect_attempts + 1
    assert len(waits) == settings.evm_reconnect_attempts
    assert "secret-token" not in str(caught.value)


async def test_scheduled_ownership_shutdown(runtime_db, settings, now):
    engine, store = runtime_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL scheduler ownership")
    settings.market_watcher_enabled = True
    entered = asyncio.Event()
    sleep_entered = asyncio.Event()

    async def ingestion():
        entered.set()
        return (
            IngestionSummary("bsc", discovered=1, recorded=1, readable=1),
            IngestionSummary("robinhood", discovered=1, recorded=1, readable=1),
        )

    async def sleep(_):
        sleep_entered.set()
        await asyncio.Event().wait()

    first = MarketWatcher(
        settings, engine, store, FixedClock(now), ingestion=ingestion, sleep=sleep
    )
    second = MarketWatcher(
        settings, engine, store, FixedClock(now), ingestion=ingestion, sleep=sleep
    )
    task = asyncio.create_task(first.serve())
    try:
        await entered.wait()
        await sleep_entered.wait()
        with pytest.raises(RuntimeFailure):
            await first.serve()
        with pytest.raises(RuntimeFailure):
            await second.serve()
        assert (await store.latest("market-watcher", "SUCCESS")).payload["chains"][0][
            "recorded"
        ] == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await store.latest("market-watcher")).kind == "STOPPED"
    async with ownership(engine, 46635601) as acquired:
        assert acquired


async def test_watcher_failure_releases_pass(runtime_db, settings, now):
    engine, store = runtime_db

    async def fail():
        raise OSError("secret-token")

    watcher = MarketWatcher(settings, engine, store, FixedClock(now), ingestion=fail)
    for _ in range(2):
        with pytest.raises(OSError):
            await watcher.pass_once()
    row = await store.latest("market-watcher")
    assert row.kind == "FAILED"
    assert "secret-token" not in str(row.payload)
    assert watcher.failures == 2


async def test_runtime_api_no_secrets(runtime_db, settings, now, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    import httpx

    from src.api.main import create_app
    from src.api.runtime import runtime_store

    _, store = runtime_db
    app = create_app(settings)
    app.dependency_overrides[runtime_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/api/runtime", "/api/runtime/chains", "/api/runtime/market-watcher"):
            response = await client.get(path)
            assert response.status_code == 200
            assert "secret-token" not in response.text
            assert "rpc_http_url" not in response.text


async def test_shutdown_during_reconnect(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    recovery = Recovery(
        chain_config, settings, Rpc(chain_config.chain_id), store, FixedClock(now), uuid4()
    )
    entered = asyncio.Event()
    attempts = []

    @asynccontextmanager
    async def connector():
        attempts.append(1)
        raise OSError()
        yield

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    runtime = ObservedRuntime(
        chain_config,
        settings,
        recovery,
        store,
        FixedClock(now),
        uuid4(),
        connector=connector,
        sleep=sleep,
    )
    task = asyncio.create_task(runtime.serve())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1
    assert (await store.latest(chain_config.chain, "HEALTH")).payload["state"] == "STOPPED"


async def test_actual_disconnect_resubscribe(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    sockets = []

    class DisconnectSocket(FakeSocket):
        async def recv(self):
            if self.responses.empty() and len(sockets) == 1:
                await runtime.processed.wait()
                rpc.height = 110
                raise OSError("disconnected")
            return await super().recv()

    @asynccontextmanager
    async def connector():
        socket = DisconnectSocket(chain_config.chain_id, rpc, None)
        sockets.append(socket)
        yield socket

    runtime = ObservedRuntime(
        chain_config, settings, recovery, store, FixedClock(now), uuid4(), connector=connector
    )
    task = asyncio.create_task(runtime.serve())
    try:
        async with asyncio.timeout(5):
            await runtime.second_processed.wait()
        assert sockets[1].sent == ["eth_chainId", "eth_subscribe"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cursor_race_rejects(runtime_db, chain_config, settings, now):
    engine, store = runtime_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking")
    rpc = Rpc(chain_config.chain_id)
    first = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    await first.accept()
    rpc.height = 106
    second = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    results = await asyncio.gather(first.accept(), second.accept(), return_exceptions=True)
    assert any(result is None for result in results)
    assert all(result is None or isinstance(result, RuntimeFailure) for result in results)
    assert (await store.cursor(chain_config.chain)).last_processed_block == 104


class ObservedRuntime(EvmWebSocketRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.processed = asyncio.Event()
        self.second_processed = asyncio.Event()
        self.healthy_count = 0

    async def health(self, state, code=None):
        await super().health(state, code)
        if state == "HEALTHY":
            self.healthy_count += 1
            self.processed.set()
            if self.healthy_count >= 2:
                self.second_processed.set()


async def test_mocked_watcher_complete_both_chains(runtime_db, settings, now, monkeypatch):
    from pathlib import Path

    import httpx

    from src.data.tables import MarketObservationRow
    from src.markets import ingest
    from src.markets.geckoterminal.transport import GeckoTerminalTransport
    from src.markets.reader import MarketReader

    engine, store = runtime_db
    async with engine.begin() as connection:
        await connection.run_sync(MarketObservationRow.__table__.create)
    fixture_path = Path(__file__).parents[1] / "markets/fixtures/geckoterminal"

    def handler(request):
        name = (
            "networks"
            if request.url.path.endswith("/networks")
            else request.url.path.split("/")[-2] + "_new_pools"
        )
        return httpx.Response(200, text=(fixture_path / (name + ".json")).read_text())

    class EngineView:
        async def dispose(self):
            pass

    monkeypatch.setattr(ingest, "connect", lambda url: (EngineView(), store.sessions))

    def transport(settings, **kwargs):
        return GeckoTerminalTransport(settings, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("src.runtime.watcher.GeckoTerminalTransport", transport)
    watcher = MarketWatcher(settings, engine, store, FixedClock(now))
    results = await watcher.pass_once()
    assert [(row.chain, row.recorded, row.readable, row.failed) for row in results] == [
        ("robinhood", 1, 1, 0),
        ("bsc", 1, 1, 0),
    ]
    assert watcher.logical_requests == 3 and watcher.retries == 0
    reader = MarketReader(store.sessions, clock=FixedClock(now))
    assert len(await reader.markets()) == 2
    assert (await store.latest("market-watcher", "SUCCESS")).payload["state"] == "HEALTHY"


async def test_disabled_native_supervisor(runtime_db, settings, monkeypatch):
    from src.runtime import main

    engine, store = runtime_db
    monkeypatch.setattr(main, "connect", lambda url: (engine, store.sessions))
    assert await main.serve(settings) is None


async def test_native_supervisor_chain_lifecycle(runtime_db, settings, chain_config, monkeypatch):
    from src.runtime import main

    engine, store = runtime_db
    if engine.dialect.name != "postgresql":
        pytest.skip("Native PostgreSQL ownership")
    settings.evm_runtime_enabled = True
    monkeypatch.setattr(main, "chain_configs", lambda _: (chain_config,))
    monkeypatch.setattr(main, "connect", lambda url: (engine, store.sessions))
    called = []

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def serve(self):
            called.append("served")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            called.append("closed")

    monkeypatch.setattr(main, "EvmWebSocketRuntime", Worker)
    monkeypatch.setattr(main, "EvmRpcClient", Client)
    await main.serve(settings)
    assert called == ["served", "closed"]


async def test_shallow_head_reorg_does_not_repeat(runtime_db, chain_config, settings, now):
    _, store = runtime_db
    rpc = Rpc(chain_config.chain_id)
    recovery = Recovery(chain_config, settings, rpc, store, FixedClock(now), uuid4())
    await recovery.accept()
    rpc.fork_at = 102
    await recovery.accept()
    assert recovery.reorg
    recovery.reorg = False
    await recovery.accept()
    assert not recovery.reorg
    assert (await store.cursor(chain_config.chain)).last_processed_block == 100
