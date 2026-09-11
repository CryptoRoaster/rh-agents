"""Adapter error mapping, proven without a single network call.

Every vendor failure must land in exactly one typed category, because a failure
misclassified as permanent blocks work that would have succeeded, and one
misclassified as transient retries what never can.
"""

from decimal import Decimal
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel, SecretStr

from src.reasoning.anthropic_provider import ANTHROPIC_PROVIDER, AnthropicReasoningProvider
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningRequest,
)


class Output(BaseModel):
    verdict: str


def request() -> ReasoningRequest[Output]:
    return ReasoningRequest(
        instructions="Only return the schema.",
        data={"observation": {"value": Decimal("1.10")}},
        output_model=Output,
        max_output_tokens=256,
        timeout_seconds=5.0,
    )


def provider(key: str = "test-key") -> AnthropicReasoningProvider:
    return AnthropicReasoningProvider(api_key=SecretStr(key), model="claude-opus-5")


class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: Any) -> None:
        self.messages = FakeMessages(outcome)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeResponse:
    def __init__(self, parsed: Any, stop_reason: str | None = None) -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = type("Usage", (), {"input_tokens": 11, "output_tokens": 5})()
        self._request_id = "req_test"


def install(monkeypatch, outcome: Any) -> FakeClient:
    client = FakeClient(outcome)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kwargs: client)
    return client


def http_error(cls: type, status: int = 500) -> Exception:
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
    return cls("vendor detail that must not leak", response=response, body=None)


async def test_missing_key_never_reaches_the_network(monkeypatch):
    called = {"value": False}

    def explode(**kwargs: Any) -> None:
        called["value"] = True
        raise AssertionError("no client may be constructed without a key")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", explode)
    with pytest.raises(ReasoningFailure) as caught:
        await provider(key="").generate_structured(request())
    assert caught.value.category == ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED
    assert called["value"] is False


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            anthropic.APITimeoutError(httpx.Request("POST", "https://example.invalid")),
            ReasoningErrorCategory.PROVIDER_TIMEOUT,
        ),
        (http_error(anthropic.RateLimitError, 429), ReasoningErrorCategory.PROVIDER_RATE_LIMIT),
        (
            http_error(anthropic.AuthenticationError, 401),
            ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED,
        ),
        (
            http_error(anthropic.PermissionDeniedError, 403),
            ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED,
        ),
        (
            http_error(anthropic.BadRequestError, 400),
            ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST,
        ),
        (
            http_error(anthropic.NotFoundError, 404),
            ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST,
        ),
        (
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
            ReasoningErrorCategory.PROVIDER_UNAVAILABLE,
        ),
        (http_error(anthropic.APIStatusError, 503), ReasoningErrorCategory.PROVIDER_UNAVAILABLE),
    ],
)
async def test_every_vendor_failure_maps_to_one_typed_category(monkeypatch, error, expected):
    install(monkeypatch, error)
    with pytest.raises(ReasoningFailure) as caught:
        await provider().generate_structured(request())
    assert caught.value.category == expected
    # The vendor message may quote the request back; it must never surface.
    assert "vendor detail" not in str(caught.value)


async def test_a_refusal_is_not_mistaken_for_output(monkeypatch):
    install(monkeypatch, FakeResponse({"verdict": "ok"}, stop_reason="refusal"))
    with pytest.raises(ReasoningFailure) as caught:
        await provider().generate_structured(request())
    assert caught.value.category == ReasoningErrorCategory.PROVIDER_REFUSED


@pytest.mark.parametrize("parsed", [None, {"wrong_field": 1}, {"verdict": None}])
async def test_missing_or_malformed_output_is_invalid_not_a_guess(monkeypatch, parsed):
    install(monkeypatch, FakeResponse(parsed))
    with pytest.raises(ReasoningFailure) as caught:
        await provider().generate_structured(request())
    assert caught.value.category == ReasoningErrorCategory.INVALID_MODEL_OUTPUT


async def test_successful_call_returns_typed_output_and_safe_metadata(monkeypatch):
    client = install(monkeypatch, FakeResponse({"verdict": "ok"}))
    result = await provider().generate_structured(request())
    assert result.output.verdict == "ok"
    assert result.model.provider == ANTHROPIC_PROVIDER
    assert result.model.model == "claude-opus-5"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 5
    assert result.usage.latency_ms is not None
    assert result.usage.provider_request_id == "req_test"

    sent = client.messages.calls[0]
    # Instructions and data stay in separate channels, and money is canonical text.
    assert sent["system"] == "Only return the schema."
    assert sent["messages"][0]["content"] == '{"observation":{"value":"1.1"}}'
    assert sent["max_tokens"] == 256
    assert sent["output_format"] is Output


async def test_transport_retries_stay_minimal_so_attempts_do_not_multiply(monkeypatch):
    captured: dict[str, Any] = {}

    def capture(**kwargs: Any) -> FakeClient:
        captured.update(kwargs)
        return FakeClient(FakeResponse({"verdict": "ok"}))

    monkeypatch.setattr(anthropic, "AsyncAnthropic", capture)
    await provider().generate_structured(request())
    # The worker runtime owns authoritative retries; the transport must not
    # silently multiply them.
    assert captured["max_retries"] == 1
    assert captured["timeout"] == 5.0


async def test_effort_is_only_sent_when_configured(monkeypatch):
    client = install(monkeypatch, FakeResponse({"verdict": "ok"}))
    await provider().generate_structured(request())
    assert "output_config" not in client.messages.calls[0]

    client = install(monkeypatch, FakeResponse({"verdict": "ok"}))
    configured = AnthropicReasoningProvider(
        api_key=SecretStr("k"), model="claude-opus-5", effort="low"
    )
    await configured.generate_structured(request())
    assert client.messages.calls[0]["output_config"] == {"effort": "low"}
