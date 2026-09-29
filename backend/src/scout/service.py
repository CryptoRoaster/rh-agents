"""One bounded scout run: discover, watch, re-observe, review, check maturity.

    python -m src.runner.main --scout-once

What one run does, in order
---------------------------

1. **Refuse or stop** before anything is asked: the scout must be enabled, the
   market provider must be the real one, ORBIT must be composable, and a durable
   system stop — or a stop that cannot be read — ends the run with no call made.
2. **Bootstrap** a bounded number of market streams recorded before the scout
   existed. Database only.
3. **Discover**: one `new_pools` read per configured chain. Every normalized pool
   is recorded through the production recorder and attached to its watch; a
   provider-identity rejection creates nothing. There is no size, liquidity,
   volume or trend filter anywhere in this path.
4. **Re-observe** due watches whose latest reading is no longer fresh, by their
   stored pool locator and nothing else, within the refresh budget.
5. **Review** due watches with ORBIT — the same evaluator, prompt, validator and
   digest the TradeCase worker uses — at most one review per watch and at most
   the review budget per run, on the current fresh reading.
6. **Check history** for watches that reached a history checkpoint: one OHLCV
   read and VECTOR's own `assess(...)`, never a model call.

What it never does
------------------

It never opens a TradeCase, never asks SENTINEL, never sizes, fills or signs,
and never turns a scout assessment into case evidence. A PROMOTABLE watch is
only a candidate a later, separate full PAPER run may consider under COMMANDER's
unchanged rules.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.orbit.context import (
    OrbitContextUnavailable,
    evaluation_input,
    orbit_input_digest,
)
from src.agents.orbit.evaluator import OrbitEvaluator
from src.agents.orbit.models import (
    ORBIT_OUTPUT_SCHEMA_VERSION,
    OrbitClassification,
    OrbitEvaluationInput,
)
from src.agents.orbit.prompt import ORBIT_PROMPT_HASH, ORBIT_PROMPT_VERSION
from src.agents.orbit.validation import OrbitValidationError
from src.agents.vector.sufficiency import VectorMarketDataSufficiency, assess
from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.data.database import connect
from src.fast_reasoning.provider import FastAssessmentProvider
from src.markets.geckoterminal.adapter import MAX_POOLS_PER_REQUEST, GeckoTerminalAdapter
from src.markets.geckoterminal.errors import IdentityError, ProviderError
from src.markets.geckoterminal.networks import CHAINS, Chain, NetworkDirectory, selected_chains
from src.markets.geckoterminal.ohlcv import GeckoTerminalOhlcvSource
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.history import MarketHistory, MarketHistorySource, MarketHistoryUnavailable
from src.markets.models import Availability, MarketSnapshot
from src.markets.recorder import MarketRecorder, ObservationConflict, record_pair_reporting
from src.orchestration.commander.context import (
    AccountPauseReader,
    SystemPausePort,
    SystemPauseUnavailable,
)
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure
from src.reasoning.provider import ReasoningProvider
from src.runner.models import ConfigurationRefused
from src.scout.budget import OrbitBudget, SlotPacing, utc_day
from src.scout.models import (
    DiscoveryWatch,
    ModelFailureCount,
    ScoutReview,
    ScoutSummary,
    WatchAssessment,
)
from src.scout.outcomes import (
    SAMPLE_AGGREGATE,
    SAMPLE_STEP,
    SAMPLE_TIMEFRAME,
    BarStore,
    Candidate,
    OutcomeSampler,
    OutcomeStore,
    OutcomeTally,
    SamplerSkip,
    SamplerStop,
    classify_history_failure,
)
from src.scout.policy import EARLY_SCOUT_V2, EarlyScoutPolicy, WatchStatus
from src.scout.repository import SyncResult, WatchRepository, refreshed_identity_contradicts
from src.scout.runs import ScoutRunRepository
from src.scout.shadow import FastAssessmentStore, ShadowTally, ShadowTriage

ScoutReading = ScoutSummary | ConfigurationRefused

# One scout run at a time across every process on this database.
RUN_LOCK = "rh-agents:early-scout"


@dataclass(frozen=True)
class ScoutPorts:
    """The outside world, as one scout run is allowed to see it.

    Built from settings by `scout_ports_from_settings`. A test replaces a field
    in its own code: a scripted model, the provider's HTTP responses, prepared
    OHLCV series. A configuration can produce none of those substitutes.
    """

    reasoning: ReasoningProvider | None = None
    reasoning_unavailable: str = "REASONING_PROVIDER_NOT_CONFIGURED"
    market_http: httpx.AsyncBaseTransport | None = None
    # Replaces the GeckoTerminal OHLCV source for every chain when supplied.
    history: MarketHistorySource | None = None
    pause: SystemPausePort | None = None
    # Shadow fast assessments of new watches (JEV). None means none are asked;
    # nothing else in the run depends on it either way.
    fast: FastAssessmentProvider | None = None


def scout_ports_from_settings(settings: Settings) -> ScoutPorts:
    """The models the scout may ask. Constructing a client opens no connection."""
    ports = _reasoning_ports(settings)
    if settings.fast_reasoning_provider == "jev" and settings.typesafe_api_key.get_secret_value():
        from src.fast_reasoning.jev import JevProvider

        ports = replace(
            ports,
            fast=JevProvider(
                api_key=settings.typesafe_api_key,
                requested_model=settings.jev_model,
                base_url=settings.jev_base_url,
                timeout_seconds=float(settings.jev_timeout_seconds),
            ),
        )
    return ports


def _reasoning_ports(settings: Settings) -> ScoutPorts:
    if settings.reasoning_provider == "anthropic":
        from src.reasoning.anthropic_provider import AnthropicReasoningProvider

        return ScoutPorts(
            reasoning=AnthropicReasoningProvider(
                api_key=settings.anthropic_api_key,
                model=settings.reasoning_model,
                effort=settings.reasoning_effort,
            )
        )
    if settings.reasoning_provider == "codex":
        from src.codex_reasoning.provider import codex_provider_from

        codex = codex_provider_from(settings)
        if codex is None:
            return ScoutPorts(reasoning_unavailable="REASONING_PROVIDER_CLI_NOT_FOUND")
        return ScoutPorts(reasoning=codex)
    if settings.reasoning_provider == "fake":
        # A scripted model is test code, never a configuration.
        return ScoutPorts(reasoning_unavailable="REASONING_PROVIDER_NOT_COMPOSABLE")
    return ScoutPorts()


def refuse_scout(settings: Settings, ports: ScoutPorts) -> ConfigurationRefused | None:
    """Every reason a scout run may not start, checked before anything is asked.

    Deliberately no trading-mode requirement: the scout holds no trading
    authority, so it is as safe under OBSERVE as under PAPER.
    """
    if not settings.early_scout_enabled:
        return ConfigurationRefused(reason="EARLY_SCOUT_NOT_ENABLED")
    if settings.market_provider != "geckoterminal":
        return ConfigurationRefused(reason="EARLY_SCOUT_PROVIDER_NOT_GECKOTERMINAL")
    if ports.reasoning is None:
        return ConfigurationRefused(
            reason="EARLY_SCOUT_REASONING_UNAVAILABLE", detail=ports.reasoning_unavailable
        )
    try:
        selected_chains(settings)
    except ProviderError as error:
        return ConfigurationRefused(reason="EARLY_SCOUT_CHAINS_INVALID", detail=error.code.upper())
    return None


_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
UNCLASSIFIED = "UNCLASSIFIED"


def round_robin[T](groups: list[list[T]]) -> list[T]:
    """One item from each group in turn, keeping each group's own order."""
    ordered: list[T] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return ordered


def safe_code(value: str) -> str:
    """A failure reason as a code, or UNCLASSIFIED: never a path, message or payload."""
    return value if _SAFE_CODE.fullmatch(value) else UNCLASSIFIED


@dataclass
class Tally:
    """What the run did, accumulated as it happens rather than assembled at the end."""

    stop: str = "COMPLETED"
    bootstrapped: int = 0
    discovered: int = 0
    valid_markets: int = 0
    provider_identity_rejects: int = 0
    other_provider_rejects: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    watches_created: int = 0
    watches_updated: int = 0
    # Streams the per-run watch limit turned away (still observed and assessed).
    watches_declined: int = 0
    # New discovery candidates for JEV-0, fixed before watch allocation.
    shadow_candidates: list[MarketSnapshot] = field(default_factory=list)
    refreshed: int = 0
    watches_due_orbit: int = 0
    orbit_reviews_started: int = 0
    orbit_reviews_completed: int = 0
    classifications: dict[str, int] = field(default_factory=dict)
    watches_due_history: int = 0
    history_checks: int = 0
    vector_sufficient: int = 0
    promotable_new: int = 0
    dormant_new: int = 0
    retired_new: int = 0
    provider_failures: int = 0
    model_failures: int = 0
    # (provider, category, reason code) -> failures.
    model_failure_reasons: dict[tuple[str, str, str], int] = field(default_factory=dict)
    orbit_backlog_before: int = 0
    orbit_backlog_after: int = 0
    oldest_orbit_due_age_seconds: int | None = None
    new_watches_without_orbit_assessment: int = 0
    orbit_daily_budget: int = 0
    orbit_daily_used_before: int = 0
    orbit_daily_remaining_before: int = 0
    orbit_daily_used_after: int = 0
    orbit_daily_remaining_after: int = 0
    orbit_fresh_first_reviews_due: int = 0
    orbit_first_reviews_skipped_stale: int = 0
    orbit_follow_ups_deferred: int = 0
    orbit_slots_released: int = 0
    reviews: list[ScoutReview] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shadow: ShadowTally = field(default_factory=ShadowTally)
    outcomes: OutcomeTally = field(default_factory=OutcomeTally)

    def fail(self, code: str) -> None:
        if code not in self.errors:
            self.errors.append(code)


# How many due watches one run may walk past while looking for ones it can
# actually work on. Bounds a database read, never a provider or model call.
DUE_SCAN = 50


@dataclass
class Readings:
    """Fresh readings this run has, and what it may still spend to get more."""

    recorded: dict[str, MarketSnapshot]
    refresh_budget: int
    # Per pair: the reading, or None where this run already tried and failed.
    by_pair: dict[str, MarketSnapshot | None] = field(default_factory=dict)


class EarlyScoutCycle:
    """Coordinates one scout run. Owns no rule: the policy and the services do."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        ports: ScoutPorts | None = None,
        clock: Clock | None = None,
        policy: EarlyScoutPolicy = EARLY_SCOUT_V2,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._ports = ports if ports is not None else scout_ports_from_settings(settings)
        self._clock = clock if clock is not None else SystemClock()
        self._policy = policy
        self._watches = WatchRepository(sessions, policy=policy)
        self._budget = OrbitBudget(sessions)
        # V2 spreads the day's paid ORBIT slots evenly; V1 keeps its daily cap only.
        self._pacing = SlotPacing() if policy.fresh_first_only else None
        # Discovery and refresh freshness is ORBIT's own discovery freshness.
        self._max_input_age = timedelta(seconds=settings.orbit_input_max_age_seconds)

    async def execute(self) -> ScoutReading:
        refusal = refuse_scout(self._settings, self._ports)
        if refusal is not None:
            return refusal
        async with self._exclusive() as held:
            if not held:
                # Another scout run holds the lock. Not a fault and not a run: it
                # made no call and leaves no history row.
                now = self._clock.now().isoformat()
                return ScoutSummary(
                    policy_version=self._policy.version,
                    started_at=now,
                    finished_at=now,
                    stop="ALREADY_RUNNING",
                )
            return await self._run()

    @asynccontextmanager
    async def _exclusive(self) -> AsyncIterator[bool]:
        """At most one scout run at a time, whoever starts it.

        A session-level PostgreSQL advisory lock on a connection held for the
        whole run: a scheduled run that starts while a manual one is still going
        gives way instead of reviewing the same due watches twice. The lock is
        released on every path and dies with the connection if the process does.
        The metadata-only SQLite test engine has no advisory locks.
        """
        async with self._sessions() as session:
            if session.get_bind().dialect.name != "postgresql":
                yield True
                return
            held = bool(
                await session.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": RUN_LOCK}
                )
            )
            try:
                yield held
            finally:
                if held:
                    await session.scalar(
                        text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": RUN_LOCK}
                    )
                    await session.commit()

    async def _run(self) -> ScoutSummary:
        started = self._clock.now()
        tally = Tally()
        transport: GeckoTerminalTransport | None = None
        try:
            if await self._permitted(tally):
                tally.bootstrapped = await self._watches.bootstrap(
                    started, self._settings.early_scout_max_bootstrap_streams
                )
                transport = GeckoTerminalTransport(
                    self._settings, transport=self._ports.market_http, clock=self._clock
                )
                directory = NetworkDirectory(transport, self._settings)
                recorder = MarketRecorder(self._sessions, clock=self._clock)
                fresh = await self._discover(transport, directory, recorder, tally)
                await self._scheduled(transport, directory, recorder, fresh, tally)
                # Last, after every review and check: shadow work cannot change
                # what the run already decided, nor delay it.
                await self._shadow(tally)
                # Outcome labels, after everything else and on their own budget.
                await self._outcomes(tally)
        except SystemPauseUnavailable:
            tally.stop = "SYSTEM_STOPPED"
            tally.fail("SYSTEM_STOP_UNREADABLE")
        except (SQLAlchemyError, OSError):
            tally.fail("DATABASE_UNAVAILABLE")
        except asyncio.CancelledError:
            raise
        finally:
            if transport is not None:
                await transport.__aexit__(None, None, None)
        summary = self._summary(started, tally, transport)
        run_id = uuid4()
        try:
            await ScoutRunRepository(self._sessions).record(
                run_id, started, self._clock.now(), summary
            )
        except (SQLAlchemyError, OSError):
            return summary.model_copy(
                update={"errors": (*summary.errors, "RUN_HISTORY_UNAVAILABLE")[:16]}
            )
        return summary.model_copy(update={"run_id": run_id})

    async def _freeze_candidates(self, ordered: list[MarketSnapshot], tally: Tally) -> None:
        """Every new valid stream of this discovery, in the neutral chain order.

        New means never observed before this run and without a watch. Only
        looked up when a fast provider is configured; the lookup reads and
        never writes, and a failure empties the shadow set instead of the run.
        """
        if self._ports.fast is None:
            return
        store = FastAssessmentStore(self._sessions)
        try:
            for snapshot in ordered:
                if await store.is_new_candidate(snapshot):
                    tally.shadow_candidates.append(snapshot)
        except (SQLAlchemyError, OSError):
            tally.shadow_candidates.clear()
            tally.shadow.failure("SHADOW_STORE_UNAVAILABLE")

    async def _shadow(self, tally: Tally) -> None:
        """One JEV-0 shadow assessment per new discovery candidate. Never raises.

        Runs after every review and check. The candidates were fixed before
        watch allocation, so declined streams are assessed as well; nothing
        older is backfilled; every outcome, including a store that cannot be
        written, stays in the shadow tally instead of the run's errors.
        """
        if self._ports.fast is None or not tally.shadow_candidates:
            return
        try:
            triage = ShadowTriage(
                store=FastAssessmentStore(self._sessions),
                provider=self._ports.fast,
                per_run=self._settings.jev_max_assessments_per_run,
                per_day=self._settings.jev_max_assessments_per_day,
            )
            result = await triage.run(tally.shadow_candidates, self._clock.now, safe_code)
            result.failed += tally.shadow.failed
            for code, count in tally.shadow.failure_codes.items():
                result.failure_codes[code] = result.failure_codes.get(code, 0) + count
            tally.shadow = result
        except (SQLAlchemyError, OSError):
            tally.shadow.failure("SHADOW_STORE_UNAVAILABLE")

    async def _outcomes(self, tally: Tally) -> None:
        """Label eligible discovery streams within the sampler's own budget. Never raises."""
        settings = self._settings
        if not settings.outcome_sampler_enabled or settings.outcome_max_requests_per_run <= 0:
            return
        chains = selected_chains(settings)
        budget = settings.model_copy(
            update={"geckoterminal_max_requests": settings.outcome_max_requests_per_run}
        )
        transport = GeckoTerminalTransport(
            budget, transport=self._ports.market_http, clock=self._clock
        )
        directory = NetworkDirectory(transport, budget)
        recorder = MarketRecorder(self._sessions, clock=self._clock)
        now = self._clock.now()

        async def fetch(candidate: Candidate) -> MarketHistory:
            chain = CHAINS.get(candidate.key[1])
            if chain is None or chain not in chains:
                raise SamplerSkip("CHAIN_NOT_CONFIGURED", final=False)
            source = self._history_source(chain, transport, directory)
            span = now - candidate.first_seen
            bars = min(1000, int(span / SAMPLE_STEP) + 2)
            try:
                return await source.history(
                    candidate.reference.pair.market_identity,
                    timeframe=SAMPLE_TIMEFRAME,
                    aggregate=SAMPLE_AGGREGATE,
                    bars=bars,
                )
            except MarketHistoryUnavailable as error:
                raise classify_history_failure(error.reason_code) from None

        async def observe(candidates: list[Candidate]) -> dict[str, Decimal]:
            """Current liquidity, one batched re-observation per chain (recorded as usual)."""
            found: dict[str, Decimal] = {}
            by_chain: dict[str, list[Candidate]] = {}
            for item in candidates:
                if item.reference.pair.pool_locator is not None:
                    by_chain.setdefault(item.key[1], []).append(item)
            for chain_name, batch in by_chain.items():
                chain = CHAINS.get(chain_name)
                if chain is None or chain not in chains:
                    continue
                adapter = GeckoTerminalAdapter(
                    transport, directory, chain, budget, clock=self._clock
                )
                for start in range(0, len(batch), MAX_POOLS_PER_REQUEST):
                    part = batch[start : start + MAX_POOLS_PER_REQUEST]
                    try:
                        pairs = await adapter.observe(
                            tuple(
                                item.reference.pair.market_identity.pool_locator
                                for item in part
                                if item.reference.pair.market_identity.pool_locator is not None
                            )
                        )
                    except ProviderError as error:
                        raise SamplerStop(error.code.upper()) from None
                    for pair in pairs:
                        try:
                            written = await record_pair_reporting(adapter, pair, recorder)
                        except (ObservationConflict, ValueError):
                            continue
                        liquidity = written.observation.liquidity
                        if liquidity.status == Availability.AVAILABLE and liquidity.value_usd:
                            found[pair.pair_id] = liquidity.value_usd
            return found

        sampler = OutcomeSampler(
            bars=BarStore(self._sessions),
            store=OutcomeStore(self._sessions),
            seed=settings.outcome_sample_seed,
            max_streams=settings.outcome_max_streams_per_run,
            # One request resolves the networks; one per chain re-observes liquidity.
            max_fetches=max(0, settings.outcome_max_requests_per_run - 1 - len(chains)),
        )
        try:
            result = await sampler.run(now, fetch, observe)
            for code, count in tally.outcomes.failure_codes.items():
                result.failure_codes[code] = result.failure_codes.get(code, 0) + count
            tally.outcomes = result
        except (SQLAlchemyError, OSError):
            tally.outcomes.failure("OUTCOME_STORE_UNAVAILABLE")
        except ProviderError as error:
            tally.outcomes.failure(error.code.upper())
        finally:
            tally.outcomes.requests = transport.logical_requests
            await transport.__aexit__(None, None, None)

    async def _permitted(self, tally: Tally) -> bool:
        """A durable stop, or one that cannot be read, means nobody is asked anything."""
        pause = (
            self._ports.pause
            if self._ports.pause is not None
            else AccountPauseReader(self._sessions)
        )
        if await pause.system_paused():
            tally.stop = "SYSTEM_STOPPED"
            return False
        return True

    # -------------------------------------------------------------- discovery

    async def _discover(
        self,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        tally: Tally,
    ) -> dict[str, MarketSnapshot]:
        """One new-pool read per chain. Returns what this run recorded, by pair.

        Every valid pool is recorded. Watch creation then shares the per-run
        limit across chains round-robin, in provider order within each chain, so
        the first configured chain cannot take the whole limit. Nothing is
        ranked by liquidity, volume or price. A stream the limit turns away is
        marked declined, which keeps the recovery bootstrap from adopting it on
        the next run.
        """
        recorded: dict[str, MarketSnapshot] = {}
        by_chain: list[list[MarketSnapshot]] = []
        bounded = self._settings.model_copy(
            update={"geckoterminal_pools_per_chain": self._settings.early_scout_max_discovery_pools}
        )
        for chain in selected_chains(self._settings):
            adapter = GeckoTerminalAdapter(transport, directory, chain, bounded, clock=self._clock)
            try:
                pairs = await adapter.discover()
            except ProviderError as error:
                tally.provider_failures += 1
                tally.rejections[error.code.upper()] = (
                    tally.rejections.get(error.code.upper(), 0) + 1
                )
                continue
            finally:
                tally.discovered += adapter.discovered
                for code, count in adapter.rejections.items():
                    tally.rejections[code.upper()] = tally.rejections.get(code.upper(), 0) + count
                    if code == IdentityError.code:
                        tally.provider_identity_rejects += count
                    else:
                        tally.other_provider_rejects += count
            tally.valid_markets += len(pairs)
            batch: list[MarketSnapshot] = []
            for pair in pairs:
                snapshot = await self._record(adapter, pair, recorder, tally)
                if snapshot is None:
                    continue
                recorded[snapshot.pair.pair_id] = snapshot
                batch.append(snapshot)
            by_chain.append(batch)
        ordered = round_robin(by_chain)
        # The JEV-0 candidate set is fixed here, from discovery alone and before
        # any watch is allocated: a stream the limit turns away is a candidate
        # exactly like one that becomes a watch.
        await self._freeze_candidates(ordered, tally)
        limit = self._settings.early_scout_max_new_watches_per_run
        # A stream the limit turned away once never becomes a watch later, not by
        # bootstrap and not by being discovered again: the slots of later runs go
        # to genuinely new pools.
        declined = await self._watches.declined(
            [snapshot.pair.market_identity for snapshot in ordered]
        )
        for snapshot in ordered:
            now = self._clock.now()
            result = await self._watches.sync(
                snapshot,
                now=now,
                allow_create=tally.watches_created < limit
                and snapshot.pair.pair_id not in declined,
            )
            if result is SyncResult.CREATED:
                tally.watches_created += 1
            elif result is SyncResult.UPDATED:
                tally.watches_updated += 1
            elif result is SyncResult.SKIPPED and snapshot.pair.pair_id not in declined:
                if await self._watches.decline(
                    snapshot, now=now, reason="NOT_OPENED_AS_WATCH_DUE_TO_WATCH_LIMIT"
                ):
                    tally.watches_declined += 1
        return recorded

    async def _record(
        self, adapter: GeckoTerminalAdapter, pair: object, recorder: MarketRecorder, tally: Tally
    ) -> MarketSnapshot | None:
        try:
            written = await record_pair_reporting(adapter, pair, recorder)  # type: ignore[arg-type]
        except (ObservationConflict, ValueError):
            tally.rejections["RECORDING_REFUSED"] = tally.rejections.get("RECORDING_REFUSED", 0) + 1
            return None
        return written.observation

    # ------------------------------------------------------------ due work

    async def _scheduled(
        self,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        fresh: dict[str, MarketSnapshot],
        tally: Tally,
    ) -> None:
        """Due reviews, then due history checks, each within its own budget.

        A budget is spent only by work that actually happens. A due watch that
        cannot be worked on this run — no fresh reading and nothing that could
        re-observe it — is noted and passed over, so it can never hold the only
        slot of a one-review budget run after run.

        Stale readings are re-observed in one batch per chain before the work
        starts, so every review slot can be used at one provider request rather
        than one request per watch.
        """
        now = self._clock.now()
        # V2 closes review debt it will not serve before counting anything, so
        # "due" only ever means work this run could actually do.
        settled = await self._watches.settle_orbit_debts(now)
        tally.orbit_first_reviews_skipped_stale = settled.skipped_stale
        tally.orbit_follow_ups_deferred = settled.follow_ups_deferred
        tally.watches_due_orbit, tally.watches_due_history = await self._watches.count_due(now)
        if self._policy.fresh_first_only:
            tally.orbit_fresh_first_reviews_due = tally.watches_due_orbit
        before = await self._watches.backlog(now)
        tally.orbit_backlog_before = before.due
        tally.oldest_orbit_due_age_seconds = before.oldest_due_age_seconds
        readings = Readings(
            recorded=fresh, refresh_budget=self._settings.early_scout_max_refresh_markets_per_run
        )
        # The persistent daily bound, read before any call. A count that cannot be
        # read raises, and the run ends without asking any model.
        day = utc_day(now)
        cap = self._settings.early_scout_max_orbit_reviews_per_day
        used = await self._budget.used(day)
        tally.orbit_daily_budget = cap
        tally.orbit_daily_used_before = used
        tally.orbit_daily_remaining_before = max(0, cap - used)
        available = await self._budget.available(now, cap, self._pacing)
        if self._pacing is not None:
            tally.orbit_slots_released = min(cap, self._pacing.released(now, cap))
        # Choose the workable reviews and history checks first, then re-observe
        # every stale one in a single batch per chain: one refresh request for
        # the whole run instead of one per stage.
        stale: list[DiscoveryWatch] = []
        reviews = await self._select(
            await self._watches.due_for_orbit(now, DUE_SCAN),
            # The per-run cap stays as a safety bound; pacing decides the rest.
            min(self._settings.early_scout_max_orbit_reviews_per_run, available),
            readings,
            stale,
        )
        checks = await self._select(
            await self._watches.due_for_history(now, DUE_SCAN),
            self._settings.early_scout_max_history_checks_per_run,
            readings,
            stale,
        )
        await self._refresh(stale, readings, transport, directory, recorder, tally)
        for watch in reviews:
            reading = readings.by_pair.get(watch.pair_id)
            if reading is not None:
                await self._review(watch, reading, tally)
        for watch in checks:
            reading = readings.by_pair.get(watch.pair_id)
            if reading is not None:
                await self._check_history(watch, reading, transport, directory, tally)
        after = await self._watches.backlog(now)
        tally.orbit_backlog_after = after.due
        tally.new_watches_without_orbit_assessment = after.unreviewed
        tally.orbit_daily_used_after = await self._budget.used(day)
        tally.orbit_daily_remaining_after = max(0, cap - tally.orbit_daily_used_after)

    def _is_fresh(self, snapshot: MarketSnapshot, now: datetime) -> bool:
        return snapshot.observed_at <= now and now - snapshot.freshness_at <= self._max_input_age

    async def _select(
        self,
        due: tuple[DiscoveryWatch, ...],
        limit: int,
        readings: "Readings",
        stale: list[DiscoveryWatch],
    ) -> list[DiscoveryWatch]:
        """Choose up to `limit` workable due watches, in queue order.

        A watch is workable when it already has a fresh reading, or when it can
        be re-observed by its locator within the refresh budget; those are added
        to `stale` for the run's single refresh batch. One attempt per market per
        run: a pair already chosen for re-observation, or already failed, is
        answered from that.
        """
        now = self._clock.now()
        chosen: list[DiscoveryWatch] = []
        configured = {item.name for item in selected_chains(self._settings)}
        pending = {item.pair_id for item in stale}
        for watch in due:
            if len(chosen) >= limit:
                break
            if watch.pair_id in pending:
                chosen.append(watch)
                continue
            if watch.pair_id in readings.by_pair:
                if readings.by_pair[watch.pair_id] is not None:
                    chosen.append(watch)
                continue
            snapshot = readings.recorded.get(watch.pair_id)
            if snapshot is None:
                snapshot = await self._watches.snapshot(watch.latest_snapshot_id)
            if snapshot is not None and self._is_fresh(snapshot, now):
                readings.by_pair[watch.pair_id] = snapshot
                chosen.append(watch)
                continue
            if watch.market.pool_locator is None:
                # Never addressed by a name, a symbol or an identifier's text.
                await self._watches.note(watch.id, "POOL_LOCATOR_UNKNOWN", now)
                readings.by_pair[watch.pair_id] = None
                continue
            if watch.chain not in configured or CHAINS.get(watch.chain) is None:
                await self._watches.note(watch.id, "CHAIN_NOT_CONFIGURED", now)
                readings.by_pair[watch.pair_id] = None
                continue
            if readings.refresh_budget <= 0:
                await self._watches.note(watch.id, "REFRESH_BUDGET_REACHED", now)
                continue
            readings.refresh_budget -= 1
            stale.append(watch)
            pending.add(watch.pair_id)
            chosen.append(watch)
        return chosen

    async def _refresh(
        self,
        watches: list[DiscoveryWatch],
        readings: "Readings",
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        tally: Tally,
    ) -> None:
        """Observe exactly these pools again by their stored locators, one request per chain."""
        now = self._clock.now()
        by_chain: dict[str, list[DiscoveryWatch]] = {}
        for watch in watches:
            by_chain.setdefault(watch.chain, []).append(watch)
        for chain_name, batch in by_chain.items():
            for watch in batch:
                readings.by_pair[watch.pair_id] = None
            adapter = GeckoTerminalAdapter(
                transport, directory, CHAINS[chain_name], self._settings, clock=self._clock
            )
            locators = tuple(
                watch.market.pool_locator for watch in batch if watch.market.pool_locator
            )
            try:
                confirmed = await adapter.observe(locators)
            except ProviderError as error:
                tally.provider_failures += 1
                for watch in batch:
                    await self._watches.note(watch.id, error.code.upper(), now)
                continue
            answered = {pair.pair_id: pair for pair in confirmed}
            for watch in batch:
                pair = answered.get(watch.pair_id)
                if pair is None:
                    await self._watches.note(watch.id, "MARKET_NOT_RETURNED", now)
                    continue
                if refreshed_identity_contradicts(watch.market, pair.market_identity):
                    # The same pool now names a different market. Fail closed.
                    await self._watches.retire(watch.id, "MARKET_IDENTITY_MISMATCH", now)
                    tally.retired_new += 1
                    continue
                snapshot = await self._record(adapter, pair, recorder, tally)
                if snapshot is None:
                    continue
                result = await self._watches.sync(snapshot, now=now, allow_create=False)
                if result is SyncResult.CONFLICT:
                    await self._watches.retire(watch.id, "MARKET_IDENTITY_MISMATCH", now)
                    tally.retired_new += 1
                    continue
                tally.refreshed += 1
                readings.by_pair[watch.pair_id] = snapshot

    # ------------------------------------------------------------------ ORBIT

    async def _review(
        self, watch: DiscoveryWatch, snapshot: MarketSnapshot | None, tally: Tally
    ) -> None:
        """At most one ORBIT review of this watch in this run."""
        now = self._clock.now()
        if snapshot is None:
            return  # stays due; the reason was noted when no reading could be had
        try:
            evaluation = evaluation_input(
                snapshot,
                liquidity_floor_usd=self._settings.orbit_discovery_liquidity_floor_usd,
                max_input_age=self._max_input_age,
                now=now,
            )
        except OrbitContextUnavailable as error:
            await self._watches.note(watch.id, error.reason_code, now)
            return
        review = self._policy.orbit_review(watch.first_seen_at, now)
        if watch.next_orbit_review_at is None:
            return
        # A durable budget slot first, committed before anything else happens:
        # no reservation, no model call.
        reservation = await self._budget.reserve(
            watch.id,
            review.checkpoint_index,
            now,
            self._settings.early_scout_max_orbit_reviews_per_day,
            self._pacing,
        )
        if reservation is None:
            await self._watches.note(
                watch.id,
                "ORBIT_SLOT_NOT_RELEASED"
                if self._pacing is not None
                else "ORBIT_DAILY_BUDGET_REACHED",
                now,
            )
            return
        if not await self._watches.claim_orbit(
            watch.id,
            expected=watch.next_orbit_review_at,
            next_at=review.next_review_at,
            checkpoint_index=review.checkpoint_index,
            now=now,
        ):
            # Another run took this checkpoint; nobody was asked anything. The
            # slot stays spent — conservative, never the other way round.
            await self._budget.settle(
                reservation, status="FAILED", now=now, failure_reason="CHECKPOINT_TAKEN"
            )
            return
        tally.orbit_reviews_started += 1
        assert self._ports.reasoning is not None  # refused before the run otherwise
        evaluator = OrbitEvaluator(
            provider=self._ports.reasoning,
            max_output_tokens=self._settings.reasoning_max_output_tokens,
            timeout=timedelta(seconds=self._settings.reasoning_timeout_seconds),
        )
        base = dict(
            id=uuid4(),
            watch_id=watch.id,
            snapshot_id=snapshot.id,
            assessed_at=now,
            checkpoint_index=review.checkpoint_index,
            checkpoint_seconds=int(review.checkpoint.total_seconds()),
            policy_version=self._policy.version,
            prompt_version=ORBIT_PROMPT_VERSION,
            prompt_hash=ORBIT_PROMPT_HASH,
            output_schema_version=ORBIT_OUTPUT_SCHEMA_VERSION,
        )
        try:
            result = await evaluator.evaluate(evaluation)
        except ReasoningFailure as error:
            await self._failed(
                base,
                evaluation,
                watch,
                review,
                tally,
                reason=error.category.value,
                code=safe_code(error.reason_code),
                category=error.category.value,
            )
            await self._budget.settle(
                reservation,
                status="FAILED",
                now=self._clock.now(),
                assessment_id=base["id"],  # type: ignore[arg-type]
                failure_reason=error.category.value,
            )
            return
        except OrbitValidationError as error:
            # Contradicted output is recorded as a failure, never as an assessment.
            await self._failed(
                base,
                evaluation,
                watch,
                review,
                tally,
                reason=error.reason_code,
                code=safe_code(error.reason_code),
                category=ReasoningErrorCategory.INVALID_MODEL_OUTPUT.value,
            )
            await self._budget.settle(
                reservation,
                status="FAILED",
                now=self._clock.now(),
                assessment_id=base["id"],  # type: ignore[arg-type]
                failure_reason=error.reason_code,
            )
            return
        assessment = result.assessment
        await self._watches.append_assessment(
            WatchAssessment(
                **base,  # type: ignore[arg-type]
                status="COMPLETED",
                classification=assessment.classification.value,
                strength=assessment.strength.value,
                reason_codes=tuple(item.value for item in assessment.reason_codes),
                data_gaps=tuple(item.value for item in assessment.data_gaps),
                cited_observation_ids=assessment.cited_observation_ids,
                summary=assessment.summary,
                input_digest=result.input_digest,
                reasoning_provider=result.model.provider,
                reasoning_model=result.model.model,
                reasoning_effort=result.model.effort,
                reported_effort=result.model.reported_effort,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                latency_ms=result.usage.latency_ms,
            )
        )
        await self._budget.settle(
            reservation,
            status="COMPLETED",
            now=self._clock.now(),
            assessment_id=base["id"],  # type: ignore[arg-type]
        )
        tally.orbit_reviews_completed += 1
        key = assessment.classification.value
        tally.classifications[key] = tally.classifications.get(key, 0) + 1
        tally.reviews.append(
            ScoutReview(
                watch_id=watch.id,
                pair_id=watch.pair_id,
                watch_age_seconds=watch.age_seconds(now),
                checkpoint_seconds=base["checkpoint_seconds"],  # type: ignore[arg-type]
                status="COMPLETED",
                classification=assessment.classification.value,
                strength=assessment.strength.value,
                reason_codes=tuple(item.value for item in assessment.reason_codes),
                data_gaps=tuple(item.value for item in assessment.data_gaps),
                next_review_at=None
                if review.next_review_at is None
                else review.next_review_at.isoformat(),
            )
        )

    async def _failed(
        self,
        base: dict[str, object],
        evaluation: OrbitEvaluationInput,
        watch: DiscoveryWatch,
        review: object,
        tally: Tally,
        *,
        reason: str,
        code: str,
        category: str,
    ) -> None:
        """Record a failed review with its category and its sanitised reason code."""
        now = self._clock.now()
        assert self._ports.reasoning is not None  # a review only runs with a provider
        provider = self._ports.reasoning.name
        await self._watches.append_assessment(
            WatchAssessment(
                **base,  # type: ignore[arg-type]
                status="FAILED",
                failure_reason=reason,
                failure_reason_code=code,
                reasoning_provider=provider,
                input_digest=orbit_input_digest(evaluation),
            )
        )
        tally.model_failures += 1
        key = (provider, category, code)
        tally.model_failure_reasons[key] = tally.model_failure_reasons.get(key, 0) + 1
        next_at = getattr(review, "next_review_at", None)
        tally.reviews.append(
            ScoutReview(
                watch_id=watch.id,
                pair_id=watch.pair_id,
                watch_age_seconds=watch.age_seconds(now),
                checkpoint_seconds=base["checkpoint_seconds"],  # type: ignore[arg-type]
                status="FAILED",
                failure_reason=reason,
                failure_reason_code=code,
                next_review_at=None if next_at is None else next_at.isoformat(),
            )
        )

    # ---------------------------------------------------------------- history

    def _history_source(
        self, chain: Chain, transport: GeckoTerminalTransport, directory: NetworkDirectory
    ) -> MarketHistorySource:
        if self._ports.history is not None:
            return self._ports.history
        return GeckoTerminalOhlcvSource(
            transport, directory, chain, self._settings, clock=self._clock
        )

    async def _check_history(
        self,
        watch: DiscoveryWatch,
        snapshot: MarketSnapshot | None,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        tally: Tally,
    ) -> None:
        """One structural VECTOR check under the unchanged VECTOR policy."""
        now = self._clock.now()
        if snapshot is None:
            return  # promotion needs a current reading; the check stays due
        chain = CHAINS.get(watch.chain)
        if chain is None:
            await self._watches.note(watch.id, "CHAIN_NOT_CONFIGURED", now)
            return
        vector = self._policy.vector
        source = self._history_source(chain, transport, directory)
        try:
            history = await source.history(
                watch.market,
                timeframe=vector.history_timeframe,
                aggregate=vector.history_aggregate,
                bars=vector.history_bars,
            )
        except MarketHistoryUnavailable as error:
            tally.provider_failures += 1
            await self._watches.note(watch.id, error.reason_code, now)
            return
        tally.history_checks += 1
        verdict = assess(history, watch.market, now, vector)
        # The same read labels outcomes later: keep its bars. Recording never
        # changes the verdict, and a store that cannot be written costs nothing.
        try:
            await BarStore(self._sessions).record(
                history, watch.market, source="SCOUT_VECTOR_HISTORY"
            )
        except (SQLAlchemyError, OSError, ValueError):
            tally.outcomes.failure("BAR_STORE_UNAVAILABLE")
        sufficient = verdict is VectorMarketDataSufficiency.SUFFICIENT
        if sufficient:
            tally.vector_sufficient += 1
        outcome = self._policy.history_outcome(watch.first_seen_at, now, sufficient=sufficient)
        settled = await self._watches.settle_history(
            watch.id,
            expected_next=watch.next_history_review_at,
            verdict=verdict.value,
            status=outcome.status,
            next_at=outcome.next_review_at,
            now=now,
        )
        if settled and outcome.status is WatchStatus.PROMOTABLE:
            tally.promotable_new += 1
        if settled and outcome.status is WatchStatus.DORMANT:
            tally.dormant_new += 1

    # ---------------------------------------------------------------- summary

    def _summary(
        self, started: datetime, tally: Tally, transport: GeckoTerminalTransport | None
    ) -> ScoutSummary:
        classified = tally.classifications
        return ScoutSummary(
            policy_version=self._policy.version,
            started_at=started.isoformat(),
            finished_at=self._clock.now().isoformat(),
            stop=tally.stop,
            bootstrapped=tally.bootstrapped,
            discovered=tally.discovered,
            valid_markets=tally.valid_markets,
            provider_identity_rejects=tally.provider_identity_rejects,
            other_provider_rejects=tally.other_provider_rejects,
            rejections=tuple(sorted(tally.rejections))[:32],
            watches_created=tally.watches_created,
            watches_updated=tally.watches_updated,
            watches_declined=tally.watches_declined,
            refreshed=tally.refreshed,
            watches_due_orbit=tally.watches_due_orbit,
            orbit_reviews_started=tally.orbit_reviews_started,
            orbit_reviews_completed=tally.orbit_reviews_completed,
            interesting=classified.get(OrbitClassification.INTERESTING.value, 0),
            not_interesting=classified.get(OrbitClassification.NOT_INTERESTING.value, 0),
            insufficient_data=classified.get(OrbitClassification.INSUFFICIENT_DATA.value, 0),
            watches_due_history=tally.watches_due_history,
            history_checks=tally.history_checks,
            vector_sufficient=tally.vector_sufficient,
            promotable_new=tally.promotable_new,
            dormant_new=tally.dormant_new,
            retired_new=tally.retired_new,
            provider_failures=tally.provider_failures,
            model_failures=tally.model_failures,
            provider_requests=0 if transport is None else transport.logical_requests,
            orbit_backlog_before=tally.orbit_backlog_before,
            orbit_backlog_after=tally.orbit_backlog_after,
            oldest_orbit_due_age_seconds=tally.oldest_orbit_due_age_seconds,
            new_watches_without_orbit_assessment=tally.new_watches_without_orbit_assessment,
            orbit_daily_budget=tally.orbit_daily_budget,
            orbit_daily_used_before=tally.orbit_daily_used_before,
            orbit_daily_remaining_before=tally.orbit_daily_remaining_before,
            orbit_daily_used_after=tally.orbit_daily_used_after,
            orbit_daily_remaining_after=tally.orbit_daily_remaining_after,
            orbit_fresh_first_reviews_due=tally.orbit_fresh_first_reviews_due,
            orbit_first_reviews_skipped_stale=tally.orbit_first_reviews_skipped_stale,
            orbit_follow_ups_deferred=tally.orbit_follow_ups_deferred,
            orbit_slots_released=tally.orbit_slots_released,
            reviews=tuple(tally.reviews[:16]),
            model_failure_reasons=tuple(
                ModelFailureCount(provider=provider, category=category, reason_code=code, count=n)
                for (provider, category, code), n in sorted(tally.model_failure_reasons.items())
            )[:32],
            outcome_eligible=tally.outcomes.eligible,
            outcome_sampled=tally.outcomes.sampled,
            outcome_reused=tally.outcomes.reused,
            outcome_fetched=tally.outcomes.fetched,
            outcome_requests=tally.outcomes.requests,
            outcome_failure_codes=tuple(sorted(tally.outcomes.failure_codes))[:16],
            shadow_candidates=len(tally.shadow_candidates),
            shadow_assessments_started=tally.shadow.started,
            shadow_assessments_completed=tally.shadow.completed,
            shadow_assessments_failed=tally.shadow.failed,
            shadow_skipped_budget=tally.shadow.skipped_budget,
            shadow_failure_codes=tuple(sorted(tally.shadow.failure_codes))[:16],
            errors=tuple(tally.errors),
        )


async def run_scout(settings: Settings, *, ports: ScoutPorts | None = None) -> ScoutReading:
    """One scout run with its own engine, released on every path."""
    supplied = ports if ports is not None else scout_ports_from_settings(settings)
    refusal = refuse_scout(settings, supplied)
    if refusal is not None:
        return refusal
    engine, sessions = connect(settings.database_url)
    try:
        return await EarlyScoutCycle(settings, sessions, ports=supplied).execute()
    finally:
        await engine.dispose()


__all__ = [
    "EarlyScoutCycle",
    "ScoutPorts",
    "ScoutReading",
    "refuse_scout",
    "run_scout",
    "scout_ports_from_settings",
]
