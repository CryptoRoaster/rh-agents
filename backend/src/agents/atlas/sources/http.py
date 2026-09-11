"""Bounded read-only HTTP for ATLAS fact providers.

Infrastructure. A worker never holds one of these: the deterministic collector
constructs the adapters, and the adapters construct this. There is no generic
"fetch a URL" capability anywhere in the worker path, and no caller — model or
otherwise — can choose a host: the base URL comes from validated configuration
and only a fixed path is appended.

Every request is bounded in time, response size and count. Retries are
deliberately absent: Phase 2B already owns authoritative task retries, and a
second retry layer here would silently multiply a provider's rate-limit cost.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from decimal import DecimalException
from types import TracebackType

import httpx

from src.agents.atlas.models import AtlasSourceFailure
from src.markets.geckoterminal.transport import invalid_constant, unique_keys
from src.runtime.rpc import RedactTransport

# Provider status codes mapped to safe categories. Nothing provider-specific
# reaches evidence, an audit record or an exception message.
STATUS_FAILURES: dict[int, AtlasSourceFailure] = {
    401: AtlasSourceFailure.NOT_CONFIGURED,
    402: AtlasSourceFailure.NOT_CONFIGURED,
    403: AtlasSourceFailure.NOT_CONFIGURED,
    404: AtlasSourceFailure.UNAVAILABLE,
    429: AtlasSourceFailure.RATE_LIMIT,
}


class SourceRequestError(Exception):
    """A provider call failed. Carries a category, never provider detail.

    The message is the category name on purpose. A URL, query string or response
    body must never reach a log, a traceback or an evidence record, because any
    of them can carry an API key.
    """

    def __init__(self, failure: AtlasSourceFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class SourceTransport:
    """One bounded JSON client for one configured provider origin."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: int = 10,
        max_requests: int = 6,
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

    async def __aenter__(self) -> "SourceTransport":
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
            # A single ATLAS assessment must never become an unbounded crawl.
            raise SourceRequestError(AtlasSourceFailure.INCOMPLETE_RESULT)
        self.requests_made += 1
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._get(path, params)
        except TimeoutError:
            raise SourceRequestError(AtlasSourceFailure.TIMEOUT) from None

    async def _get(self, path: str, params: Mapping[str, str | int]) -> object:
        try:
            request = self._client.stream("GET", path.lstrip("/"), params=dict(params))
            async with request as response:
                status = response.status_code
                if status != 200:
                    raise SourceRequestError(
                        STATUS_FAILURES.get(
                            status,
                            AtlasSourceFailure.UNAVAILABLE
                            if status >= 500
                            else AtlasSourceFailure.INVALID_RESPONSE,
                        )
                    )
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";")[0].strip().lower() != "application/json":
                    # An HTML challenge page or an error portal is not data.
                    raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
        except httpx.TimeoutException:
            raise SourceRequestError(AtlasSourceFailure.TIMEOUT) from None
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            raise SourceRequestError(AtlasSourceFailure.UNAVAILABLE) from None
        except httpx.HTTPError:
            raise SourceRequestError(AtlasSourceFailure.UNAVAILABLE) from None
        try:
            return json.loads(
                bytes(body),
                parse_constant=invalid_constant,
                object_pairs_hook=unique_keys,
            )
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE) from None
