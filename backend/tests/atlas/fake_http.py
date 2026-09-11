"""A deterministic stand-in for provider HTTP. No network, no credentials, no sleep."""

import json
from collections.abc import Callable

import httpx

from src.agents.atlas.sources.http import SourceTransport

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


class RecordingRoutes:
    """Routes by path suffix and remembers every request for assertions."""

    def __init__(self, routes: dict[str, Handler]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, handler in self.routes.items():
            if request.url.path.endswith(suffix):
                return handler(request)
        return httpx.Response(404, content=b"{}", headers={"Content-Type": "application/json"})

    def transport_factory(self, **overrides: object) -> Callable[[object], SourceTransport]:
        def factory(config: object) -> SourceTransport:
            headers: dict[str, str] = {}
            key = getattr(config, "api_key", "")
            if isinstance(config, object) and type(config).__name__ == "BlockscoutConfig":
                headers["Authorization"] = f"Bearer {key}"
            if type(config).__name__ == "MoralisConfig":
                headers["X-API-Key"] = str(key)
            settings: dict[str, object] = {
                "base_url": getattr(config, "base_url", "https://provider.invalid"),
                "headers": headers,
                # Mirror the real factories: the request budget comes from the
                # provider config, not from a transport default.
                "max_requests": getattr(config, "max_requests", 6),
                "timeout_seconds": getattr(config, "timeout_seconds", 10),
                "transport": httpx.MockTransport(self),
            }
            settings.update(overrides)
            return SourceTransport(**settings)  # type: ignore[arg-type]

        return factory
