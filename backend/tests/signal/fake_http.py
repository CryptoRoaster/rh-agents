"""A deterministic stand-in for provider HTTP. No network, no credentials, no sleep."""

import json
from collections.abc import Callable

import httpx

from src.agents.signal.sources.transport import SocialTransport

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


class RecordingRoutes:
    """Routes by path suffix and remembers every request for assertions."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def transport_factory(self, **overrides: object) -> Callable[[object], SocialTransport]:
        def factory(config: object) -> SocialTransport:
            settings: dict[str, object] = {
                "base_url": getattr(config, "base_url", "https://provider.invalid"),
                "headers": {"x-api-key": str(getattr(config, "api_key", ""))},
                # Mirror the real factory: the budget comes from the config.
                "max_requests": getattr(config, "max_requests", 4),
                "timeout_seconds": getattr(config, "timeout_seconds", 10),
                "transport": httpx.MockTransport(self),
            }
            settings.update(overrides)
            return SocialTransport(**settings)  # type: ignore[arg-type]

        return factory


def pages(*payloads: object) -> Handler:
    """Serve one scripted payload per request, repeating the last one."""
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return json_response(payload)

    return handler
