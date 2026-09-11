"""Deterministic scripted reasoning provider.

Exists so the entire specialist pipeline can be exercised offline and
reproducibly. It is never a default and performs no I/O. Production use is
meaningless: it returns exactly what a caller scripted.
"""

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningRequest,
    ReasoningResult,
    ReasoningUsage,
)

FAKE_PROVIDER = "fake"
FAKE_MODEL = "deterministic-1"


@dataclass
class ScriptedReply:
    """One scripted provider turn: either a payload or a typed failure."""

    payload: dict[str, Any] | None = None
    failure: ReasoningErrorCategory | None = None
    reason_code: str = "SCRIPTED_FAILURE"
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if (self.payload is None) == (self.failure is None):
            raise ValueError("A scripted reply is exactly one of a payload or a failure")


@dataclass
class DeterministicReasoningProvider:
    """Replays scripted turns in order, repeating the last one once exhausted."""

    replies: deque[ScriptedReply] = field(default_factory=deque)
    calls: list[ReasoningRequest[Any]] = field(default_factory=list)
    _last: ScriptedReply | None = field(default=None, repr=False)

    @classmethod
    def scripted(cls, replies: Iterable[ScriptedReply]) -> "DeterministicReasoningProvider":
        return cls(replies=deque(replies))

    @classmethod
    def returning(cls, payload: dict[str, Any]) -> "DeterministicReasoningProvider":
        return cls.scripted([ScriptedReply(payload=payload)])

    @classmethod
    def failing(
        cls, category: ReasoningErrorCategory, reason_code: str = "SCRIPTED_FAILURE"
    ) -> "DeterministicReasoningProvider":
        return cls.scripted([ScriptedReply(failure=category, reason_code=reason_code)])

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    async def generate_structured[Output: BaseModel](
        self, request: ReasoningRequest[Output]
    ) -> ReasoningResult[Output]:
        self.calls.append(request)
        if self.replies:
            reply = self.replies.popleft()
            self._last = reply
        elif self._last is not None:
            reply = self._last
        else:
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, "NO_SCRIPTED_REPLY"
            )
        if reply.delay_seconds:
            # Honour the caller's own deadline rather than inventing one.
            await asyncio.wait_for(
                asyncio.sleep(reply.delay_seconds), timeout=request.timeout_seconds
            )
        if reply.failure is not None:
            raise ReasoningFailure(reply.failure, reply.reason_code)
        assert reply.payload is not None
        try:
            output = request.output_model.model_validate(reply.payload)
        except ValidationError:
            # A scripted malformed payload must fail exactly like a real model's.
            raise ReasoningFailure(
                ReasoningErrorCategory.INVALID_MODEL_OUTPUT, "OUTPUT_SCHEMA_MISMATCH"
            ) from None
        return ReasoningResult(
            output=output,
            model=ReasoningModel(provider=FAKE_PROVIDER, model=FAKE_MODEL),
            usage=ReasoningUsage(input_tokens=0, output_tokens=0, latency_ms=0),
        )
