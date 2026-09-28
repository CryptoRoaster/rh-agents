"""TypeSafe Jev over its documented HTTP API. Infrastructure only.

Contract (docs.typesafe.ai/api, /models, /sdk/python/api/constants, checked
2026-09-28):

- `POST {base}/v1/systemone`, `Authorization: Bearer <TYPESAFE_API_KEY>`.
- Body: `state`, `model`, `questions` (a map of typed noul/choice/score
  questions). Response: `model` (the versioned id that answered), `answers`
  keyed like the questions, `usage.input_tokens/output_tokens`.
- Errors: 401 bad key, 422 invalid request, 429 rate limit, 529 overloaded.
- Pin a versioned model (`jev-1.13.0`); aliases such as `jev-latest` move when
  a release ships and are refused by Settings.

Deliberately raw HTTP rather than the SDK: one dependency fewer, and the SDK
retries by default, while this adapter makes exactly one attempt. A failure is
recorded and the watch simply has no fast assessment; there is no retry storm.

The key is a `SecretStr`, sent only in the Authorization header, and never
logged, persisted or put into an error. No error keeps a response body.
"""

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from src.core.numbers import canonical_decimal
from src.fast_reasoning.models import (
    ChoiceAnswer,
    ChoiceQuestion,
    FastRequest,
    FastResult,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure

JEV_PROVIDER = "jev"
ENDPOINT = "/v1/systemone"
_C = ReasoningErrorCategory

# Status -> (category, code). Anything else 4xx is a rejected request, 5xx unavailable.
STATUS_FAILURES: dict[int, tuple[ReasoningErrorCategory, str]] = {
    401: (_C.PROVIDER_NOT_CONFIGURED, "JEV_UNAUTHORIZED"),
    403: (_C.PROVIDER_NOT_CONFIGURED, "JEV_FORBIDDEN"),
    404: (_C.PROVIDER_NOT_CONFIGURED, "JEV_NOT_FOUND"),
    422: (_C.PROVIDER_REJECTED_REQUEST, "JEV_UNPROCESSABLE"),
    429: (_C.PROVIDER_RATE_LIMIT, "JEV_RATE_LIMITED"),
    529: (_C.PROVIDER_UNAVAILABLE, "JEV_OVERLOADED"),
}


def _canonical(value: object) -> object:
    """JSON-safe state without passing money through a float."""
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _answer(question: object, raw: object) -> NoulAnswer | ChoiceAnswer | ScoreAnswer:
    """One answer, validated against the question that asked for it."""
    if isinstance(question, NoulQuestion):
        return NoulAnswer.model_validate(raw)
    if isinstance(question, ChoiceQuestion):
        answer = ChoiceAnswer.model_validate(raw)
        if set(answer.probabilities) != set(question.criteria):
            raise ValueError("choice options differ from the question")
        return answer
    if isinstance(question, ScoreQuestion):
        score = ScoreAnswer.model_validate(raw)
        levels = {str(index) for index in range(len(question.criteria))}
        if set(score.probabilities) != levels or score.score > len(question.criteria) - 1:
            raise ValueError("score levels differ from the question")
        return score
    raise ValueError("unknown question type")  # pragma: no cover - the union is closed


@dataclass(frozen=True)
class JevProvider:
    api_key: SecretStr
    requested_model: str
    base_url: str = "https://api.typesafe.ai"
    timeout_seconds: float = 10.0
    transport: httpx.AsyncBaseTransport | None = None

    @property
    def name(self) -> str:
        return JEV_PROVIDER

    @property
    def model(self) -> str:
        return self.requested_model

    async def assess(self, request: FastRequest) -> FastResult:
        key = self.api_key.get_secret_value()
        if not key:
            raise ReasoningFailure(_C.PROVIDER_NOT_CONFIGURED, "JEV_KEY_MISSING")
        body = {
            "state": _canonical(request.state),
            "model": self.requested_model,
            "questions": {
                name: question.model_dump(mode="json", exclude_none=True)
                for name, question in request.questions.items()
            },
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
            ) as client:
                response = await client.post(
                    ENDPOINT,
                    content=json.dumps(body, separators=(",", ":"), ensure_ascii=True),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise ReasoningFailure(_C.PROVIDER_TIMEOUT, "JEV_TIMEOUT") from None
        except httpx.HTTPError:
            raise ReasoningFailure(_C.PROVIDER_UNAVAILABLE, "JEV_CONNECTION_FAILED") from None
        latency_ms = int((time.monotonic() - started) * 1000)
        status = response.status_code
        if status != 200:
            if status in STATUS_FAILURES:
                category, code = STATUS_FAILURES[status]
            elif 400 <= status < 500:
                category, code = _C.PROVIDER_REJECTED_REQUEST, "JEV_CLIENT_ERROR"
            else:
                category, code = _C.PROVIDER_UNAVAILABLE, "JEV_SERVER_ERROR"
            raise ReasoningFailure(category, code)
        try:
            payload: Any = response.json()
            raw_answers = payload["answers"]
            if not isinstance(raw_answers, dict) or set(raw_answers) != set(request.questions):
                raise ValueError("answers do not match the questions")
            answers = {
                name: _answer(question, raw_answers[name])
                for name, question in request.questions.items()
            }
            reported = payload.get("model")
            usage = payload.get("usage") or {}
            return FastResult(
                answers=answers,
                provider=JEV_PROVIDER,
                requested_model=self.requested_model,
                model_version=reported if isinstance(reported, str) and reported else None,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                latency_ms=latency_ms,
            )
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise ReasoningFailure(_C.INVALID_MODEL_OUTPUT, "JEV_OUTPUT_INVALID") from None
