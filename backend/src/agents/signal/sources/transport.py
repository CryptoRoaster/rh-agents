"""Bounded read-only HTTP for SIGNAL's social provider.

Infrastructure. A worker never holds one of these: the deterministic collector
constructs the adapter, and the adapter constructs this. There is no generic
"fetch a URL" capability in the SIGNAL package, and no caller — model, provider
response or otherwise — can choose a host: the base URL comes from validated
configuration and only a fixed path is appended.

Every request is bounded in time, response size and count. Transport-level
retries are deliberately absent. Phase 2B already owns authoritative, durable
task retries, and a second retry layer here would multiply into it — five
transport attempts inside three worker attempts is fifteen calls against a rate
limit that was already the problem.

This deliberately does not import ATLAS's transport. The two carry different
typed failure domains, and sharing one would mean a generic error enum that
neither agent wants to translate through. When a third agent needs one, that is
the moment to lift a common transport into core rather than now.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from decimal import DecimalException
from enum import StrEnum
from types import TracebackType

import httpx

from src.markets.geckoterminal.transport import invalid_constant, unique_keys
from src.runtime.rpc import RedactTransport


class SignalSourceFailure(StrEnum):
    """Why a social source could not answer, mapped safely from provider detail."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PAGINATION_INCONSISTENT = "PAGINATION_INCONSISTENT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


# Provider status codes mapped to safe categories. Nothing provider-specific
# reaches evidence, an audit record or an exception message.
STATUS_FAILURES: dict[int, SignalSourceFailure] = {
    400: SignalSourceFailure.INVALID_RESPONSE,
    401: SignalSourceFailure.UNAUTHORIZED,
    402: SignalSourceFailure.UNAUTHORIZED,
    403: SignalSourceFailure.UNAUTHORIZED,
    404: SignalSourceFailure.UNAVAILABLE,
    429: SignalSourceFailure.RATE_LIMIT,
}


class SignalTransportError(Exception):
    """A provider call failed. Carries a category, never provider detail.

    The message is the category name on purpose. A URL, query string or response
    body must never reach a log, a traceback or an evidence record, because any
    of them can carry an API key or a stranger's post.
    """

    def __init__(self, failure: SignalSourceFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class SocialTransport:
    """One bounded JSON client for one configured provider origin."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: int = 10,
        max_requests: int = 4,
        max_response_bytes: int = 2_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
            logger = logging.getLogger(name)
            if not any(isinstance(item, RedactTransport) for item in logger.filters):
                logger.addFilter(RedactTransport())
        self._max_requests = max_requests
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds
        self.requests_made = 0
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={
                "Accept": "application/json",
                "User-Agent": "rh-agents/0.4.0",
                **(headers or {}),
            },
            timeout=httpx.Timeout(timeout_seconds, connect=5),
            transport=transport,
            # A redirect could move the request — and its credentials — to a host
            # that was never configured.
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "SocialTransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: Mapping[str, str | int]) -> object:
        """One bounded GET returning parsed JSON, or a typed failure."""
        if self.requests_made >= self._max_requests:
            # One SIGNAL assessment must never become an unbounded crawl, and a
            # provider that keeps promising another page must not be able to
            # spend an account through this worker.
            raise SignalTransportError(SignalSourceFailure.BUDGET_EXHAUSTED)
        self.requests_made += 1
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._get(path, params)
        except TimeoutError:
            raise SignalTransportError(SignalSourceFailure.TIMEOUT) from None

    async def _get(self, path: str, params: Mapping[str, str | int]) -> object:
        try:
            request = self._client.stream("GET", path.lstrip("/"), params=dict(params))
            async with request as response:
                status = response.status_code
                if status != 200:
                    raise SignalTransportError(
                        STATUS_FAILURES.get(
                            status,
                            SignalSourceFailure.UNAVAILABLE
                            if status >= 500
                            else SignalSourceFailure.INVALID_RESPONSE,
                        )
                    )
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";")[0].strip().lower() != "application/json":
                    # An HTML error portal or a challenge page is not data.
                    raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
        except httpx.TimeoutException:
            raise SignalTransportError(SignalSourceFailure.TIMEOUT) from None
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            raise SignalTransportError(SignalSourceFailure.UNAVAILABLE) from None
        except httpx.HTTPError:
            raise SignalTransportError(SignalSourceFailure.UNAVAILABLE) from None
        try:
            return json.loads(
                bytes(body),
                parse_constant=invalid_constant,
                object_pairs_hook=unique_keys,
            )
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE) from None
