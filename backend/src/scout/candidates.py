"""PROMOTABLE watches as the full PAPER run's candidate source.

With the early scout enabled, COMMANDER intake stops reading "every fresh new
pool" and reads only the fresh markets behind PROMOTABLE watches. That is the
whole change: COMMANDER still decides whether a case may be opened, with its
rules untouched.

**Filter before limit.** Intake reads at most `max_candidates` candidates, and a
small budget can be as small as one. A PROMOTABLE market that COMMANDER would
refuse anyway — one with a live case, or one a RISK_REJECTED or EXECUTED case
already spoke for — must not take that single slot from an eligible one. So
the source asks COMMANDER's own questions, through the same functions intake
uses, before it counts. It adds no rule of its own: EXPIRED and CANCELLED stay
successor-capable exactly as intake treats them.

**Order.** Discovery order (`first_seen_at`, then pair). Never liquidity,
volume, price or an ORBIT classification.

**A scout assessment is not evidence.** Nothing here reads assessment history.
A case formed from a PROMOTABLE watch starts with no ORBIT evidence and gets its
own ORBIT task on its own fresh input.
"""

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import ProviderError
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory, selected_chains
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import MarketCandidate, MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder, ObservationConflict, record_pair_reporting
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.commander.intake import active_case_exists, market_barring
from src.scout.models import DiscoveryWatch
from src.scout.repository import SyncResult, WatchRepository

# How many PROMOTABLE watches one intake read may walk past while looking for
# eligible ones. A bound on the scan, not on what may be opened.
SCAN_LIMIT = 100


@dataclass(frozen=True)
class PromotedWatchCandidates:
    """COMMANDER's `MarketCandidateSource`, restricted to PROMOTABLE watches."""

    sessions: async_sessionmaker[AsyncSession]
    markets: MarketReader
    watches: WatchRepository

    async def eligible(self, *, include_fixtures: bool = False) -> tuple[DiscoveryWatch, ...]:
        """PROMOTABLE watches COMMANDER would not refuse for an existing case."""
        found: list[DiscoveryWatch] = []
        for watch in await self.watches.promotable(SCAN_LIMIT):
            if watch.is_fixture and not include_fixtures:
                continue
            async with self.sessions() as session:
                if await active_case_exists(session, watch.pair_id):
                    continue
                if await market_barring(session, watch.pair_id) is not None:
                    continue
            found.append(watch)
        return tuple(found)

    async def candidates(
        self, *, include_fixtures: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[MarketCandidate, ...]:
        found: list[MarketCandidate] = []
        for watch in await self.eligible(include_fixtures=include_fixtures):
            snapshot = await self.markets.latest(watch.pair_id, include_fixtures=include_fixtures)
            if snapshot is None:
                # Not fresh, or not available. Never an older reading instead.
                continue
            found.append(MarketCandidate.from_snapshot(snapshot))
        return tuple(found[offset : offset + limit])

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        return await self.markets.latest(identity, include_fixtures=include_fixtures)


@dataclass(frozen=True)
class PromotionRefresh:
    """Re-observe eligible PROMOTABLE watches by exact locator before intake.

    A PROMOTABLE market is by definition no longer on the new-pool list, so its
    last reading is usually older than intake accepts. This asks the provider
    about exactly those pools — no discovery scan — within the scout's refresh
    budget. It records observations and nothing else; whether a case is opened
    is still intake's decision on what was recorded.
    """

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    source: PromotedWatchCandidates
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    http: httpx.AsyncBaseTransport | None = None

    async def execute(self) -> tuple[int, str | None]:
        """How many markets were re-observed, and why the stage stopped, if it did."""
        budget = self.settings.early_scout_max_refresh_markets_per_run
        if budget <= 0:
            return 0, None
        if self.settings.market_provider != "geckoterminal":
            return 0, "PROMOTION_REFRESH_PROVIDER_NOT_CONFIGURED"
        if self.pause is None or await self.pause.system_paused():
            # Unknown is not permission to call anybody.
            return 0, "SYSTEM_STOPPED"
        stale: list[DiscoveryWatch] = []
        for watch in await self.source.eligible():
            if await self.source.markets.latest(watch.pair_id) is not None:
                continue
            if watch.market.pool_locator is None:
                continue
            stale.append(watch)
        wanted = stale[:budget]
        if not wanted:
            return 0, None
        refreshed = 0
        configured = {item.name for item in selected_chains(self.settings)}
        async with GeckoTerminalTransport(
            self.settings, transport=self.http, clock=self.clock
        ) as transport:
            directory = NetworkDirectory(transport, self.settings)
            recorder = MarketRecorder(self.sessions, clock=self.clock)
            by_chain: dict[str, list[DiscoveryWatch]] = {}
            for watch in wanted:
                by_chain.setdefault(watch.chain, []).append(watch)
            for chain_name, batch in by_chain.items():
                chain = CHAINS.get(chain_name)
                if chain is None or chain_name not in configured:
                    continue
                adapter = GeckoTerminalAdapter(
                    transport, directory, chain, self.settings, clock=self.clock
                )
                try:
                    confirmed = await adapter.observe(
                        tuple(
                            item.market.pool_locator for item in batch if item.market.pool_locator
                        )
                    )
                except ProviderError as error:
                    return refreshed, error.code.upper()
                answered = {pair.pair_id: pair for pair in confirmed}
                for watch in batch:
                    pair = answered.get(watch.pair_id)
                    if pair is None:
                        continue
                    try:
                        written = await record_pair_reporting(adapter, pair, recorder)
                    except (ObservationConflict, ValueError):
                        continue
                    result = await self.source.watches.sync(
                        written.observation, now=self.clock.now(), allow_create=False
                    )
                    if result is not SyncResult.CONFLICT:
                        refreshed += 1
        return refreshed, None
