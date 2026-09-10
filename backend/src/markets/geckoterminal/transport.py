"""Bounded public API access. One transport/budget belongs to one ingestion pass."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal, DecimalException
from email.utils import parsedate_to_datetime
from types import TracebackType

import httpx

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.markets.geckoterminal.errors import (
    AuthenticationError,
    BudgetError,
    ClientError,
    ConnectivityError,
    ContractError,
    ProviderError,
    RateLimitError,
    UnavailableError,
)

Sleep = Callable[[int], Awaitable[None]]


def invalid_constant(value: str) -> object:
    raise ValueError("Nonfinite JSON number")


def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class GeckoTerminalTransport:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._sleep = sleep
        self._retries = settings.geckoterminal_retries
        self._delay = settings.geckoterminal_retry_delay_seconds
        self._delay_cap = settings.geckoterminal_max_retry_after_seconds
        self._timeout = settings.geckoterminal_total_timeout_seconds
        self._max_requests = settings.geckoterminal_max_requests
        self._max_attempts = settings.geckoterminal_max_http_attempts
        self.logical_requests = 0
        self.http_attempts = 0
        self._slots = asyncio.Semaphore(settings.geckoterminal_max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=settings.geckoterminal_base_url + "/",
            headers={
                "Accept": f"application/json;version={settings.geckoterminal_api_version}",
                "User-Agent": "rh-agents/0.1.0",
            },
            timeout=httpx.Timeout(
                settings.geckoterminal_read_timeout_seconds,
                connect=settings.geckoterminal_connect_timeout_seconds,
            ),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "GeckoTerminalTransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    def retry_delay(self, header: str | None, attempt: int) -> int:
        fallback = min(self._delay * (1 << attempt), self._delay_cap)
        if header is None:
            return fallback
        try:
            if header.isascii() and header.isdigit() and len(header) <= 10:
                return min(int(header), self._delay_cap)
            date = parsedate_to_datetime(header)
            if date.utcoffset() is None:
                return fallback
            seconds = (date - self._clock.now()).total_seconds()
            return max(0, min(int(seconds) + (seconds > int(seconds)), self._delay_cap))
        except (ValueError, TypeError, OverflowError):
            return fallback

    async def get(self, path: str, params: dict[str, str | int]) -> object:
        if self.logical_requests >= self._max_requests:
            raise BudgetError()
        self.logical_requests += 1
        try:
            async with asyncio.timeout(self._timeout), self._slots:
                return await self._get(path, params)
        except TimeoutError:
            raise ConnectivityError() from None

    async def _get(self, path: str, params: dict[str, str | int]) -> object:
        for attempt in range(self._retries + 1):
            if self.http_attempts >= self._max_attempts:
                raise BudgetError()
            self.http_attempts += 1
            delay = self.retry_delay(None, attempt)
            failure: ProviderError
            try:
                async with self._client.stream("GET", path, params=params) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise AuthenticationError()
                    if status == 429:
                        failure = RateLimitError()
                    elif status in (500, 502, 503, 504):
                        failure = UnavailableError()
                    elif 500 <= status <= 599:
                        raise UnavailableError()
                    elif status != 200:
                        raise ClientError()
                    else:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2_000_000:
                                raise ContractError()
                        try:
                            payload: object = json.loads(
                                body,
                                parse_float=Decimal,
                                parse_constant=invalid_constant,
                                object_pairs_hook=unique_keys,
                            )
                        except (ValueError, UnicodeError, RecursionError, DecimalException):
                            raise ContractError() from None
                        if not isinstance(payload, dict) or "errors" in payload:
                            raise ContractError()
                        return payload
                    delay = self.retry_delay(response.headers.get("Retry-After"), attempt)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                failure = ConnectivityError()
            except httpx.HTTPError:
                raise UnavailableError() from None
            if attempt == self._retries:
                raise failure from None
            await self._sleep(delay)
        raise AssertionError("Bounded retries exhausted")
