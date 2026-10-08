"""What a configuration can actually build, and what it honestly cannot.

Every port here is constructed from `Settings` and nothing else. A port that the
configuration does not describe is **absent**, not stubbed: the role that needed
it is reported unavailable and takes no step. That is the same choice
`CapabilityProvider` already made, for the same reason — a stub that silently
returns nothing is indistinguishable from a specialist that looked and found
nothing, and those mean opposite things.

Two absences are deliberate and permanent rather than gaps to be filled later.

**There is no synthetic model here.** `DeterministicReasoningProvider` replays a
script, and a script is not something a configuration can hold — so `fake` is
not constructible from settings at all. A production configuration therefore
cannot reach a synthetic model by any combination of environment values, and a
test that wants one has to pass it in through `RunnerPorts`, in its own code,
where it is visible.

**There is no fixture fallback.** If the market provider is the fixture reader,
that is what the operator configured; nothing here quietly substitutes one for a
provider that failed to build.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.anchor.context import AnchorContextReader
from src.agents.anchor.handler import AnchorWorkerHandler
from src.agents.atlas.context import AtlasContextReader, SnapshotBuilderPort
from src.agents.early.context import EarlyContextReader
from src.agents.early.handler import EarlyWorkerHandler
from src.agents.fuse.context import FuseContextReader
from src.agents.fuse.handler import FuseWorkerHandler
from src.agents.orbit.context import OrbitContextReader
from src.agents.orbit.handler import OrbitWorkerHandler
from src.agents.pulse.context import PulseContextReader
from src.agents.pulse.handler import PulseWorkerHandler
from src.agents.signal.context import SignalContextReader
from src.agents.signal.handler import SignalWorkerHandler
from src.agents.signal.sources.factory import social_source
from src.agents.vector.context import VectorContextReader
from src.agents.vector.handler import VectorWorkerHandler
from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.core.models import AgentRole, RiskLimits
from src.markets.history import (
    ChainRoutedHistory,
    MarketHistorySource,
    UnconfiguredHistorySource,
)
from src.markets.quotes import ExecutionQuoteSource, UnconfiguredQuoteSource
from src.markets.reader import MarketReader
from src.orchestration.casefill.service import CaseFillService
from src.orchestration.commander.context import AccountPauseReader, SystemPausePort
from src.orchestration.commander.intake import CommanderIntakeService
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1
from src.orchestration.costs.models import PaperCostReading, paper_cost_assumptions
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paper import PaperTradingService
from src.orchestration.paperexit.exitread import (
    AtlasExitRead,
    ExitOnchainReadPort,
    UnavailableExitRead,
)
from src.orchestration.paperexit.service import PaperExitService
from src.orchestration.riskrequest.service import RiskRequestService
from src.orchestration.strategy.early import EARLY_ENTRY_V1, PRE_VECTOR_EARLY_ENTRY_V1
from src.orchestration.worker.runner import CapabilityProvider, WorkerHandler, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.policy import TRADE_CASE_EARLY_V1
from src.orchestration.workflow.service import TradeCaseService
from src.reasoning.provider import ReasoningProvider
from src.runner.acquisition import BoundedMarketAcquisition
from src.runner.models import AcquisitionLimits, RoleAvailability, RunLimits
from src.runner.pre_risk import PreRiskLimits, PreRiskMarketRefresh
from src.runtime.models import chain_configs
from src.scout.candidates import EarlyWatchCandidates, PromotedWatchCandidates, PromotionRefresh
from src.scout.repository import WatchRepository

Closer = Callable[[], Awaitable[None]]


def _exit(resource: Any) -> Closer:
    """Close something whose lifetime contract is `async with`.

    Both the market transport and the quote source expose their release through
    `__aexit__` and nothing else, so that is what is called — the documented
    contract, rather than reaching for the client they happen to hold.
    """

    async def close() -> None:
        await resource.__aexit__(None, None, None)

    return close


@dataclass(frozen=True)
class RunnerPorts:
    """The outside world, as this run is allowed to see it.

    Built from settings by `ports_from_settings`. A test substitutes a field to
    replace an external boundary — a scripted model, a fixture quote source —
    and does so in its own code rather than through an environment value no
    production deployment should be able to set.
    """

    reasoning: ReasoningProvider | None = None
    reasoning_unavailable: str = "REASONING_PROVIDER_NOT_CONFIGURED"
    history: MarketHistorySource | None = None
    history_unavailable: str = "MARKET_HISTORY_NOT_CONFIGURED"
    quotes: ExecutionQuoteSource | None = None
    onchain: object | None = None
    onchain_unavailable: str = "ONCHAIN_SOURCE_NOT_CONFIGURED"
    # The two ATLAS fact sources and the SIGNAL social source. Built from
    # settings when a provider is selected; supplied here when a caller is
    # replacing the external boundary itself.
    holders: object | None = None
    origins: object | None = None
    social: object | None = None
    pause: SystemPausePort | None = None
    # The HTTP boundary the market acquisition reaches through. Supplied only
    # where a caller is replacing the outside edge itself; everything above it —
    # transport, network directory, adapter, normalization, recorder — stays the
    # production object. A configuration cannot produce one.
    market_http: httpx.AsyncBaseTransport | None = None
    # Everything built here that owns a connection pool. Closed by
    # `runner_stack` on success, on error and on cancellation alike.
    closers: tuple[Closer, ...] = ()


def limits_from_settings(settings: Settings) -> RunLimits:
    return RunLimits(
        max_candidates=settings.paper_runner_max_candidates,
        max_new_cases=settings.paper_runner_max_new_cases,
        max_steps=settings.paper_runner_max_steps,
        max_cases=settings.paper_runner_max_cases,
        max_runtime_seconds=settings.paper_runner_max_seconds,
        step_timeout_seconds=settings.paper_runner_step_timeout_seconds,
    )


def acquisition_limits_from_settings(settings: Settings) -> AcquisitionLimits:
    return AcquisitionLimits(
        max_markets=settings.paper_runner_acquisition_max_markets,
        max_discovery_requests=settings.paper_runner_acquisition_max_discovery_requests,
        max_provider_requests=settings.paper_runner_acquisition_max_provider_requests,
        max_http_attempts=settings.paper_runner_acquisition_max_http_attempts,
        max_seconds=settings.paper_runner_acquisition_max_seconds,
    )


@dataclass(frozen=True)
class ChainContractSources:
    """ATLAS's contract source for every configured chain, keyed by chain and network.

    A contract source reads "the" chain head and takes no chain argument, so
    one is built per chain and the case's own market chooses among them.
    """

    sources: Mapping[tuple[str, str], object]


def ports_from_settings(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    clock: Clock | None = None,
) -> RunnerPorts:
    """Build every port the configuration actually describes.

    Constructing an HTTP or RPC client opens no connection; the first call
    happens when a handler asks, under this run's own step timeout. Every client
    built here is owned by `runner_stack`, which closes it on success, on error
    and on cancellation alike.
    """
    tick = clock if clock is not None else SystemClock()
    reasoning: ReasoningProvider | None = None
    unavailable = "REASONING_PROVIDER_NOT_CONFIGURED"
    if settings.reasoning_provider == "anthropic":
        from src.reasoning.anthropic_provider import AnthropicReasoningProvider

        reasoning = AnthropicReasoningProvider(
            api_key=settings.anthropic_api_key,
            model=settings.reasoning_model,
            effort=settings.reasoning_effort,
        )
    elif settings.reasoning_provider == "codex":
        from src.codex_reasoning.provider import codex_provider_from

        reasoning = codex_provider_from(settings)
        if reasoning is None:
            unavailable = "REASONING_PROVIDER_CLI_NOT_FOUND"
    elif settings.reasoning_provider == "fake":
        # Selectable in settings, and deliberately not constructible here: the
        # deterministic provider replays a script, and a configuration has no
        # script to give it. A run that wants one is a test, and says so in code.
        unavailable = "REASONING_PROVIDER_NOT_COMPOSABLE"

    closers: list[Closer] = []

    history: MarketHistorySource | None = None
    history_unavailable = "MARKET_HISTORY_NOT_CONFIGURED"
    if settings.vector_history_provider == "geckoterminal":
        from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
        from src.markets.geckoterminal.ohlcv import GeckoTerminalOhlcvSource
        from src.markets.geckoterminal.transport import GeckoTerminalTransport

        names = tuple(name.strip() for name in settings.market_chains.split(",") if name.strip())
        if not names or any(name not in CHAINS for name in names):
            history_unavailable = "MARKET_HISTORY_CHAIN_NOT_SUPPORTED"
        else:
            # One transport and one network directory, a source per chain — the
            # scout's arrangement — routed by each case's own market.
            transport = GeckoTerminalTransport(settings, clock=tick)
            closers.append(_exit(transport))
            directory = NetworkDirectory(transport, settings)
            history = ChainRoutedHistory(
                {
                    (name, "mainnet"): GeckoTerminalOhlcvSource(
                        transport, directory, CHAINS[name], settings, clock=tick
                    )
                    for name in names
                }
            )

    onchain: object | None = None
    onchain_unavailable = "ONCHAIN_SOURCE_NOT_CONFIGURED"
    configs = chain_configs(settings)
    if configs:
        from src.agents.atlas.rpc_source import RpcTokenContractSource
        from src.runtime.rpc import EvmRpcClient

        # The request/response RPC client the ATLAS source already uses, one per
        # enabled chain. No websocket, no recovery loop and no ingestion: each
        # reads a block and two contract slots when a handler asks for them.
        sources: dict[tuple[str, str], object] = {}
        for config in configs:
            client = EvmRpcClient(config, settings)
            closers.append(client.close)
            sources[(config.chain, config.network)] = RpcTokenContractSource(
                client=client, config=config, clock=tick
            )
        onchain = ChainContractSources(sources)

    quotes: ExecutionQuoteSource | None = None
    if settings.execution_quote_provider == "kyberswap":
        from src.markets.kyberswap.source import KyberSwapQuoteSource

        source = KyberSwapQuoteSource(settings, clock=tick)
        closers.append(_exit(source))
        quotes = source

    return RunnerPorts(
        reasoning=reasoning,
        reasoning_unavailable=unavailable,
        history=history,
        history_unavailable=history_unavailable,
        quotes=quotes,
        onchain=onchain,
        onchain_unavailable=onchain_unavailable,
        pause=AccountPauseReader(sessions),
        closers=tuple(closers),
    )


@dataclass(frozen=True)
class RunnerStack:
    """Every service one run coordinates, and the roles it may actually step."""

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    runtime: WorkerRuntimeService
    markets: MarketReader
    intake: CommanderIntakeService
    risk: RiskRequestService
    fills: CaseFillService
    costs: PaperCostReading
    limits: RunLimits
    runners: tuple[WorkerRunner, ...]
    roles: tuple[RoleAvailability, ...]
    clock: Clock
    # The durable stop, as the run's own handle on it. The services that write
    # already hold it; the run needs it to refuse to *ask a provider anything*
    # before it starts, which is earlier than any of them is consulted.
    pause: SystemPausePort | None = None
    # Present only where the operator switched market acquisition on. Absent is
    # the ordinary case and means exactly what it did before this contract: the
    # run trades whatever was already recorded and asks nobody for anything.
    acquisition: BoundedMarketAcquisition | None = None
    # Composed under the same consent as acquisition, with a budget of its own.
    # Absent, a risk request is asked exactly as it was before: over whatever
    # was already recorded, with SENTINEL's own bound refusing what is stale.
    pre_risk: PreRiskMarketRefresh | None = None
    # Present only with the early scout enabled: intake then reads PROMOTABLE
    # watches instead of every fresh new pool, and this re-observes them by
    # exact locator first. Absent means intake is exactly what it always was.
    promotion: PromotionRefresh | None = None
    watches: WatchRepository | None = None
    # Present only with automatic PAPER exits configured: the sweep that asks
    # PAPER_EXIT_V1 and exits through PaperExitService. Absent means no position
    # is ever closed without an explicit request, as before.
    exits: AutoExitService | None = None
    # PRE_VECTOR_EARLY_ENTRY_V1, present only under its own explicit consent:
    # the intake that opens early cases from young WATCHING watches, and the
    # refresh that re-observes those watches first. Absent, nothing early exists.
    early_intake: CommanderIntakeService | None = None
    early_promotion: PromotionRefresh | None = None
    # Why no model can be asked, or None when one can. Read by preflight for the
    # scout, which needs ORBIT's model whether or not the ORBIT worker is on.
    reasoning_unavailable: str | None = None
    closers: tuple[Closer, ...] = ()

    @property
    def misconfigured(self) -> tuple[RoleAvailability, ...]:
        """Roles an operator switched on that this configuration cannot run.

        Kept apart from the ones deliberately left off. Enabling a role and not
        configuring what it needs is a mistake somebody made, and a run that
        quietly proceeded without it would report a market as unexamined when it
        was really unexaminable.
        """
        return tuple(
            item for item in self.roles if not item.available and item.reason != "ROLE_NOT_ENABLED"
        )


def _exit_read(settings: Settings, ports: RunnerPorts, clock: Clock) -> ExitOnchainReadPort:
    """ATLAS's deterministic collector and policy, composed for a sale.

    The same chain, holder and origin sources the ATLAS worker is built on, and
    no model: an exit's on-chain basis is facts and policy only.
    """
    if ports.onchain is None:
        return UnavailableExitRead(ports.onchain_unavailable)
    # No pool census for a sale: its result cannot bind one, and its reads
    # would only slow the exit down.
    return AtlasExitRead(
        builder=atlas_builder(settings, ports, clock, pool_control=False), clock=clock
    )


def atlas_builder(
    settings: Settings, ports: RunnerPorts, clock: Clock, *, pool_control: bool = True
) -> SnapshotBuilderPort:
    # ``pool_control`` is False for a PAPER exit's read: neither the V4 census
    # nor the funding graph can bind a sale, so neither is collected for one.
    """ATLAS's snapshot builder: one per chain when several are configured.

    The holder and origin sources take a chain argument and are shared; only
    the contract source is bound to one chain. Used by the ATLAS worker and by
    a PAPER exit's fresh read alike, so both route a case the same way.
    """
    from src.agents.atlas.context import AtlasSnapshotBuilder, ChainRoutedSnapshotBuilder
    from src.agents.atlas.rpc_source import RpcTokenContractSource
    from src.agents.atlas.sources.factory import funding_source, holder_sources, origin_sources
    from src.agents.atlas.v4.census import V4PoolCensus
    from src.agents.atlas.v4.rpc import RpcV4ChainReads

    holders = ports.holders if ports.holders is not None else holder_sources(settings, clock=clock)
    origins = ports.origins if ports.origins is not None else origin_sources(settings)

    def one(contracts: object) -> AtlasSnapshotBuilder:
        census: V4PoolCensus | None = None
        verifier: RpcTokenContractSource | None = None
        if (
            pool_control
            and settings.atlas_v4_pool_control_enabled
            and isinstance(contracts, RpcTokenContractSource)
        ):
            # The same chain-bound client as the contract source, so the census
            # can only ever answer for the chain this builder is routed to.
            census = V4PoolCensus(
                reads=RpcV4ChainReads(contracts.client), chain=contracts.config.chain
            )
            verifier = contracts
        funding = (
            funding_source(settings, contracts.config.chain)
            if pool_control and isinstance(contracts, RpcTokenContractSource)
            else None
        )
        return AtlasSnapshotBuilder(
            contracts=contracts,  # type: ignore[arg-type]
            holders=holders,  # type: ignore[arg-type]
            origins=origins,  # type: ignore[arg-type]
            verifier=verifier,
            pool_census=census,
            funding=funding,
            clock=clock,
        )

    if isinstance(ports.onchain, ChainContractSources):
        return ChainRoutedSnapshotBuilder(
            {key: one(source) for key, source in ports.onchain.sources.items()}
        )
    # A single source supplied at the outside edge (a test, or one chain).
    return one(ports.onchain)


def build_stack(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    ports: RunnerPorts | None = None,
    clock: Clock | None = None,
) -> RunnerStack:
    """Compose the existing services. Nothing here is a second implementation."""
    tick = clock if clock is not None else SystemClock()
    supplied = ports if ports is not None else ports_from_settings(settings, sessions, clock=tick)
    if supplied.pause is None:
        supplied = replace(supplied, pause=AccountPauseReader(sessions))

    cases = TradeCaseService(sessions, clock=tick)
    runtime = WorkerRuntimeService(sessions, cases, clock=tick)
    markets = MarketReader(
        sessions,
        clock=tick,
        max_age=timedelta(seconds=settings.market_max_age_seconds),
    )
    costs = paper_cost_assumptions(
        fee_bps=settings.paper_fee_bps,
        slippage_bps=settings.paper_slippage_bps,
        trading_mode=settings.trading_mode,
    )
    paper = PaperTradingService(sessions, RiskLimits(), settings.trading_mode, clock=tick)
    limits = limits_from_settings(settings)
    watches: WatchRepository | None = None
    promotion: PromotionRefresh | None = None
    candidates: MarketReader | PromotedWatchCandidates = markets
    if settings.early_scout_enabled:
        watches = WatchRepository(sessions)
        candidates = PromotedWatchCandidates(sessions=sessions, markets=markets, watches=watches)
        promotion = PromotionRefresh(
            settings=settings,
            sessions=sessions,
            source=candidates,
            pause=supplied.pause,
            clock=tick,
            http=supplied.market_http,
        )
    early_intake: CommanderIntakeService | None = None
    early_promotion: PromotionRefresh | None = None
    if settings.pre_vector_early_entry_enabled and watches is not None:
        early_source = EarlyWatchCandidates(
            sessions=sessions, markets=markets, watches=watches, clock=tick
        )
        early_promotion = PromotionRefresh(
            settings=settings,
            sessions=sessions,
            source=early_source,
            pause=supplied.pause,
            clock=tick,
            http=supplied.market_http,
        )
        early_intake = CommanderIntakeService(
            cases=cases,
            markets=early_source,
            sessions=sessions,
            # One new early case per cycle, Robinhood only, and a short life:
            # an early setup lives ten minutes, so a case that has not reached
            # its entry within the hour is not an early entry any more.
            policy=replace(
                COMMANDER_CONTROL_V1,
                version=f"{COMMANDER_CONTROL_V1.version}-early",
                enabled_chains=frozenset({"robinhood"}),
                max_cases_per_cycle=EARLY_ENTRY_V1.max_entries_per_run,
                case_lifetime=timedelta(hours=1),
            ),
            clock=tick,
            kill_switch=settings.commander_kill_switch,
            pause=supplied.pause,
            strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1,
            workflow=TRADE_CASE_EARLY_V1,
        )
    intake = CommanderIntakeService(
        cases=cases,
        markets=candidates,
        sessions=sessions,
        # The opening budget, enforced where cases are actually written. A
        # ceiling applied to the result would mean opening cases and then
        # discarding them, and an opened case is a real thing in the workflow
        # that somebody has to work or expire. Never above the control policy's
        # own bound, and never above the case budget either: a case this run
        # could not then work on is a case opened for nobody.
        policy=replace(
            COMMANDER_CONTROL_V1,
            max_cases_per_cycle=min(
                COMMANDER_CONTROL_V1.max_cases_per_cycle,
                limits.max_new_cases,
                limits.max_cases,
            ),
        ),
        clock=tick,
        kill_switch=settings.commander_kill_switch,
        pause=supplied.pause,
    )
    risk = RiskRequestService(
        sessions=sessions,
        cases=cases,
        markets=markets,
        costs=costs,
        requested_notional_usd=settings.paper_requested_notional_usd,
        limits=paper.limits,
        trading_mode=settings.trading_mode,
        kill_switch=settings.commander_kill_switch,
        pause=supplied.pause,
        clock=tick,
    )
    fills = CaseFillService(
        sessions=sessions,
        cases=cases,
        paper=paper,
        markets=markets,
        costs=costs,
        trading_mode=settings.trading_mode,
        kill_switch=settings.commander_kill_switch,
        pause=supplied.pause,
        clock=tick,
    )
    exits = None
    if settings.paper_auto_exit_enabled:
        # The one exit path, driven by a deterministic policy. Built only when
        # an operator stated the policy's numbers; validation refuses otherwise.
        exits = AutoExitService(
            sessions=sessions,
            exits=PaperExitService(
                sessions=sessions,
                cases=cases,
                paper=paper,
                markets=markets,
                costs=costs,
                trading_mode=settings.trading_mode,
                kill_switch=settings.commander_kill_switch,
                pause=supplied.pause,
                clock=tick,
                # Always a fresh read for an automatic exit: the entry's evidence
                # is never the basis of a sale hours later. Without a chain
                # source every exit is refused by name, never judged on less.
                exit_read=_exit_read(settings, supplied, tick),
            ),
            markets=markets,
            policy=PaperExitPolicy(
                stop_loss_bps=settings.paper_exit_stop_loss_bps or 0,
                take_profit_bps=settings.paper_exit_take_profit_bps or 0,
                max_holding_seconds=(settings.paper_exit_max_holding_minutes or 0) * 60,
                invalidate_below_min_liquidity=settings.paper_exit_invalidate_below_min_liquidity,
            ),
            limits=paper.limits,
            max_exits=settings.paper_exit_max_per_run,
            clock=tick,
        )
    runners, roles = _runners(settings, sessions, runtime, markets, supplied, tick)
    acquisition = None
    pre_risk = None
    if settings.paper_runner_market_acquisition_enabled:
        # Composed from settings and nothing else, like every other port here.
        # Its own transport is built when it runs and closed when it finishes,
        # so a run that never reaches the stage opens no connection at all.
        acquisition = BoundedMarketAcquisition(
            settings,
            sessions,
            markets,
            acquisition_limits_from_settings(settings),
            pause=supplied.pause,
            clock=tick,
            http=supplied.market_http,
        )
        pre_risk = PreRiskMarketRefresh(
            settings,
            sessions,
            markets,
            paper.limits,
            PreRiskLimits(
                max_requests=settings.paper_runner_pre_risk_market_max_requests,
                max_seconds=settings.paper_runner_pre_risk_market_max_seconds,
            ),
            clock=tick,
            http=supplied.market_http,
        )
    return RunnerStack(
        settings=settings,
        sessions=sessions,
        cases=cases,
        runtime=runtime,
        markets=markets,
        intake=intake,
        risk=risk,
        fills=fills,
        costs=costs,
        limits=limits,
        runners=runners,
        roles=roles,
        clock=tick,
        pause=supplied.pause,
        acquisition=acquisition,
        pre_risk=pre_risk,
        promotion=promotion,
        watches=watches,
        exits=exits,
        early_intake=early_intake,
        early_promotion=early_promotion,
        reasoning_unavailable=None
        if supplied.reasoning is not None
        else supplied.reasoning_unavailable,
        closers=supplied.closers,
    )


def _runners(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    runtime: WorkerRuntimeService,
    markets: MarketReader,
    ports: RunnerPorts,
    clock: Clock,
) -> tuple[tuple[WorkerRunner, ...], tuple[RoleAvailability, ...]]:
    """One runner per role that is both enabled and fully composable.

    Enabled and unbuildable is reported, not silently skipped: a run in which a
    role was never wired looks identical to one in which it had nothing to do,
    and those are very different things to read afterwards.
    """
    cases = runtime.cases
    built: list[WorkerRunner] = []
    reported: list[RoleAvailability] = []

    def note(role: AgentRole, reason: str | None) -> None:
        reported.append(RoleAvailability(role=role.value, available=reason is None, reason=reason))

    def add(role: AgentRole, handler: WorkerHandler, provider: CapabilityProvider) -> None:
        built.append(
            WorkerRunner(
                runtime,
                handler,
                provider,
                poll_interval=timedelta(seconds=settings.worker_poll_interval_seconds),
            )
        )
        note(role, None)

    if not settings.orbit_worker_enabled:
        note(AgentRole.ORBIT, "ROLE_NOT_ENABLED")
    elif ports.reasoning is None:
        note(AgentRole.ORBIT, ports.reasoning_unavailable)
    else:
        add(
            AgentRole.ORBIT,
            OrbitWorkerHandler(
                provider=ports.reasoning,
                max_output_tokens=settings.reasoning_max_output_tokens,
                timeout=timedelta(seconds=settings.reasoning_timeout_seconds),
            ),
            CapabilityProvider(
                service=runtime,
                context=OrbitContextReader(
                    cases=cases,
                    markets=markets,
                    liquidity_floor_usd=settings.orbit_discovery_liquidity_floor_usd,
                    max_input_age=timedelta(seconds=settings.orbit_input_max_age_seconds),
                    clock=clock,
                ),
            ),
        )

    if not settings.atlas_worker_enabled:
        note(AgentRole.ATLAS, "ROLE_NOT_ENABLED")
    elif ports.onchain is None:
        note(AgentRole.ATLAS, ports.onchain_unavailable)
    else:
        from src.agents.atlas.handler import AtlasWorkerHandler

        add(
            AgentRole.ATLAS,
            AtlasWorkerHandler(provider=ports.reasoning, clock=clock),
            CapabilityProvider(
                service=runtime,
                onchain=AtlasContextReader(
                    cases=cases, builder=atlas_builder(settings, ports, clock)
                ),
            ),
        )

    social = ports.social if ports.social is not None else social_source(settings, clock=clock)
    if not settings.signal_worker_enabled:
        note(AgentRole.SIGNAL, "ROLE_NOT_ENABLED")
    elif ports.reasoning is None:
        note(AgentRole.SIGNAL, ports.reasoning_unavailable)
    elif social is None:
        # No social source selected. Absent rather than empty: an empty feed and
        # an unread one are different answers about the same market.
        note(AgentRole.SIGNAL, "SOCIAL_SOURCE_NOT_CONFIGURED")
    else:
        add(
            AgentRole.SIGNAL,
            SignalWorkerHandler(
                provider=ports.reasoning,
                max_output_tokens=settings.reasoning_max_output_tokens,
                timeout=timedelta(seconds=settings.reasoning_timeout_seconds),
            ),
            CapabilityProvider(
                service=runtime,
                sentiment=SignalContextReader(
                    cases=cases,
                    source=social,  # type: ignore[arg-type]
                    max_observations=settings.signal_max_observations,
                    max_model_observations=settings.signal_max_model_observations,
                    clock=clock,
                ),
            ),
        )

    if not settings.vector_worker_enabled:
        note(AgentRole.VECTOR, "ROLE_NOT_ENABLED")
    elif ports.reasoning is None:
        note(AgentRole.VECTOR, ports.reasoning_unavailable)
    elif ports.history is None and settings.vector_history_provider != "disabled":
        # A configured history provider this run cannot build is a gap, not a
        # reason to fall back to the unconfigured source and call it a refusal.
        note(AgentRole.VECTOR, ports.history_unavailable)
    else:
        add(
            AgentRole.VECTOR,
            VectorWorkerHandler(
                provider=ports.reasoning,
                max_output_tokens=settings.reasoning_max_output_tokens,
                timeout=timedelta(seconds=settings.reasoning_timeout_seconds),
            ),
            CapabilityProvider(
                service=runtime,
                setup=VectorContextReader(
                    cases=cases,
                    markets=markets,
                    # Absent means the unconfigured source, which refuses rather
                    # than drawing structure from a single price.
                    history=ports.history
                    if ports.history is not None
                    else UnconfiguredHistorySource(),
                    clock=clock,
                ),
            ),
        )

    if not settings.pulse_worker_enabled:
        note(AgentRole.PULSE, "ROLE_NOT_ENABLED")
    else:
        add(
            AgentRole.PULSE,
            PulseWorkerHandler(),
            CapabilityProvider(
                service=runtime,
                pulse=PulseContextReader(cases=cases, markets=markets, clock=clock),
            ),
        )

    if not settings.anchor_worker_enabled:
        note(AgentRole.ANCHOR, "ROLE_NOT_ENABLED")
    else:
        add(
            AgentRole.ANCHOR,
            AnchorWorkerHandler(
                quote_provider=settings.execution_quote_provider
                if ports.quotes is not None
                else "unconfigured"
            ),
            CapabilityProvider(
                service=runtime,
                anchor=AnchorContextReader(
                    cases=cases,
                    markets=markets,
                    quotes=ports.quotes if ports.quotes is not None else UnconfiguredQuoteSource(),
                    clock=clock,
                ),
            ),
        )

    if not settings.pre_vector_early_entry_enabled:
        note(AgentRole.EARLY, "ROLE_NOT_ENABLED")
    elif ports.history is None and settings.vector_history_provider != "disabled":
        # The same history VECTOR would read; a configured provider this run
        # cannot build is a gap, never a fallback to "no history".
        note(AgentRole.EARLY, ports.history_unavailable)
    else:
        add(
            AgentRole.EARLY,
            EarlyWorkerHandler(),
            CapabilityProvider(
                service=runtime,
                early=EarlyContextReader(
                    cases=cases,
                    markets=markets,
                    history=ports.history
                    if ports.history is not None
                    else UnconfiguredHistorySource(),
                    clock=clock,
                ),
            ),
        )

    if not settings.fuse_worker_enabled:
        note(AgentRole.FUSE, "ROLE_NOT_ENABLED")
    else:
        # FUSE has an evidence requirement in the workflow policy —
        # `SYNTHESIZE_EVIDENCE`, optional and not safety-critical — so
        # `authorized_task_type` answers for it and its tasks are claimable like
        # any other. Nothing else is needed: the synthesis reads verdicts the
        # specialists already committed, so there is no provider and no model.
        add(
            AgentRole.FUSE,
            FuseWorkerHandler(),
            CapabilityProvider(service=runtime, fuse=FuseContextReader(cases=cases, clock=clock)),
        )

    order = {role: index for index, role in enumerate(AgentRole)}
    return tuple(built), tuple(sorted(reported, key=lambda item: order[AgentRole(item.role)]))


@asynccontextmanager
async def runner_stack(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    ports: RunnerPorts | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[RunnerStack]:
    """The composed stack, with every client it owns released afterwards.

    Success, failure and cancellation all leave through the same `finally`, so a
    run that is cut off does not leave an HTTP or RPC connection pool behind.
    Each close is awaited and its own failure suppressed: one client that cannot
    be closed must not prevent the others from being.
    """
    stack = build_stack(settings, sessions, ports=ports, clock=clock)
    try:
        yield stack
    finally:
        for close in stack.closers:
            with suppress(Exception):
                await close()
