"""The narrow read surfaces SIGNAL is built on.

A worker never receives an HTTP client, a provider SDK, a session or a URL. It
receives finished normalized observations, assembled by trusted infrastructure
that already decided which source to ask and how to authenticate. There is
deliberately no ``fetch(url)`` anywhere in this package: a capability that can
retrieve an arbitrary address is a capability a prompt can eventually aim.
"""

from typing import Protocol
from uuid import UUID

from src.agents.signal.models import SignalObservation, SignalTaskInput, SignalWindow


class SignalSourceUnavailable(Exception):
    """A social source could not answer. Carries a safe reason code only.

    Never a provider message, a URL or a response body: any of the three can
    carry a credential, and none of them belongs in evidence or a traceback.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class SignalObservationReadPort(Protocol):
    """One question: what was publicly published about this market in this window.

    The window is passed in rather than chosen by the adapter, so freshness stays
    a policy decision and a provider cannot widen it by returning older rows —
    anything outside the window is dropped downstream regardless.
    """

    async def observations(
        self, *, chain: str, pair_id: str, window: SignalWindow
    ) -> tuple[SignalObservation, ...]: ...


class SignalContextPort(Protocol):
    """SIGNAL's only read capability, mirroring ORBIT and ATLAS.

    There is no method to browse other markets, read another role's evidence, see
    a risk outcome or read TradeCase status.
    """

    async def sentiment_context(self, trade_case_id: UUID, task_id: UUID) -> SignalTaskInput: ...
