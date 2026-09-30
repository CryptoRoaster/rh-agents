"""Observe, by exact locator, every market a risk request is about to read.

SENTINEL judges a request on recorded observations no older than its own bound
(thirty seconds by default). The run-start acquisition records them once, at
the beginning of a pass; the specialists, a trigger and ANCHOR's quotes then
take their time, and by the moment a case is ready to be asked about, what was
recorded at the start has aged past that bound. This stage closes that gap and
nothing else: immediately before a new risk request — and again before a
request is re-asked after a workflow source refresh — it observes the markets
that request will read and records them.

**Which markets.** The case's own market, and the market of every open position
SENTINEL values for the portfolio. Nothing else: not the payment-asset reference
ANCHOR uses, not discovery, not other cases. A market needed twice is read once.

**How.** The same transport, network directory, `GeckoTerminalAdapter`,
identity checks and `MarketRecorder` the run-start acquisition uses
(`src.runner.acquisition`), with a budget of its own. A market is asked about by
its stored pool locator only and must come back as exactly the identity that was
stored; no symbol, name or address text is searched, and no pool is substituted.

**Fail closed.** If any market the request needs cannot be shown fresh, the
request is not sent. A provider failure never falls back on an older reading, a
write whose outcome is unknown stops the request, and a provider answer that
replays an event already stored makes nothing fresher: after recording, the
latest stored observation of every market is checked against SENTINEL's own
bound on its real source instants. Nothing is re-dated, and a liquidity the
provider reported as unknown stays unknown.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.core.models import RiskLimits
from src.data.repository import aware
from src.data.tables import MarketObservationRow as Row
from src.markets.geckoterminal.adapter import MAX_POOLS_PER_REQUEST, GeckoTerminalAdapter
from src.markets.geckoterminal.errors import BudgetError, ProviderError
from src.markets.geckoterminal.networks import (
    Chain,
    NetworkDirectory,
    VerifiedNetworkRegistry,
    selected_chains,
)
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import MarketIdentity
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.orchestration.workflow.models import TradeCase
from src.runner.acquisition import (
    RunDeadline,
    Window,
    position_markets,
    record_observed,
    unaddressable,
)
from src.runner.models import AcquisitionOutcome, PreRiskRefresh

# How many open holdings may be considered. A portfolio larger than this cannot
# be shown fresh by this stage, and the request is then not sent.
MAX_POSITION_MARKETS = 20


class PreRiskReason(StrEnum):
    """Why a risk request was not sent. The first thing wrong, in these words."""

    MARKET_IDENTITY_UNKNOWN = "MARKET_IDENTITY_UNKNOWN"
    POOL_LOCATOR_UNKNOWN = "POOL_LOCATOR_UNKNOWN"
    CHAIN_NOT_CONFIGURED = "CHAIN_NOT_CONFIGURED"
    MARKET_NOT_RETURNED = "MARKET_NOT_RETURNED"
    MARKET_IDENTITY_MISMATCH = "MARKET_IDENTITY_MISMATCH"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    TIME_BUDGET_REACHED = "TIME_BUDGET_REACHED"
    REQUEST_BUDGET_REACHED = "REQUEST_BUDGET_REACHED"
    RECORD_OUTCOME_UNKNOWN = "RECORD_OUTCOME_UNKNOWN"
    # Observed and recorded (or replayed), and still older than SENTINEL's bound.
    MARKET_STILL_STALE = "MARKET_STILL_STALE"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"


# How the shared locator rules name a market this stage cannot ask about.
ADDRESSING: dict[str, PreRiskReason] = {
    "PROVIDER_NOT_CONFIGURED": PreRiskReason.MARKET_IDENTITY_UNKNOWN,
    "CHAIN_NOT_CONFIGURED": PreRiskReason.CHAIN_NOT_CONFIGURED,
    "NETWORK_NOT_SUPPORTED": PreRiskReason.CHAIN_NOT_CONFIGURED,
    "POOL_LOCATOR_UNKNOWN": PreRiskReason.POOL_LOCATOR_UNKNOWN,
    "FIXTURE_MARKET": PreRiskReason.MARKET_IDENTITY_UNKNOWN,
}


@dataclass(frozen=True)
class PreRiskLimits:
    max_requests: int
    max_seconds: int


@dataclass
class _Tally:
    attempted: int = 0
    recorded: int = 0
    unchanged: int = 0
    refused: int = 0
    failed: int = 0
    # How each chain's provider network was resolved: by the provider in this
    # refresh, or from the run's registry of bindings already validated.
    networks_by_provider: int = 0
    networks_from_cache: int = 0
    reason: PreRiskReason | None = None

    def fail(self, reason: PreRiskReason) -> None:
        if self.reason is None:
            self.reason = reason


class PreRiskMarketRefresh:
    """One bounded observation of a risk request's markets, then a freshness gate."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        markets: MarketReader,
        risk_limits: RiskLimits,
        limits: PreRiskLimits,
        *,
        clock: Clock | None = None,
        http: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._markets = markets
        self._risk = risk_limits
        self._limits = limits
        self._clock = clock if clock is not None else SystemClock()
        self._http = http
        self._configured = settings
        # The provider's own budgets, tightened to this stage's, never widened.
        self._settings = settings.model_copy(
            update={
                "geckoterminal_max_requests": min(
                    settings.geckoterminal_max_requests, limits.max_requests
                ),
                "geckoterminal_max_http_attempts": min(
                    settings.geckoterminal_max_http_attempts, limits.max_requests * 2
                ),
                "geckoterminal_total_timeout_seconds": min(
                    settings.geckoterminal_total_timeout_seconds, limits.max_seconds
                ),
            }
        )

    async def refresh(
        self,
        trade_case: TradeCase,
        deadline: RunDeadline,
        *,
        networks: VerifiedNetworkRegistry | None = None,
    ) -> PreRiskRefresh:
        """Observe the request's markets, record them, and judge their freshness.

        `networks` is the run's registry of chain bindings validated earlier in
        the same pass — normally by the run-start acquisition. A chain found
        there costs no `/networks` request; a chain that is not is validated
        here the ordinary way, under this stage's own budget, and fails closed
        when that budget ends. Nothing is ever assumed into it.
        """
        tally = _Tally()
        window = Window(self._limits.max_seconds, deadline)
        transport: GeckoTerminalTransport | None = None
        pairs: list[str] = []
        try:
            chains = {chain.name: chain for chain in selected_chains(self._configured)}
            if window.expired:
                # Out of time before the first question: nothing is read, asked
                # or written, and no request follows.
                tally.fail(PreRiskReason.TIME_BUDGET_REACHED)
                return self._reading(tally, transport, pairs)
            needed = await asyncio.wait_for(
                self._needed(trade_case, tally), timeout=max(0.001, window.remaining)
            )
            pairs = [identity.pair_id for identity in needed]
            if tally.reason is None:
                for identity in needed:
                    code = unaddressable(identity, chains)
                    if code is not None:
                        tally.refused += 1
                        tally.fail(ADDRESSING[code])
            if tally.reason is None and needed:
                transport = GeckoTerminalTransport(
                    self._settings, transport=self._http, clock=self._clock
                )
                await self._observe(needed, chains, transport, tally, window, networks)
            if tally.reason is None:
                await self._fresh_enough(needed, tally)
        except ProviderError:
            tally.fail(PreRiskReason.CHAIN_NOT_CONFIGURED)
        except TimeoutError:
            tally.fail(PreRiskReason.TIME_BUDGET_REACHED)
        except (SQLAlchemyError, OSError):
            tally.fail(PreRiskReason.DATABASE_UNAVAILABLE)
        finally:
            if transport is not None:
                await transport.__aexit__(None, None, None)
        return self._reading(tally, transport, pairs)

    @staticmethod
    def _reading(
        tally: _Tally, transport: GeckoTerminalTransport | None, pairs: list[str]
    ) -> PreRiskRefresh:
        return PreRiskRefresh(
            ready=tally.reason is None,
            required_markets=len(pairs),
            reason=None if tally.reason is None else tally.reason.value,
            attempted=tally.attempted,
            recorded=tally.recorded,
            unchanged=tally.unchanged,
            refused=tally.refused,
            failed=tally.failed,
            provider_requests=0 if transport is None else transport.logical_requests,
            network_resolution_provider=tally.networks_by_provider,
            network_resolution_cache=tally.networks_from_cache,
            markets=tuple(pairs[:8]),
        )

    async def _needed(self, trade_case: TradeCase, tally: _Tally) -> list[MarketIdentity]:
        """The case's market, then every open holding's, each exactly once."""
        needed: dict[str, MarketIdentity] = {trade_case.market.pair_id: trade_case.market}
        # One more than the bound is read, so a portfolio larger than it is
        # refused rather than silently observed in part.
        holdings = await position_markets(self._sessions, self._markets, MAX_POSITION_MARKETS + 1)
        if len(holdings) > MAX_POSITION_MARKETS:
            tally.refused += len(holdings)
            tally.fail(PreRiskReason.REQUEST_BUDGET_REACHED)
        for _, _, identity, _ in holdings[:MAX_POSITION_MARKETS]:
            if identity is None:
                # A holding SENTINEL must value, whose market cannot be named.
                tally.refused += 1
                tally.fail(PreRiskReason.MARKET_IDENTITY_UNKNOWN)
                continue
            needed.setdefault(identity.pair_id, identity)
        return list(needed.values())

    async def _observe(
        self,
        needed: list[MarketIdentity],
        chains: dict[str, Chain],
        transport: GeckoTerminalTransport,
        tally: _Tally,
        window: Window,
        networks: VerifiedNetworkRegistry | None,
    ) -> None:
        directory = NetworkDirectory(transport, self._settings, registry=networks)
        try:
            await self._batches(needed, chains, transport, directory, tally, window)
        finally:
            tally.networks_by_provider = directory.resolved_by_provider
            tally.networks_from_cache = directory.resolved_from_cache

    async def _batches(
        self,
        needed: list[MarketIdentity],
        chains: dict[str, Chain],
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        tally: _Tally,
        window: Window,
    ) -> None:
        """One exact-locator read per chain, then one recording per market."""
        recorder = MarketRecorder(self._sessions, clock=self._clock)
        for name in sorted({identity.chain for identity in needed}):
            batch = [identity for identity in needed if identity.chain == name]
            if len(batch) > MAX_POOLS_PER_REQUEST:
                tally.refused += len(batch)
                tally.fail(PreRiskReason.REQUEST_BUDGET_REACHED)
                return
            adapter = GeckoTerminalAdapter(
                transport, directory, chains[name], self._settings, clock=self._clock
            )
            tally.attempted += len(batch)
            try:
                confirmed = await asyncio.wait_for(
                    adapter.observe(
                        tuple(
                            identity.pool_locator
                            for identity in batch
                            if identity.pool_locator is not None
                        )
                    ),
                    timeout=max(0.001, window.remaining),
                )
            except TimeoutError:
                tally.failed += len(batch)
                tally.fail(PreRiskReason.TIME_BUDGET_REACHED)
                return
            except BudgetError:
                tally.failed += len(batch)
                tally.fail(PreRiskReason.REQUEST_BUDGET_REACHED)
                return
            except ProviderError:
                tally.failed += len(batch)
                tally.fail(PreRiskReason.PROVIDER_FAILED)
                return
            answered = {pair.pair_id: pair for pair in confirmed}
            for identity in batch:
                pair = answered.get(identity.pair_id)
                if pair is None:
                    tally.refused += 1
                    tally.fail(PreRiskReason.MARKET_NOT_RETURNED)
                    continue
                outcome, reason = await record_observed(
                    adapter, pair, identity, recorder, window.remaining
                )
                if outcome is AcquisitionOutcome.RECORDED:
                    tally.recorded += 1
                elif outcome is AcquisitionOutcome.UNCHANGED:
                    tally.unchanged += 1
                elif outcome is AcquisitionOutcome.UNKNOWN:
                    tally.failed += 1
                    tally.fail(PreRiskReason.RECORD_OUTCOME_UNKNOWN)
                    return
                else:
                    tally.refused += 1
                    tally.fail(
                        PreRiskReason.MARKET_IDENTITY_MISMATCH
                        if reason == "MARKET_IDENTITY_MISMATCH"
                        else PreRiskReason.MARKET_NOT_RETURNED
                    )

    async def _fresh_enough(self, needed: list[MarketIdentity], tally: _Tally) -> None:
        """Every needed market's newest stored event, on its real source instants.

        A replayed provider event is not a new observation: whatever the read
        returned, what counts is the source time of what is actually stored,
        against SENTINEL's own bound. The newest event is judged whether or not
        it is usable — a fresh reading whose liquidity the provider could not
        state is fresh and stays unknown, and the risk request's own readiness
        check refuses it by that name — and an older usable event never stands
        in for a newer one.
        """
        now = self._clock.now()
        bound = timedelta(seconds=self._risk.max_snapshot_age_seconds)
        async with self._sessions() as session:
            for identity in needed:
                row = (
                    await session.execute(
                        select(Row.observed_at, Row.freshness_at)
                        .where(
                            Row.pair_id == identity.pair_id,
                            Row.provider == identity.provider,
                            Row.is_fixture.is_(False),
                        )
                        .order_by(Row.observed_at.desc(), Row.recorded_at.desc(), Row.id.desc())
                        .limit(1)
                    )
                ).first()
                if row is None:
                    tally.fail(PreRiskReason.MARKET_STILL_STALE)
                    return
                observed, freshness = aware(row.observed_at), aware(row.freshness_at)
                if observed > now or now - freshness > bound:
                    tally.fail(PreRiskReason.MARKET_STILL_STALE)
                    return
