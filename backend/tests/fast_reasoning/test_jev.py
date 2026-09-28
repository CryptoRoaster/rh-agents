"""The Jev adapter against the documented HTTP contract, offline (httpx.MockTransport)."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import Settings
from src.fast_reasoning.jev import JevProvider
from src.fast_reasoning.models import (
    ChoiceQuestion,
    FastRequest,
    NoulQuestion,
    ScoreQuestion,
)
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure

KEY = "ts-test-key-not-real"
QUESTIONS = {
    "anomaly": NoulQuestion(instructions="Is this unusual?"),
    "quality": ChoiceQuestion(
        instructions="How complete?", criteria={"complete": "all", "partial": "some"}
    ),
    "organic": ScoreQuestion(instructions="How organic?", criteria=("none", "thin", "strong")),
}
GOOD = {
    "model": "jev-1.13.0",
    "answers": {
        "anomaly": {"type": "noul", "noul": 0.12},
        "quality": {
            "type": "choice",
            "choice": "complete",
            "probabilities": {"complete": 0.9, "partial": 0.1},
            "confidence": 0.85,
        },
        "organic": {
            "type": "score",
            "score": 1.2,
            "legend": {"0": "none", "1": "thin", "2": "strong"},
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
            "confidence": 0.5,
        },
    },
    "usage": {"input_tokens": 410, "output_tokens": 22},
}


class Scripted:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return self.response

        return httpx.MockTransport(handle)


def provider(scripted: Scripted, key: str = KEY) -> JevProvider:
    return JevProvider(
        api_key=SecretStr(key), requested_model="jev-1.13.0", transport=scripted.transport()
    )


def request() -> FastRequest:
    return FastRequest(state={"liquidity_usd": {"value": Decimal("5000.50")}}, questions=QUESTIONS)


async def test_a_valid_answer_is_typed_and_keeps_the_answering_version():
    scripted = Scripted(httpx.Response(200, json=GOOD))
    result = await provider(scripted).assess(request())

    assert result.provider == "jev"
    assert result.requested_model == "jev-1.13.0"
    assert result.model_version == "jev-1.13.0"
    assert result.answers["anomaly"].noul == pytest.approx(0.12)
    assert result.answers["quality"].choice == "complete"
    assert result.answers["organic"].score == pytest.approx(1.2)
    assert (result.input_tokens, result.output_tokens) == (410, 22)


async def test_the_request_follows_the_documented_contract():
    scripted = Scripted(httpx.Response(200, json=GOOD))
    await provider(scripted).assess(request())

    (sent,) = scripted.requests  # exactly one attempt
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.typesafe.ai/v1/systemone"
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["model"] == "jev-1.13.0"
    # Money travels as a decimal string, never through a float.
    assert body["state"] == {"liquidity_usd": {"value": "5000.5"}}
    assert body["questions"]["organic"] == {
        "type": "score",
        "instructions": "How organic?",
        "criteria": ["none", "thin", "strong"],
    }


@pytest.mark.parametrize(
    ("status", "category", "code"),
    [
        (401, ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, "JEV_UNAUTHORIZED"),
        (422, ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST, "JEV_UNPROCESSABLE"),
        (429, ReasoningErrorCategory.PROVIDER_RATE_LIMIT, "JEV_RATE_LIMITED"),
        (529, ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "JEV_OVERLOADED"),
        (500, ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "JEV_SERVER_ERROR"),
        (418, ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST, "JEV_CLIENT_ERROR"),
    ],
)
async def test_an_error_status_is_typed_and_never_retried(status, category, code):
    body = {"error": f"secret echo {KEY}"}
    scripted = Scripted(httpx.Response(status, json=body))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(scripted).assess(request())

    assert (raised.value.category, raised.value.reason_code) == (category, code)
    assert len(scripted.requests) == 1
    # Neither the key nor the body leaves in the error.
    assert KEY not in str(raised.value) and "echo" not in str(raised.value)


async def test_a_timeout_and_a_lost_connection_are_typed():
    timeout = Scripted(error=httpx.ReadTimeout("slow"))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(timeout).assess(request())
    assert raised.value.category is ReasoningErrorCategory.PROVIDER_TIMEOUT
    assert raised.value.reason_code == "JEV_TIMEOUT"

    lost = Scripted(error=httpx.ConnectError("refused"))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(lost).assess(request())
    assert raised.value.category is ReasoningErrorCategory.PROVIDER_UNAVAILABLE
    assert raised.value.reason_code == "JEV_CONNECTION_FAILED"


@pytest.mark.parametrize(
    "answers",
    [
        {},  # nothing answered
        {**GOOD["answers"], "extra": {"type": "noul", "noul": 0.5}},  # an unasked answer
        {**GOOD["answers"], "anomaly": {"type": "noul", "noul": 1.7}},  # not a probability
        {  # probabilities that are not a distribution
            **GOOD["answers"],
            "quality": {
                **GOOD["answers"]["quality"],
                "probabilities": {"complete": 0.9, "partial": 0.5},
            },
        },
        {  # options that are not the question's
            **GOOD["answers"],
            "quality": {
                "type": "choice",
                "choice": "buy",
                "probabilities": {"buy": 1.0},
                "confidence": 1.0,
            },
        },
        {  # levels that are not the question's
            **GOOD["answers"],
            "organic": {
                **GOOD["answers"]["organic"],
                "legend": {"0": "a", "1": "b"},
                "probabilities": {"0": 0.5, "1": 0.5},
            },
        },
        {**GOOD["answers"], "anomaly": {"type": "score", "score": 1}},  # wrong type
    ],
)
async def test_an_answer_that_does_not_fit_the_questions_is_refused(answers):
    scripted = Scripted(httpx.Response(200, json={**GOOD, "answers": answers}))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(scripted).assess(request())
    assert raised.value.category is ReasoningErrorCategory.INVALID_MODEL_OUTPUT
    assert raised.value.reason_code == "JEV_OUTPUT_INVALID"


async def test_a_body_that_is_not_json_is_refused():
    scripted = Scripted(httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(scripted).assess(request())
    assert raised.value.reason_code == "JEV_OUTPUT_INVALID"


async def test_a_missing_key_makes_no_call():
    scripted = Scripted(httpx.Response(200, json=GOOD))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(scripted, key="").assess(request())
    assert raised.value.reason_code == "JEV_KEY_MISSING"
    assert scripted.requests == []


def settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg:///rh_agents_test?host=/tmp")
    for name in ("FAST_REASONING_PROVIDER", "TYPESAFE_API_KEY", "JEV_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_jev_is_disabled_by_default_and_a_key_alone_selects_nothing(monkeypatch):
    configured = settings(monkeypatch, TYPESAFE_API_KEY="ts-unrelated")
    assert configured.fast_reasoning_provider == "disabled"
    assert configured.jev_model == "jev-1.13.0"
    assert (configured.jev_max_assessments_per_run, configured.jev_max_assessments_per_day) == (
        10,
        960,
    )


def test_jev_needs_a_key_and_a_pinned_version(monkeypatch):
    with pytest.raises(ValidationError):
        settings(monkeypatch, FAST_REASONING_PROVIDER="jev")
    for alias in ("jev-latest", "jev-preview"):
        with pytest.raises(ValidationError):
            settings(
                monkeypatch, FAST_REASONING_PROVIDER="jev", TYPESAFE_API_KEY=KEY, JEV_MODEL=alias
            )
    chosen = settings(monkeypatch, FAST_REASONING_PROVIDER="jev", TYPESAFE_API_KEY=KEY)
    assert chosen.fast_reasoning_provider == "jev"
    # The key never appears in a repr of the settings.
    assert KEY not in repr(chosen)
