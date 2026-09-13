"""The narrow read surface ANCHOR is built on.

A worker never receives a quote provider, an HTTP client, an RPC client or a
URL. It receives one finished ladder of typed quotes, already obtained. There is
no method here to ask for an arbitrary quote, reach another market, or send
anything anywhere — a port that could would put the means of execution one line
from the thing that assesses it.
"""

from typing import Protocol
from uuid import UUID

from src.agents.anchor.models import AnchorTaskInput


class AnchorContextUnavailable(Exception):
    """No usable execution context exists. Carries a safe reason code only.

    Raised when the assessment could not be attempted at all — the case could
    not be read, the market identity is incoherent. A market that answers and
    says it has no liquidity is not this: that is evidence, and it travels
    through the ladder rather than through an exception.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AnchorContextPort(Protocol):
    """ANCHOR's only read capability, mirroring the other specialists."""

    async def execution_context(self, trade_case_id: UUID, task_id: UUID) -> AnchorTaskInput: ...
