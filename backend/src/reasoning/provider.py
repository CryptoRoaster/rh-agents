"""The single reasoning boundary every specialist worker uses."""

from typing import Protocol

from pydantic import BaseModel

from src.reasoning.models import ReasoningRequest, ReasoningResult


class ReasoningProvider(Protocol):
    """Turn typed input into typed output within a bounded time.

    Implementations must not expose tools, network access, filesystem access or
    credentials through this interface, and must raise ``ReasoningFailure`` with a
    typed category rather than leaking vendor errors.
    """

    @property
    def name(self) -> str: ...

    async def generate_structured[Output: BaseModel](
        self, request: ReasoningRequest[Output]
    ) -> ReasoningResult[Output]: ...
