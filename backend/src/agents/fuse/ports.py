"""The single read a synthesis performs.

A synthesizer never receives a session, a repository, an evidence query or a
provider client. It receives one finished typed view of what the server decided
is currently admissible. There is no method here to fetch another case, another
version of this case's evidence, or a specific envelope by id — the absence of
those is what makes "FUSE cannot cherry-pick its inputs" a property of the type
rather than a rule somebody has to keep.
"""

from typing import Protocol
from uuid import UUID

from src.agents.fuse.models import FuseTaskInput


class FuseContextUnavailable(Exception):
    """No synthesis context exists. Carries a safe reason code only.

    Raised when the question cannot be asked at all — the case is terminal, or
    nothing has been recorded yet. Distinct from a synthesis that found the
    evidence blocked or incomplete, which is an ordinary result and is recorded.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class FuseContextPort(Protocol):
    """FUSE's only read capability, mirroring every other specialist."""

    async def synthesis_context(self, trade_case_id: UUID, task_id: UUID) -> FuseTaskInput: ...
