"""The narrow read surface PULSE is built on.

A monitor never receives a session, a market provider client, an RPC client or a
URL. It receives one finished typed view containing the condition to watch and
the observation to judge it against. There is no method here to browse other
markets, read another case, choose what to query, or change anything at all.
"""

from typing import Protocol
from uuid import UUID

from src.agents.pulse.models import PulseTaskInput


class PulseContextUnavailable(Exception):
    """No usable trigger context exists. Carries a safe reason code only.

    Distinct from "the condition is not yet true", which is an ordinary result
    rather than an exception. This is raised when the check could not be made at
    all — the case could not be read, the market layer could not answer.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PulseContextPort(Protocol):
    """PULSE's only read capability, mirroring the other specialists."""

    async def trigger_context(self, trade_case_id: UUID, task_id: UUID) -> PulseTaskInput: ...
