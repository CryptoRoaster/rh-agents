"""One reusable ingestion pass; scheduler holds PostgreSQL ownership for its lifetime."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.clock import Clock
from src.core.config import Settings
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.ingest import IngestionSummary, run
from src.runtime.models import ErrorCode, RuntimeFailure
from src.runtime.rpc import Sleep
from src.runtime.store import RuntimeStore, ownership

WATCHER_LOCK = 46635601
Pass = Callable[[], Awaitable[tuple[IngestionSummary, ...]]]


class MarketWatcher:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        store: RuntimeStore,
        clock: Clock,
        *,
        ingestion: Pass | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings, self.engine, self.store, self.clock = settings, engine, store, clock
        self.ingestion, self.sleep = ingestion, sleep
        self._running = False
        self._passing = False
        self.failures = 0
        self.logical_requests = 0
        self.retries = 0

    async def pass_once(self) -> tuple[IngestionSummary, ...]:
        """Caller must own WATCHER_LOCK (scheduler or deliberate manual runtime pass)."""
        if self._passing:
            raise RuntimeFailure(ErrorCode.OWNERSHIP)
        self._passing = True
        run_id = uuid4()
        started = self.clock.now()
        self.logical_requests = self.retries = 0
        try:
            await self.store.audit(
                "market-watcher",
                "geckoterminal",
                "STARTED",
                run_id,
                {"started_at": started.isoformat(), "state": "STARTING"},
            )
            if self.ingestion:
                results = await self.ingestion()
            else:
                transport = GeckoTerminalTransport(self.settings, clock=self.clock)
                try:
                    results = await run(self.settings, clock=self.clock, transport=transport)
                finally:
                    self.logical_requests = transport.logical_requests
                    self.retries = max(0, transport.http_attempts - transport.logical_requests)
            failed = any(result.failed for result in results)
            self.failures = self.failures + 1 if failed else 0
            completed = self.clock.now()
            payload = {
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
                "state": "DEGRADED" if failed else "HEALTHY",
                "consecutive_failures": self.failures,
                "logical_requests": self.logical_requests,
                "retry_count": self.retries,
                "next_scheduled_at": (
                    completed + timedelta(seconds=self.settings.market_watch_interval_seconds)
                ).isoformat(),
                "chains": [asdict(result) for result in results],
            }
            await self.store.audit("market-watcher", "geckoterminal", "COMPLETED", run_id, payload)
            if not failed:
                await self.store.audit(
                    "market-watcher", "geckoterminal", "SUCCESS", run_id, payload
                )
            return results
        except BaseException as error:
            self.failures += 1
            await self.store.audit(
                "market-watcher",
                "geckoterminal",
                "FAILED",
                run_id,
                {
                    "state": "STOPPED" if isinstance(error, asyncio.CancelledError) else "ERROR",
                    "started_at": started.isoformat(),
                    "completed_at": self.clock.now().isoformat(),
                    "error_category": "CANCELLED"
                    if isinstance(error, asyncio.CancelledError)
                    else "INGESTION_FAILED",
                    "consecutive_failures": self.failures,
                },
            )
            raise
        finally:
            self._passing = False

    async def serve(self) -> None:
        if not self.settings.market_watcher_enabled:
            return
        if self._running:
            raise RuntimeFailure(ErrorCode.OWNERSHIP)
        self._running = True
        try:
            async with ownership(self.engine, WATCHER_LOCK) as acquired:
                if not acquired:
                    raise RuntimeFailure(ErrorCode.OWNERSHIP)
                try:
                    while True:
                        try:
                            await self.pass_once()
                        except Exception:
                            # Safe run error already audited; failure never becomes empty success.
                            pass
                        await self.sleep(self.settings.market_watch_interval_seconds)
                finally:
                    await self.store.audit(
                        "market-watcher", "geckoterminal", "STOPPED", uuid4(), {"state": "STOPPED"}
                    )
        finally:
            self._running = False
