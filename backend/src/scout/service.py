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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
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
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import ProviderError
from src.markets.geckoterminal.networks import CHAINS, Chain, NetworkDirectory, selected_chains
from src.markets.geckoterminal.ohlcv import GeckoTerminalOhlcvSource
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.history import MarketHistorySource, MarketHistoryUnavailable
from src.markets.models import MarketSnapshot
from src.markets.recorder import MarketRecorder, ObservationConflict, record_pair_reporting
from src.orchestration.commander.context import (
    AccountPauseReader,
    SystemPausePort,
    SystemPauseUnavailable,
)
from src.reasoning.models import ReasoningFailure
from src.reasoning.provider import ReasoningProvider
from src.runner.models import ConfigurationRefused
from src.scout.models import DiscoveryWatch, ScoutReview, ScoutSummary, WatchAssessment
from src.scout.policy import EARLY_SCOUT_V1, EarlyScoutPolicy, WatchStatus
from src.scout.repository import SyncResult, WatchRepository

ScoutReading = ScoutSummary | ConfigurationRefused


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


def scout_ports_from_settings(settings: Settings) -> ScoutPorts:
    """The model the scout may ask. Constructing a client opens no connection."""
    if settings.reasoning_provider == "anthropic":
        from src.reasoning.anthropic_provider import AnthropicReasoningProvider

        return ScoutPorts(
            reasoning=AnthropicReasoningProvider(
                api_key=settings.anthropic_api_key,
                model=settings.reasoning_model,
                effort=settings.reasoning_effort,
            )
        )
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


@dataclass
class Tally:
    """What the run did, accumulated as it happens rather than assembled at the end."""

    stop: str = "COMPLETED"
    bootstrapped: int = 0
    discovered: int = 0
    valid_markets: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    watches_created: int = 0
    watches_updated: int = 0
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
    reviews: list[ScoutReview] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
        policy: EarlyScoutPolicy = EARLY_SCOUT_V1,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._ports = ports if ports is not None else scout_ports_from_settings(settings)
        self._clock = clock if clock is not None else SystemClock()
        self._policy = policy
        self._watches = WatchRepository(sessions, policy=policy)
        # Discovery and refresh freshness is ORBIT's own discovery freshness.
        self._max_input_age = timedelta(seconds=settings.orbit_input_max_age_seconds)

    async def execute(self) -> ScoutReading:
        refusal = refuse_scout(self._settings, self._ports)
        if refusal is not None:
            return refusal
        started = self._clock.now()
        tally = Tally()
        transport: GeckoTerminalTransport | None = None
        try:
            if not await self._permitted(tally):
                return self._summary(started, tally, transport)
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
        return self._summary(started, tally, transport)

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
        """One new-pool read per chain. Returns what this run recorded, by pair."""
        recorded: dict[str, MarketSnapshot] = {}
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
            tally.valid_markets += len(pairs)
            for pair in pairs:
                snapshot = await self._record(adapter, pair, recorder, tally)
                if snapshot is None:
                    continue
                recorded[snapshot.pair.pair_id] = snapshot
                allowed = tally.watches_created < self._settings.early_scout_max_new_watches_per_run
                result = await self._watches.sync(
                    snapshot, now=self._clock.now(), allow_create=allowed
                )
                if result is SyncResult.CREATED:
                    tally.watches_created += 1
                elif result is SyncResult.UPDATED:
                    tally.watches_updated += 1
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
        """
        now = self._clock.now()
        tally.watches_due_orbit, tally.watches_due_history = await self._watches.count_due(now)
        readings = Readings(
            recorded=fresh, refresh_budget=self._settings.early_scout_max_refresh_markets_per_run
        )
        reviews = self._settings.early_scout_max_orbit_reviews_per_run
        for watch in await self._watches.due_for_orbit(now, DUE_SCAN):
            if tally.orbit_reviews_started >= reviews:
                break
            snapshot = await self._reading(watch, readings, transport, directory, recorder, tally)
            await self._review(watch, snapshot, tally)
        checks = self._settings.early_scout_max_history_checks_per_run
        attempted = 0
        for watch in await self._watches.due_for_history(now, DUE_SCAN):
            if attempted >= checks:
                break
            snapshot = await self._reading(watch, readings, transport, directory, recorder, tally)
            if snapshot is None:
                continue
            attempted += 1
            await self._check_history(watch, snapshot, transport, directory, tally)

    def _is_fresh(self, snapshot: MarketSnapshot, now: datetime) -> bool:
        return snapshot.observed_at <= now and now - snapshot.freshness_at <= self._max_input_age

    async def _reading(
        self,
        watch: DiscoveryWatch,
        readings: "Readings",
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        tally: Tally,
    ) -> MarketSnapshot | None:
        """A fresh reading of this watch's market, re-observed by locator if needed.

        One attempt per market per run: a pair that was already re-observed, or
        already failed to be, is answered from that attempt.
        """
        now = self._clock.now()
        if watch.pair_id in readings.by_pair:
            return readings.by_pair[watch.pair_id]
        snapshot = readings.recorded.get(watch.pair_id)
        if snapshot is None:
            snapshot = await self._watches.snapshot(watch.latest_snapshot_id)
        if snapshot is not None and self._is_fresh(snapshot, now):
            readings.by_pair[watch.pair_id] = snapshot
            return snapshot
        reason: str | None = None
        chain = CHAINS.get(watch.chain)
        if watch.market.pool_locator is None:
            # Never addressed by a name, a symbol or an identifier's text.
            reason = "POOL_LOCATOR_UNKNOWN"
        elif chain is None or watch.chain not in {
            item.name for item in selected_chains(self._settings)
        }:
            reason = "CHAIN_NOT_CONFIGURED"
        elif readings.refresh_budget <= 0:
            reason = "REFRESH_BUDGET_REACHED"
        if reason is not None or chain is None:
            await self._watches.note(watch.id, reason or "CHAIN_NOT_CONFIGURED", now)
            if reason != "REFRESH_BUDGET_REACHED":
                readings.by_pair[watch.pair_id] = None
            return None
        readings.refresh_budget -= 1
        refreshed = await self._refresh(chain, watch, transport, directory, recorder, tally)
        readings.by_pair[watch.pair_id] = refreshed
        return refreshed

    async def _refresh(
        self,
        chain: Chain,
        watch: DiscoveryWatch,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        tally: Tally,
    ) -> MarketSnapshot | None:
        """Observe exactly this pool again, by its stored locator."""
        now = self._clock.now()
        locator = watch.market.pool_locator
        if locator is None:  # pragma: no cover - checked by the caller
            return None
        adapter = GeckoTerminalAdapter(
            transport, directory, chain, self._settings, clock=self._clock
        )
        try:
            confirmed = await adapter.observe((locator,))
        except ProviderError as error:
            tally.provider_failures += 1
            await self._watches.note(watch.id, error.code.upper(), now)
            return None
        pair = next((item for item in confirmed if item.pair_id == watch.pair_id), None)
        if pair is None:
            await self._watches.note(watch.id, "MARKET_NOT_RETURNED", now)
            return None
        if pair.market_identity.model_copy(update={"pool_locator": None}) != (
            watch.market.model_copy(update={"pool_locator": None})
        ):
            # The same pool now names a different market. Fail closed.
            await self._watches.retire(watch.id, "MARKET_IDENTITY_MISMATCH", now)
            tally.retired_new += 1
            return None
        snapshot = await self._record(adapter, pair, recorder, tally)
        if snapshot is None:
            return None
        if await self._watches.sync(snapshot, now=now, allow_create=False) is SyncResult.CONFLICT:
            await self._watches.retire(watch.id, "MARKET_IDENTITY_MISMATCH", now)
            tally.retired_new += 1
            return None
        tally.refreshed += 1
        return snapshot

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
        if watch.next_orbit_review_at is None or not await self._watches.claim_orbit(
            watch.id,
            expected=watch.next_orbit_review_at,
            next_at=review.next_review_at,
            checkpoint_index=review.checkpoint_index,
            now=now,
        ):
            return  # another run took this checkpoint; nobody was asked anything
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
            await self._failed(base, evaluation, error.category.value, watch, review, tally)
            return
        except OrbitValidationError as error:
            # Contradicted output is recorded as a failure, never as an assessment.
            await self._failed(base, evaluation, error.reason_code, watch, review, tally)
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
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                latency_ms=result.usage.latency_ms,
            )
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
        reason: str,
        watch: DiscoveryWatch,
        review: object,
        tally: Tally,
    ) -> None:
        now = self._clock.now()
        await self._watches.append_assessment(
            WatchAssessment(
                **base,  # type: ignore[arg-type]
                status="FAILED",
                failure_reason=reason,
                input_digest=orbit_input_digest(evaluation),
            )
        )
        tally.model_failures += 1
        next_at = getattr(review, "next_review_at", None)
        tally.reviews.append(
            ScoutReview(
                watch_id=watch.id,
                pair_id=watch.pair_id,
                watch_age_seconds=watch.age_seconds(now),
                checkpoint_seconds=base["checkpoint_seconds"],  # type: ignore[arg-type]
                status="FAILED",
                failure_reason=reason,
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
            rejections=tuple(sorted(tally.rejections))[:32],
            watches_created=tally.watches_created,
            watches_updated=tally.watches_updated,
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
            reviews=tuple(tally.reviews[:16]),
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
