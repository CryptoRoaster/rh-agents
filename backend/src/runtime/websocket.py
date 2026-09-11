"""Bounded subscriptions and recovery wakeups, with explicit reconnect exhaustion."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from websockets.asyncio.client import connect

from src.core.clock import Clock
from src.core.config import Settings
from src.runtime.models import (
    ChainConfig,
    ErrorCode,
    Head,
    Log,
    RuntimeFailure,
    SubscriptionSpec,
    quantity,
)
from src.runtime.recovery import Recovery
from src.runtime.rpc import Sleep, decode, response_result
from src.runtime.store import RuntimeStore


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connector = Callable[[], AbstractAsyncContextManager[Socket]]


def native_connector(config: ChainConfig, settings: Settings) -> Connector:
    @asynccontextmanager
    async def open_socket() -> AsyncIterator[Socket]:
        if not config.ws_url.get_secret_value():
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        logger = logging.Logger("rh-agents.private-websocket", level=logging.CRITICAL + 1)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        async with connect(
            config.ws_url.get_secret_value(),
            proxy=None,
            open_timeout=settings.evm_timeout_seconds,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=20,
            max_size=2_000_000,
            max_queue=settings.evm_queue_size,
            logger=logger,
            user_agent_header="rh-agents/0.3.0",
        ) as socket:
            yield socket

    return open_socket


def failure_code(error: BaseException) -> ErrorCode:
    if isinstance(error, RuntimeFailure):
        return error.code
    if isinstance(error, BaseExceptionGroup):
        return failure_code(error.exceptions[0])
    if isinstance(error, TimeoutError):
        return ErrorCode.TIMEOUT
    return ErrorCode.CONNECTIVITY


class EvmWebSocketRuntime:
    def __init__(
        self,
        config: ChainConfig,
        settings: Settings,
        recovery: Recovery,
        store: RuntimeStore,
        clock: Clock,
        session_id: UUID,
        *,
        connector: Connector | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config, self.settings, self.recovery = config, settings, recovery
        self.store, self.clock, self.session_id = store, clock, session_id
        self.connector = connector or native_connector(config, settings)
        self.sleep = sleep
        self.connected = False
        self.verified = False
        self.errors = 0
        self.started_at = clock.now()
        self.last_message_at: datetime | None = None
        self.first_message_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.disconnected_at: datetime | None = None
        self.reconnected_at: datetime | None = None
        self.queue: asyncio.Queue[Head | None] = asyncio.Queue(maxsize=settings.evm_queue_size)

    async def health(self, state: str, code: ErrorCode | None = None) -> None:
        cursor = await self.store.cursor(self.config.chain)
        await self.store.audit(
            self.config.chain,
            "evm_ws",
            "HEALTH",
            self.session_id,
            {
                "state": state,
                "configured": True,
                "http_reachable": self.recovery.rpc.verified,
                "wss_connected": self.connected,
                "chain_id_verified": self.verified,
                "expected_chain_id": self.config.chain_id,
                "started_at": self.started_at.isoformat(),
                "last_message_at": self.last_message_at.isoformat()
                if self.last_message_at
                else None,
                "first_message_at": self.first_message_at.isoformat()
                if self.first_message_at
                else None,
                "connected_at": self.connected_at.isoformat() if self.connected_at else None,
                "disconnected_at": self.disconnected_at.isoformat()
                if self.disconnected_at
                else None,
                "reconnected_at": self.reconnected_at.isoformat() if self.reconnected_at else None,
                "latest_head": cursor.last_seen_head if cursor else None,
                "last_processed_safe_block": cursor.last_processed_block if cursor else None,
                "gap_detected": self.recovery.gap is not None,
                "gap_start": self.recovery.gap[0] if self.recovery.gap else None,
                "gap_end": self.recovery.gap[1] if self.recovery.gap else None,
                "consecutive_errors": self.errors,
                "error_category": code.value if code else None,
            },
        )

    def enqueue(self, head: Head | None) -> None:
        try:
            self.queue.put_nowait(head)
        except asyncio.QueueFull:
            raise RuntimeFailure(ErrorCode.OVERFLOW) from None

    async def session(self, socket: Socket) -> None:
        pending: list[object] = []

        async def exchange(request_id: int, method: str, params: list[object]) -> object:
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            )
            async with asyncio.timeout(self.settings.evm_timeout_seconds):
                while True:
                    payload = decode(await socket.recv())
                    if isinstance(payload, dict) and payload.get("method") == "eth_subscription":
                        if len(pending) >= self.settings.evm_queue_size:
                            raise RuntimeFailure(ErrorCode.OVERFLOW)
                        pending.append(payload)
                    else:
                        return response_result(payload, request_id)

        self.verified = False
        await self.recovery.rpc.verify_chain()
        if quantity(await exchange(1, "eth_chainId", [])) != self.config.chain_id:
            raise RuntimeFailure(ErrorCode.CHAIN_ID_MISMATCH)
        self.verified = True
        subscriptions: dict[str, SubscriptionSpec | None] = {}
        for index, spec in enumerate((None, *self.recovery.specs), 2):
            params: list[object] = ["newHeads"] if spec is None else ["logs", spec.rpc_filter()]
            identity = await exchange(index, "eth_subscribe", params)
            if (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 200
                or identity in subscriptions
            ):
                raise RuntimeFailure(ErrorCode.CONTRACT)
            subscriptions[identity] = spec
        await (
            self.recovery.accept()
        )  # Reconnect always backfills before accepting fresh notifications.
        self.connected = True
        if self.connected_at is not None:
            self.reconnected_at = self.clock.now()
        self.connected_at = self.clock.now()
        await self.health("STARTING")

        def notification(payload: object) -> None:
            if (
                not isinstance(payload, dict)
                or payload.get("jsonrpc") != "2.0"
                or payload.get("method") != "eth_subscription"
            ):
                raise RuntimeFailure(ErrorCode.CONTRACT)
            params = payload.get("params")
            if (
                not isinstance(params, dict)
                or not isinstance(params.get("subscription"), str)
                or params.get("subscription") not in subscriptions
            ):
                raise RuntimeFailure(ErrorCode.CONTRACT)
            spec = subscriptions[params["subscription"]]
            if spec is None:
                self.enqueue(Head.from_rpc(params.get("result")))
            else:
                result = params.get("result")
                if isinstance(result, dict) and result.get("removed") is True:
                    raise RuntimeFailure(ErrorCode.REORG)
                log = Log.from_rpc(result)
                if not log.matches(spec):
                    raise RuntimeFailure(ErrorCode.CONTRACT)
                self.enqueue(None)  # Confirmed HTTP backfill persists logs, not arrival order.

        async def receive() -> None:
            for payload in pending:
                notification(payload)
            while True:
                async with asyncio.timeout(self.settings.evm_stale_seconds):
                    payload = decode(await socket.recv())
                notification(payload)

        async def process() -> None:
            while True:
                head = await self.queue.get()
                try:
                    await self.recovery.accept(head)
                    self.last_message_at = self.clock.now()
                    if self.first_message_at is None:
                        self.first_message_at = self.last_message_at
                    self.errors = 0
                    await self.health(
                        "DEGRADED" if self.recovery.reorg else "HEALTHY",
                        ErrorCode.REORG if self.recovery.reorg else None,
                    )
                    self.recovery.reorg = False
                finally:
                    self.queue.task_done()

        async with asyncio.TaskGroup() as group:
            group.create_task(receive())
            group.create_task(process())

    async def serve(self) -> None:
        try:
            for attempt in range(self.settings.evm_reconnect_attempts + 1):
                try:
                    async with self.connector() as socket:
                        await self.session(socket)
                except Exception as error:
                    code = failure_code(error)
                    self.connected = self.verified = False
                    self.disconnected_at = self.clock.now()
                    self.errors += 1
                    while not self.queue.empty():
                        self.queue.get_nowait()
                        self.queue.task_done()
                    terminal = code in (
                        ErrorCode.CHAIN_ID_MISMATCH,
                        ErrorCode.CONTRACT,
                        ErrorCode.DEEP_REORG,
                        ErrorCode.CONFLICT,
                    )
                    exhausted = attempt == self.settings.evm_reconnect_attempts
                    await self.health("ERROR" if terminal or exhausted else "DEGRADED", code)
                    if terminal or exhausted:
                        raise RuntimeFailure(code) from None
                    await self.sleep(
                        min(
                            self.settings.evm_retry_delay_seconds * 2**attempt,
                            self.settings.evm_max_retry_delay_seconds,
                        )
                    )
        except asyncio.CancelledError:
            self.connected = False
            await self.health("STOPPED", ErrorCode.STOPPED)
            raise
