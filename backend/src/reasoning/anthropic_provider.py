"""Anthropic adapter for the structured reasoning port.

Infrastructure only: no domain rule lives here, and the rest of the system never
imports it directly. Disabled unless explicitly configured, so booting the API
cannot start paid calls.

The API key is held as a SecretStr, is never logged, never persisted, never
returned through an HTTP route and never reaches a worker result. Instructions
and data travel in separate channels so market strings can never act as
instructions.
"""

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import BaseModel, SecretStr, ValidationError

from src.core.numbers import canonical_decimal
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningRequest,
    ReasoningResult,
    ReasoningUsage,
)

ANTHROPIC_PROVIDER = "anthropic"

# The runtime owns authoritative retries, so the transport keeps only a minimal
# budget for connection blips. Anything else becomes a typed failure and Phase 2B
# decides whether the task retries.
DEFAULT_TRANSPORT_RETRIES = 1


def _canonical(value: object) -> object:
    """Serialize domain values without ever passing money through a float."""
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


@dataclass(frozen=True)
class AnthropicReasoningProvider:
    api_key: SecretStr
    model: str
    effort: str | None = None
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES

    @property
    def name(self) -> str:
        return ANTHROPIC_PROVIDER

    async def generate_structured[Output: BaseModel](
        self, request: ReasoningRequest[Output]
    ) -> ReasoningResult[Output]:
        key = self.api_key.get_secret_value()
        if not key:
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, "MISSING_API_KEY"
            )
        client = anthropic.AsyncAnthropic(
            api_key=key, timeout=request.timeout_seconds, max_retries=self.transport_retries
        )
        # The data channel is a single serialized JSON document. Nothing from the
        # market is interpolated into the instruction channel, so a token named
        # "IGNORE ALL RULES" stays a quoted string.
        payload = json.dumps(
            _canonical(request.data), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        parameters: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.instructions,
            "messages": [{"role": "user", "content": payload}],
            "output_format": request.output_model,
        }
        if self.effort is not None:
            parameters["output_config"] = {"effort": self.effort}
        started = time.monotonic()
        try:
            async with client:
                response = await client.messages.parse(**parameters)
        except anthropic.APITimeoutError:
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_TIMEOUT, "PROVIDER_TIMEOUT"
            ) from None
        except anthropic.RateLimitError:
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_RATE_LIMIT, "PROVIDER_RATE_LIMIT"
            ) from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, "PROVIDER_CREDENTIALS_REJECTED"
            ) from None
        except (anthropic.BadRequestError, anthropic.NotFoundError):
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST, "PROVIDER_REJECTED_REQUEST"
            ) from None
        except anthropic.APIConnectionError:
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "PROVIDER_UNREACHABLE"
            ) from None
        except anthropic.APIStatusError:
            # Never surface a vendor message; it may quote the request back.
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "PROVIDER_ERROR"
            ) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        if getattr(response, "stop_reason", None) == "refusal":
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_REFUSED, "PROVIDER_REFUSED"
            ) from None
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ReasoningFailure(
                ReasoningErrorCategory.INVALID_MODEL_OUTPUT, "OUTPUT_MISSING"
            ) from None
        try:
            # Provider-side schema enforcement never replaces local validation.
            output = request.output_model.model_validate(
                parsed if isinstance(parsed, dict) else parsed.model_dump()
            )
        except ValidationError:
            raise ReasoningFailure(
                ReasoningErrorCategory.INVALID_MODEL_OUTPUT, "OUTPUT_SCHEMA_MISMATCH"
            ) from None
        usage = getattr(response, "usage", None)
        return ReasoningResult(
            output=output,
            model=ReasoningModel(provider=ANTHROPIC_PROVIDER, model=self.model, effort=self.effort),
            usage=ReasoningUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                latency_ms=latency_ms,
                provider_request_id=getattr(response, "_request_id", None),
            ),
        )
