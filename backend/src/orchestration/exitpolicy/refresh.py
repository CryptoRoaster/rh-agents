"""The held market observed again, between the slow exit read and the sale.

A sweep decides a trigger on the marks recorded at the start of its run. The
exit's own on-chain read then takes its time, and every exit after the first
in a sweep waits on every read before it. This is the one bounded observation
that closes that gap: the held position's case and its market, plus every other
open holding SENTINEL values for the portfolio — exactly what a risk request's
pre-risk refresh observes, by the case's stored pool locator, through the same
identity checks and recorder. No discovery, no symbol search, no substitution.

**Bounded.** One attempt per exit, no retry inside it, the refresh stage's own
request and time budget, the run's remaining time, at most `limit` attempts
per exit phase, and — when set — a provider request ceiling for the whole phase
that a refresh is admitted under only while its own maximum still fits. A
refresh that cannot show the market fresh is a typed refusal for that one sale;
the next run asks again.
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
    """One exit phase's refresh attempts and provider requests, counted and capped.

    Requests are admitted before a refresh starts, never corrected after: one is
    begun only while the largest number of requests a single refresh may spend
    still fits, so the phase can never end above its request ceiling.
    """

    limit: int
    attempts: int = 0
    # The phase's provider request ceiling; `None` leaves only each refresh's own.
    max_provider_requests: int | None = None
    # The most requests one refresh may spend — the refresh stage's own bound.
    per_refresh_requests: int = 0
    provider_requests: int = 0

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.limit

    @property
    def requests_exhausted(self) -> bool:
        if self.max_provider_requests is None:
            return False
        return self.provider_requests + self.per_refresh_requests > self.max_provider_requests


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
        if self.budget.requests_exhausted:
            return "PRE_EXIT_PROVIDER_BUDGET_REACHED", 0, 0, None
        if self.deadline.expired:
            return "TIME_BUDGET_REACHED", 0, 0, None
        self.budget.attempts += 1
        started = time.monotonic()
        reading = await self.port.refresh(trade_case, self.deadline, networks=self.networks)
        seconds = round(time.monotonic() - started, 3)
        self.budget.provider_requests += reading.provider_requests
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
