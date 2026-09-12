"""The narrow read surface VECTOR is built on.

A worker never receives a session, a market provider client, an RPC client or a
URL. It receives one finished typed view, assembled by trusted infrastructure
that already owns market reads and evidence reads. There is no method here to
browse other markets, read another case, or change anything at all.
"""

from typing import Protocol
from uuid import UUID

from src.agents.vector.models import VectorTaskInput


class VectorContextUnavailable(Exception):
    """No usable setup context exists. Carries a safe reason code only.

    Raised before any model call, so a market this system cannot currently
    describe never costs a reasoning request and never produces a setup built on
    a price nobody observed.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class VectorContextPort(Protocol):
    """VECTOR's only read capability, mirroring ORBIT, ATLAS and SIGNAL."""

    async def setup_context(self, trade_case_id: UUID, task_id: UUID) -> VectorTaskInput: ...
