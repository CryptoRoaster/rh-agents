"""The single fast-assessment boundary the scout's shadow triage uses."""

from typing import Protocol

from src.fast_reasoning.models import FastRequest, FastResult


class FastAssessmentProvider(Protocol):
    """Answer typed questions about one state within a bounded time.

    Implementations expose no tools, database, filesystem or credentials, make
    exactly one attempt per call (no hidden retries), and raise
    `ReasoningFailure` with a typed category and a sanitised reason code.
    """

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def assess(self, request: FastRequest) -> FastResult: ...
