"""The held market observed again, between the slow exit read and the sale.

A sweep decides a trigger on the marks recorded at the start of its run. The
exit's own on-chain read then takes its time, and every exit after the first
in a sweep waits on every read before it. This is the one bounded observation
that closes that gap: the held position's case and its market, plus every other
open holding SENTINEL values for the portfolio — exactly what a risk request's
pre-risk refresh observes, by the case's stored pool locator, through the same
identity checks and recorder. No discovery, no symbol search, no substitution.

**Bounded.** One attempt per exit, no retry inside it, the refresh stage's own
request and time budget, the run's remaining time, and at most `max_refreshes`
attempts per sweep. A refresh that cannot show the market fresh is a typed
refusal for that one sale; the next run asks again.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Protocol

from src.markets.geckoterminal.networks import VerifiedNetworkRegistry
from src.orchestration.workflow.models import TradeCase


class RefreshDeadline(Protocol):
    @property
    def remaining(self) -> float: ...

    @property
    def expired(self) -> bool: ...


class RefreshOutcome(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    @property
    def provider_requests(self) -> int: ...


class PreExitMarketRefresh(Protocol):
    """Observe one case's market and every open holding's, then judge freshness.

    `src.runner.pre_risk.PreRiskMarketRefresh` is the implementation.
    """

    async def refresh(
        self,
        trade_case: TradeCase,
        deadline: RefreshDeadline,
        *,
        networks: VerifiedNetworkRegistry | None = None,
    ) -> RefreshOutcome: ...


class Unbounded:
    """No outer bound beyond the refresh stage's own."""

    @property
    def remaining(self) -> float:
        return math.inf

    @property
    def expired(self) -> bool:
        return False


@dataclass
class RefreshBudget:
    """One sweep's refresh attempts, counted and capped."""

    limit: int
    attempts: int = 0

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.limit


@dataclass
class RefreshContext:
    """What a sweep shares with every exit it asks for: the port and its bounds."""

    port: PreExitMarketRefresh | None
    budget: RefreshBudget
    deadline: RefreshDeadline = field(default_factory=Unbounded)
    networks: VerifiedNetworkRegistry | None = None

    async def observe(self, trade_case: TradeCase) -> tuple[str | None, int, int, float | None]:
        """One attempt: `(refusal code or None, attempts, provider requests, seconds)`.

        No port configured observes nothing and refuses nothing: the trigger is
        then judged again on what is recorded, under the same final freshness
        checks.
        """
        if self.port is None:
            return None, 0, 0, None
        if self.budget.exhausted:
            return "PRE_EXIT_REFRESH_BUDGET_REACHED", 0, 0, None
        if self.deadline.expired:
            return "TIME_BUDGET_REACHED", 0, 0, None
        self.budget.attempts += 1
        started = time.monotonic()
        reading = await self.port.refresh(trade_case, self.deadline, networks=self.networks)
        seconds = round(time.monotonic() - started, 3)
        if not reading.ready:
            return reading.reason or "PROVIDER_FAILED", 1, reading.provider_requests, seconds
        return None, 1, reading.provider_requests, seconds


__all__ = [
    "PreExitMarketRefresh",
    "RefreshBudget",
    "RefreshContext",
    "RefreshDeadline",
    "RefreshOutcome",
    "Unbounded",
]
