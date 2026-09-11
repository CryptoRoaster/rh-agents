"""Native data runtime process: python -m src.runtime.main. No execution work."""

import asyncio
import signal
from uuid import uuid4

from src.core.clock import SystemClock
from src.core.config import Settings
from src.data.database import connect
from src.runtime.models import ChainConfig, ErrorCode, RuntimeFailure, chain_configs
from src.runtime.recovery import Recovery
from src.runtime.rpc import EvmRpcClient
from src.runtime.store import RuntimeStore, ownership
from src.runtime.watcher import MarketWatcher
from src.runtime.websocket import EvmWebSocketRuntime


async def serve(settings: Settings) -> None:
    engine, sessions = connect(settings.database_url)
    clock = SystemClock()
    store = RuntimeStore(sessions, clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def chain(config: ChainConfig) -> None:
        async with ownership(engine, 46635600 + config.chain_id) as acquired:
            if not acquired:
                raise RuntimeFailure(ErrorCode.OWNERSHIP)
            rpc = EvmRpcClient(config, settings)
            session_id = uuid4()
            recovery = Recovery(config, settings, rpc, store, clock, session_id)
            try:
                await EvmWebSocketRuntime(
                    config, settings, recovery, store, clock, session_id
                ).serve()
            finally:
                await rpc.close()

    tasks = []
    try:
        if settings.market_watcher_enabled:
            tasks.append(asyncio.create_task(MarketWatcher(settings, engine, store, clock).serve()))
        tasks.extend(asyncio.create_task(chain(config)) for config in chain_configs(settings))
        if not tasks:
            return
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> int:
    try:
        asyncio.run(serve(Settings()))
    except Exception:
        print("runtime=ERROR category=RUNTIME_STOPPED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
