"""Read-only JSON-RPC. Upstream bodies, URLs and exception text never escape."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx

from src.core.config import Settings
from src.markets.geckoterminal.transport import invalid_constant, unique_keys
from src.runtime.models import (
    ChainConfig,
    ErrorCode,
    Head,
    Log,
    RuntimeFailure,
    SubscriptionSpec,
    quantity,
)

Sleep = Callable[[int], Awaitable[None]]


class RedactTransport(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "HTTP transport diagnostic [redacted]"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def decode(body: str | bytes) -> object:
    if len(body) > 2_000_000:
        raise RuntimeFailure(ErrorCode.CONTRACT)
    try:
        result: object = json.loads(
            body,
            parse_float=Decimal,
            parse_constant=invalid_constant,
            object_pairs_hook=unique_keys,
        )
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeFailure(ErrorCode.CONTRACT) from None


def response_result(payload: object, request_id: int) -> object:
    if (
        not isinstance(payload, dict)
        or payload.get("jsonrpc") != "2.0"
        or type(payload.get("id")) is not int
        or payload.get("id") != request_id
    ):
        raise RuntimeFailure(ErrorCode.CONTRACT)
    if "error" in payload:
        raise RuntimeFailure(ErrorCode.RPC_ERROR)
    if "result" not in payload:
        raise RuntimeFailure(ErrorCode.CONTRACT)
    return payload["result"]


class EvmRpcClient:
    def __init__(
        self,
        config: ChainConfig,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.settings = settings
        self.sleep = sleep
        self.verified = False
        self._id = 0
        self.attempts = 0
        # HTTPX INFO includes full URLs; HTTP core DEBUG may include request headers.
        for name in (
            "httpx",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
        ):
            logger = logging.getLogger(name)
            if not any(isinstance(item, RedactTransport) for item in logger.filters):
                logger.addFilter(RedactTransport())
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.evm_timeout_seconds, connect=5),
            headers={"User-Agent": "rh-agents/0.3.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, params: list[object]) -> object:
        if not self.config.http_url.get_secret_value():
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        # Private implementation only; public interface has four read methods.
        if method not in {"eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs"}:
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        if method != "eth_chainId" and not self.verified:
            raise RuntimeFailure(ErrorCode.CHAIN_ID_MISMATCH)
        self._id += 1
        request_id = self._id
        try:
            async with asyncio.timeout(self.settings.evm_timeout_seconds):
                for attempt in range(self.settings.evm_retries + 1):
                    self.attempts += 1
                    delay = min(
                        self.settings.evm_retry_delay_seconds * 2**attempt,
                        self.settings.evm_max_retry_delay_seconds,
                    )
                    code = ErrorCode.UNAVAILABLE
                    try:
                        async with self._client.stream(
                            "POST",
                            self.config.http_url.get_secret_value(),
                            json={
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": method,
                                "params": params,
                            },
                        ) as response:
                            status = response.status_code
                            if status == 200:
                                body = bytearray()
                                async for chunk in response.aiter_bytes():
                                    body.extend(chunk)
                                    if len(body) > 2_000_000:
                                        raise RuntimeFailure(ErrorCode.CONTRACT)
                                return response_result(decode(bytes(body)), request_id)
                            if status in (401, 403):
                                raise RuntimeFailure(ErrorCode.AUTHENTICATION)
                            if status == 429:
                                code = ErrorCode.RATE_LIMITED
                            elif 500 <= status <= 599 and status not in (500, 502, 503, 504):
                                raise RuntimeFailure(ErrorCode.UNAVAILABLE)
                            elif status not in (500, 502, 503, 504):
                                raise RuntimeFailure(ErrorCode.CLIENT)
                            header = response.headers.get("Retry-After", "")
                            if header.isascii() and header.isdigit() and len(header) <= 10:
                                delay = min(int(header), self.settings.evm_max_retry_delay_seconds)
                    except httpx.TimeoutException:
                        code = ErrorCode.TIMEOUT
                    except (httpx.NetworkError, httpx.RemoteProtocolError):
                        code = ErrorCode.CONNECTIVITY
                    except httpx.HTTPError:
                        raise RuntimeFailure(ErrorCode.UNAVAILABLE) from None
                    if attempt == self.settings.evm_retries:
                        raise RuntimeFailure(code) from None
                    await self.sleep(delay)
        except TimeoutError:
            raise RuntimeFailure(ErrorCode.TIMEOUT) from None
        raise RuntimeFailure(ErrorCode.UNAVAILABLE)

    async def verify_chain(self) -> int:
        self.verified = False
        chain_id = quantity(await self._request("eth_chainId", []))
        if chain_id != self.config.chain_id:
            raise RuntimeFailure(ErrorCode.CHAIN_ID_MISMATCH)
        self.verified = True
        return chain_id

    async def block_number(self) -> int:
        return quantity(await self._request("eth_blockNumber", []))

    async def block(self, number: int) -> Head:
        if type(number) is not int or number < 0:
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        head = Head.from_rpc(await self._request("eth_getBlockByNumber", [hex(number), False]))
        if head.number != number:
            raise RuntimeFailure(ErrorCode.CONTRACT)
        return head

    async def logs(self, spec: SubscriptionSpec, start: int, end: int) -> tuple[Log, ...]:
        if (
            spec.chain != self.config.chain
            or start < 0
            or end < start
            or end - start + 1 > self.settings.evm_recovery_chunk_size
        ):
            raise RuntimeFailure(ErrorCode.CONFIGURATION)
        payload = await self._request(
            "eth_getLogs", [dict(spec.rpc_filter(), fromBlock=hex(start), toBlock=hex(end))]
        )
        if not isinstance(payload, list) or len(payload) > 5000:
            raise RuntimeFailure(ErrorCode.CONTRACT)
        logs = tuple(Log.from_rpc(item) for item in payload)
        if any(not item.matches(spec) or not start <= item.block_number <= end for item in logs):
            raise RuntimeFailure(ErrorCode.CONTRACT)
        return tuple(
            sorted(
                logs, key=lambda item: (item.block_number, item.transaction_index, item.log_index)
            )
        )
