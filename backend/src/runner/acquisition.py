"""One bounded market acquisition, performed by an explicitly started run.

The gap this closes
-------------------

Everything downstream of a recorded observation was already connected: intake
reads recorded candidates, the specialists read recorded markets, SENTINEL
values the portfolio from recorded markets, and the fill is priced from one. But
*nothing a run could do made a market be recorded*. A run therefore traded
whatever `python -m src.markets.ingest --once` had last written, however long
ago, and a case whose market had aged out simply stayed blocked until a human
ran the ingestion by hand.

This stage asks the market provider for observations, through the adapter,
normalization and recorder that already exist, and then gets out of the way: the
trading half of the run reads what was recorded through `MarketReader`, exactly
as it did before. No provider payload reaches SENTINEL, ANCHOR or any specialist.

The architecture decision, stated once
--------------------------------------

**How it is switched on.** `PAPER_RUNNER_MARKET_ACQUISITION_ENABLED`, off by
default, and separate from `PAPER_RUNNER_ENABLED` because consenting to a run is
not consenting to that run calling a public API. It is also separate from
`src.markets.ingest`, which keeps its own permissions, its own discovery-only
semantics and its own summary — nothing here widens what that command may do,
and nothing there reaches what this does.

**Which markets are needed.** A deterministic list, decided before a single
request, in one priority order:

1. the market of every open position, because a holding that cannot be marked
   makes SENTINEL refuse *every* case rather than judge a partial portfolio;
2. for each non-terminal case, oldest first, its own market and then the market
   that prices its payment asset in dollars;
3. whatever a bounded discovery read returns, with whatever budget is left.

Needed data comes before new work, deliberately: a run that spent its provider
budget discovering markets while the cases it already has starve is a run that
never finishes anything.

**When the trading half may proceed.** Acquisition is a prelude, never a
permission. It gates in exactly two directions, both negative:

* a system stop, read *before* any provider call, ends the pass with no call
  made and nothing traded;
* an acquisition whose outcome is *unknown* — a write that may or may not have
  committed — ends the pass before any further mutating trading stage, because
  everything after it would be a decision taken over data whose provenance this
  run cannot state.

Anything else, including a plain provider failure, lets the run continue exactly
as it would have without this stage. What was recorded is recorded; what was not
leaves its dependants blocked by the contracts that already block them. **A
successful acquisition authorises nothing and guarantees no fill.**

What this is not
----------------

*Not a second provider client, adapter or recorder.* The transport, the network
directory, `GeckoTerminalAdapter`, `normalize` and `MarketRecorder` are the ones
the standalone ingestion uses, composed here with tighter budgets.

*Not a scheduler.* One pass, inside one explicitly started run. No loop, no
daemon, no background task, and nothing left running when it returns.

*Not a transaction.* Each observation is its own recorder transaction, and
nothing spans a provider call. A run cut off halfway leaves every confirmed
recording in place.

*Not a way to make old data new.* Source instants and provenance are the
adapter's, unchanged. Reading a market again, or recording the same event again,
makes nothing fresher, and an observation that is unavailable, too old or
contradictory stays unusable under the contracts that already say so.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.data.tables import PositionRow, TradeCaseRow
from src.markets.geckoterminal.adapter import MAX_POOLS_PER_REQUEST, GeckoTerminalAdapter
from src.markets.geckoterminal.errors import (
    IdentityError,
    ProviderError,
    UnsupportedNetworkError,
)
from src.markets.geckoterminal.networks import Chain, NetworkDirectory, selected_chains
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import MarketIdentity, MarketPair, PoolLocatorIdentity
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder, ObservationConflict, record_pair_reporting
from src.orchestration.commander.context import SystemPausePort, SystemPauseUnavailable
from src.orchestration.workflow.models import TERMINAL_CASE_STATUSES
from src.runner.models import (
    AcquiredMarket,
    AcquisitionLimits,
    AcquisitionNeed,
    AcquisitionOutcome,
    AcquisitionStop,
    MarketAcquisition,
)

# The provider this stage can compose. Stated rather than assumed: a market
# recorded by anything else is refused by identity instead of being asked about
# through an adapter that never observed it.
PROVIDER = "geckoterminal"
# The one network the adapter's normalization produces. A recorded market on any
# other is refused rather than mapped onto this one.
NETWORK = "mainnet"


class RunDeadline(Protocol):
    """The run's own monotonic bound, borrowed rather than re-derived."""

    @property
    def remaining(self) -> float: ...

    @property
    def expired(self) -> bool: ...


class Window:
    """The acquisition's own deadline, never longer than the run's.

    Two bounds, and the nearer one always wins. The stage has a budget of its
    own so an operator can say how much of a run may be spent waiting on a
    public API, and it is still held to the run's remaining time so the bound
    cannot be escaped by configuring a generous one.
    """

    def __init__(self, seconds: float, outer: RunDeadline) -> None:
        self._expires = time.monotonic() + seconds
        self._outer = outer

    @property
    def remaining(self) -> float:
        return min(self._expires - time.monotonic(), self._outer.remaining)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class AcquisitionTarget:
    """One market this run intends to observe again, and why."""

    identity: MarketIdentity
    need: AcquisitionNeed
    chain: Chain

    @property
    def locator(self) -> PoolLocatorIdentity:
        """The coordinates this market is asked about by.

        Present by construction: a target without one is refused while the plan
        is built, because a pool that cannot be addressed cannot be asked about
        and must never be guessed at from a name, a symbol or an identifier
        something else derived. Checked rather than asserted, so the guarantee
        does not disappear under an optimised interpreter.
        """
        if self.identity.pool_locator is None:
            raise IdentityError()
        return self.identity.pool_locator


@dataclass(frozen=True)
class AcquisitionPlan:
    """What this run will ask about, and what it already knows it cannot.

    The refusals are part of the plan rather than a failure of it. A position
    whose market was never recorded, a case on a chain nobody configured and a
    market observed by another provider are all answers, and a stage that
    dropped them silently would report a smaller list than the work needed.
    """

    targets: tuple[AcquisitionTarget, ...] = ()
    discovery: tuple[Chain, ...] = ()
    refused: tuple[AcquiredMarket, ...] = ()


def _provider_code(error: ProviderError) -> str:
    """The provider boundary's own fixed code, in the run summary's vocabulary.

    A case change and nothing else. The codes are a closed set defined in
    `src.markets.geckoterminal.errors`, so nothing from a response can reach a
    summary through here — which is the whole reason the boundary has codes
    rather than messages.
    """
    return error.code.upper()


def _refusal(identity: str, chain: str, need: AcquisitionNeed, reason: str) -> AcquiredMarket:
    return AcquiredMarket(
        pair_id=identity,
        chain=chain or "unknown",
        need=need.value,
        outcome=AcquisitionOutcome.REFUSED.value,
        reason=reason,
    )


class AcquisitionPlanner:
    """Decides the work set from durable state only, before anything is asked.

    Every read here is a short, read-only one, and all of them are finished
    before the first provider call. That ordering is the rule, not an
    incidental: no provider request is ever made while this process holds a
    database lock, and nothing in this stage takes one at all.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        markets: MarketReader,
        chains: tuple[Chain, ...],
        limits: AcquisitionLimits,
    ) -> None:
        self._sessions = sessions
        self._markets = markets
        self._chains = {chain.name: chain for chain in chains}
        self._limits = limits

    async def plan(self) -> AcquisitionPlan:
        targets: list[AcquisitionTarget] = []
        refused: list[AcquiredMarket] = []

        def admit(
            identity: MarketIdentity, need: AcquisitionNeed, source: str | None = None
        ) -> None:
            """Add one need to the list, or say precisely why it cannot be met.

            A market wanted twice is listed twice, on purpose. One market is
            still one observation — the request is made once — and the second
            need is answered by that same reading, which the recorder reports as
            the replay it is. Collapsing the two here would hide that a position
            and a case depend on the same market.
            """
            reason = self._unusable(identity)
            if reason is not None:
                refused.append(_refusal(source or identity.pair_id, identity.chain, need, reason))
                return
            targets.append(
                AcquisitionTarget(identity=identity, need=need, chain=self._chains[identity.chain])
            )

        for asset_id, pair_id, identity, reason in await self._position_markets():
            if identity is None:
                refused.append(
                    _refusal(
                        pair_id or asset_id,
                        "unknown",
                        AcquisitionNeed.POSITION_VALUATION,
                        reason or "MARKET_NEVER_RECORDED",
                    )
                )
                continue
            admit(identity, AcquisitionNeed.POSITION_VALUATION)

        for identity, quote_asset_id in await self._case_markets():
            admit(identity, AcquisitionNeed.CASE_MARKET)
            payment = await self._payment_market(quote_asset_id)
            if payment is None:
                refused.append(
                    _refusal(
                        quote_asset_id,
                        identity.chain,
                        AcquisitionNeed.QUOTE_ASSET,
                        "MARKET_NEVER_RECORDED",
                    )
                )
                continue
            admit(payment, AcquisitionNeed.QUOTE_ASSET, source=quote_asset_id)

        return AcquisitionPlan(
            targets=tuple(targets),
            discovery=tuple(self._chains.values())[: self._limits.max_discovery_requests],
            refused=tuple(refused),
        )

    def _unusable(self, identity: MarketIdentity) -> str | None:
        """Why this market cannot be asked about, if it cannot.

        Every answer is a refusal to *substitute*. A market on a chain nobody
        configured is not served from another chain, one observed by a different
        provider is not asked of this one, and a market recorded before pool
        locators existed is not addressed by taking its address out of its own
        identifier — that string is a derived name, and reading coordinates back
        out of a name is exactly the guess this system does not make.
        """
        if identity.provider != PROVIDER:
            return "PROVIDER_NOT_CONFIGURED"
        if identity.chain not in self._chains:
            return "CHAIN_NOT_CONFIGURED"
        if identity.network != NETWORK:
            return "NETWORK_NOT_SUPPORTED"
        if identity.pool_locator is None:
            return "POOL_LOCATOR_UNKNOWN"
        if identity.is_fixture:
            return "FIXTURE_MARKET"
        return None

    async def _position_markets(
        self,
    ) -> list[tuple[str, str | None, MarketIdentity | None, str | None]]:
        """Every open holding's own market, by the identity that was recorded.

        A position names its market by pair, chain, network and provider, and
        the full canonical identity — venue and pool locator included — lives on
        the recorded observation. So the observation is what is looked up, and
        the position's own four fields are then checked against it: two
        providers observing one pool are two sources, and an address means
        nothing across chains.

        Bounded by the market budget, like the case list: a run cannot even
        consider more markets than it is permitted to observe. A portfolio
        larger than that budget is therefore not fully valuable from this pass
        alone, and SENTINEL refuses on the holdings it cannot mark rather than
        this stage pretending it covered them.
        """
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(PositionRow)
                    .where(PositionRow.quantity != 0)
                    .order_by(PositionRow.asset_id)
                    .limit(self._limits.max_markets)
                )
            ).all()
        holdings = [
            (
                row.asset_id,
                row.market_pair_id,
                row.market_chain,
                row.market_network,
                row.market_provider,
            )
            for row in rows
        ]
        wanted = [item[1] for item in holdings if item[1] is not None]
        recorded = {
            identity.pair_id: identity
            for identity in await self._markets.identities(wanted, limit=100)
        }
        found: list[tuple[str, str | None, MarketIdentity | None, str | None]] = []
        for asset_id, pair_id, chain, network, provider in holdings:
            if pair_id is None:
                # The holding never recorded which market it came from.
                # Choosing one for it would resolve an ambiguity silently.
                found.append((asset_id, None, None, "POSITION_MARKET_UNKNOWN"))
                continue
            identity = recorded.get(pair_id)
            if identity is None:
                found.append((asset_id, pair_id, None, "MARKET_NEVER_RECORDED"))
                continue
            if (identity.chain, identity.network, identity.provider) != (chain, network, provider):
                found.append((asset_id, pair_id, None, "MARKET_IDENTITY_MISMATCH"))
                continue
            found.append((asset_id, pair_id, identity, None))
        return found

    async def _case_markets(self) -> list[tuple[MarketIdentity, str]]:
        """The markets the live cases are about, oldest case first.

        Bounded by the *market* budget and by nothing else. This is deliberately
        not the run's case budget: acquiring the data a case depends on is not
        working that case, and conflating the two would either starve the data
        of a case the run means to work or turn every open case into this run's
        business. A case whose market the budget did not reach stays blocked by
        the contracts that already block it.
        """
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(TradeCaseRow)
                    .where(
                        TradeCaseRow.status.notin_([item.value for item in TERMINAL_CASE_STATUSES])
                    )
                    .order_by(TradeCaseRow.opened_at, TradeCaseRow.id)
                    .limit(self._limits.max_markets)
                )
            ).all()
        found = []
        for row in rows:
            identity = MarketIdentity.model_validate(row.market_payload)
            found.append((identity, identity.quote_asset_id))
        return found

    async def _payment_market(self, quote_asset_id: str) -> MarketIdentity | None:
        """A recorded market in which the payment asset itself is what is priced.

        ANCHOR denominates its ladder in the token that would actually be spent,
        so it needs that token's own USD reading — a different observation from
        the pair's, and one it refuses to infer. Only a market where the asset is
        the *base* can answer, so a market that merely mentions it is not
        accepted, and where several do the most recently observed one is taken:
        a stated rule, rather than whichever row the database happened to
        return first.
        """
        for identity in await self._markets.identities([quote_asset_id], limit=100):
            if identity.base_asset_id == quote_asset_id:
                return identity
        return None


@dataclass
class Ledger:
    """What the stage has done, accumulated as it happens.

    Never assembled at the end. An observation that was durably recorded stays
    recorded whatever fails afterwards, and a summary built after a failure
    would report zero and be wrong about the world.
    """

    limits: AcquisitionLimits
    stop: AcquisitionStop = AcquisitionStop.COMPLETED
    entries: list[AcquiredMarket] = field(default_factory=list)
    requested: int = 0
    acquired: set[str] = field(default_factory=set)

    def note(
        self,
        target: AcquisitionTarget,
        outcome: AcquisitionOutcome,
        reason: str | None = None,
    ) -> None:
        self.entries.append(
            AcquiredMarket(
                pair_id=target.identity.pair_id,
                chain=target.identity.chain,
                need=target.need.value,
                outcome=outcome.value,
                reason=reason,
            )
        )

    def counted(self, outcome: AcquisitionOutcome) -> int:
        return len([item for item in self.entries if item.outcome == outcome.value])

    @property
    def room(self) -> int:
        """How many further distinct markets this run may still observe."""
        return max(0, self.limits.max_markets - len(self.acquired))


class BoundedMarketAcquisition:
    """One acquisition stage: plan, ask, record, account.

    Owns the transport it builds and closes it on success, failure and
    cancellation alike, so nothing is left holding a connection pool when the
    run returns.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        markets: MarketReader,
        limits: AcquisitionLimits,
        *,
        pause: SystemPausePort | None = None,
        clock: Clock | None = None,
        http: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._markets = markets
        self._limits = limits
        self._pause = pause
        self._clock = clock if clock is not None else SystemClock()
        # The one external boundary a test replaces. Everything above it — the
        # transport, the network directory, the adapter, the normalization and
        # the recorder — is the production object.
        self._http = http
        self._settings = self._bounded_settings(settings)
        # Resolved when the stage runs rather than when it is built. A
        # configuration that names more chains than it permits is a mistake to
        # report, not a reason for composing a run to raise.
        self._configured = settings

    def _bounded_settings(self, settings: Settings) -> Settings:
        """The provider's own budgets, tightened to this run's.

        `min` in every direction, and that direction is the whole point: the run
        may spend less than the provider configuration allows and can never
        spend more. A run budget larger than the provider's changes nothing.
        """
        return settings.model_copy(
            update={
                "geckoterminal_max_requests": min(
                    settings.geckoterminal_max_requests, self._limits.max_provider_requests
                ),
                "geckoterminal_max_http_attempts": min(
                    settings.geckoterminal_max_http_attempts, self._limits.max_http_attempts
                ),
                "geckoterminal_total_timeout_seconds": min(
                    settings.geckoterminal_total_timeout_seconds, self._limits.max_seconds
                ),
            }
        )

    async def execute(self, deadline: RunDeadline) -> MarketAcquisition:
        """Acquire what the open work needs, then hand back an honest account."""
        ledger = Ledger(limits=self._limits)
        window = Window(self._limits.max_seconds, deadline)
        transport: GeckoTerminalTransport | None = None
        try:
            chains = selected_chains(self._configured)
            halted = await self._halted()
            if halted is not None:
                # Read before anything is asked. A stop that is in force, or one
                # that cannot be read at all, means no provider is called —
                # unknown is not permission here either.
                ledger.stop = halted
                return self._summary(ledger, transport)
            if window.expired:
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return self._summary(ledger, transport)
            plan = await self._plan(chains, window)
            ledger.entries.extend(plan.refused)
            if not plan.targets and not plan.discovery:
                ledger.stop = AcquisitionStop.NOTHING_TO_ACQUIRE
                return self._summary(ledger, transport)
            transport = GeckoTerminalTransport(
                self._settings, transport=self._http, clock=self._clock
            )
            directory = NetworkDirectory(transport, self._settings)
            recorder = MarketRecorder(self._sessions, clock=self._clock)
            await self._acquire(chains, plan, transport, directory, recorder, ledger, window)
        except ProviderError:
            # Only `selected_chains` can raise here: every provider call below
            # is awaited inside a handler of its own.
            ledger.stop = AcquisitionStop.CONFIGURATION_REFUSED
        except TimeoutError:
            # A read of durable state outlived the window. Nothing was asked and
            # nothing was written.
            ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
        except SystemPauseUnavailable:
            ledger.stop = AcquisitionStop.SYSTEM_STOP_UNREADABLE
        except (SQLAlchemyError, OSError):
            ledger.stop = AcquisitionStop.DATABASE_UNAVAILABLE
        finally:
            if transport is not None:
                # Success, failure and cancellation leave through here. The
                # close is awaited, so no background work outlives the stage.
                await transport.__aexit__(None, None, None)
        return self._summary(ledger, transport)

    async def _halted(self) -> AcquisitionStop | None:
        """Whether a durable stop forbids calling anybody, and which kind.

        Fails closed on every uncertainty, exactly as intake does: a missing
        stop source and an unreadable one are both *unknown*, and unknown is not
        permission. A provider request is attention and money spent on behalf of
        a system that may have been stopped.

        The two are still reported apart. Being stopped is an operator's
        decision working as intended; not being able to tell is a deployment
        that cannot answer a safety question, and reading one as the other would
        hide a broken installation behind a deliberate pause.
        """
        if self._pause is None:
            return AcquisitionStop.SYSTEM_STOP_UNREADABLE
        return AcquisitionStop.SYSTEM_STOPPED if await self._pause.system_paused() else None

    async def _plan(self, chains: tuple[Chain, ...], window: Window) -> AcquisitionPlan:
        planner = AcquisitionPlanner(self._sessions, self._markets, chains, self._limits)
        return await asyncio.wait_for(planner.plan(), timeout=max(0.001, window.remaining))

    async def _acquire(
        self,
        chains: tuple[Chain, ...],
        plan: AcquisitionPlan,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        recorder: MarketRecorder,
        ledger: Ledger,
        window: Window,
    ) -> None:
        """Targeted markets first, then discovery with whatever is left.

        One adapter per read rather than one per chain, which costs nothing —
        constructing one opens no connection — and buys two things. The
        discovery limit can be set per request, so a pass with two markets left
        in its budget asks for two pools instead of asking for more and
        discarding the rest. And `discover()` resets what an adapter has
        observed, so a fresh one cannot drop a targeted reading that has not
        been recorded yet. The network directory is shared, so a chain resolved
        once is not resolved again.
        """

        def adapter(chain: Chain, pools: int) -> GeckoTerminalAdapter:
            return GeckoTerminalAdapter(
                transport,
                directory,
                chain,
                # The bound is applied to the *request*, so nothing that was
                # fetched is thrown away afterwards. Truncating a response would
                # mean paying for observations and discarding them.
                self._settings.model_copy(update={"geckoterminal_pools_per_chain": pools}),
                clock=self._clock,
            )

        # Targeted first, and discovery afterwards, for a second reason beside
        # priority: `discover()` resets what an adapter has observed, so a
        # targeted reading taken after it would be the one kept and the
        # discovered ones lost.
        if not await self._targeted(chains, plan, adapter, recorder, ledger, window):
            return
        await self._discovery(plan, adapter, recorder, ledger, window)

    async def _targeted(
        self,
        chains: tuple[Chain, ...],
        plan: AcquisitionPlan,
        adapter: Callable[[Chain, int], GeckoTerminalAdapter],
        recorder: MarketRecorder,
        ledger: Ledger,
        window: Window,
    ) -> bool:
        """Observe the markets the open work depends on, chain by chain.

        The batch is fixed *before* the request, from the markets this run may
        still observe. Nothing fetched is ever thrown away afterwards: a budget
        applied to a response would mean paying for observations and discarding
        them, which is worse than never having asked.
        """
        for chain in chains:
            wanted = [item for item in plan.targets if item.identity.chain == chain.name]
            if not wanted:
                continue
            spent = self._affordable(ledger, window)
            if spent is not None:
                self._unattempted(wanted, ledger, spent)
                continue
            distinct: list[AcquisitionTarget] = []
            for target in wanted:
                if target.identity.pair_id not in {item.identity.pair_id for item in distinct}:
                    distinct.append(target)
            batch = distinct[: min(ledger.room, MAX_POOLS_PER_REQUEST)]
            asked = {item.identity.pair_id for item in batch}
            left = [item for item in wanted if item.identity.pair_id not in asked]
            if left:
                # Visibly a budget decision rather than a silence. The market is
                # not observed, and whatever depended on it stays blocked by the
                # contract that already blocks it.
                self._unattempted(left, ledger, "MARKET_BUDGET_REACHED")
                ledger.stop = AcquisitionStop.MARKET_BUDGET_REACHED
            if not batch:
                continue
            built = adapter(chain, self._settings.geckoterminal_pools_per_chain)
            try:
                confirmed = await asyncio.wait_for(
                    built.observe(tuple(item.locator for item in batch)),
                    timeout=max(0.001, window.remaining),
                )
            except TimeoutError:
                # Nothing was recorded, so nothing is in doubt: a read that did
                # not return cannot have written anything.
                self._failed(batch, ledger, "TIME_BUDGET_REACHED")
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return False
            except ProviderError as error:
                self._failed(batch, ledger, _provider_code(error))
                if isinstance(error, UnsupportedNetworkError):
                    # One chain nobody can resolve is not a reason to stop
                    # asking about another, which is the standing ingestion
                    # semantics and is kept here unchanged.
                    continue
                ledger.stop = AcquisitionStop.PROVIDER_FAILED
                return False
            ledger.requested += len(batch)
            answered = {pair.pair_id: pair for pair in confirmed}
            for target in wanted:
                if target.identity.pair_id not in asked:
                    continue
                pair = answered.get(target.identity.pair_id)
                if pair is None:
                    # Asked about and not returned. Left exactly as it was,
                    # rather than answered with something older.
                    ledger.note(target, AcquisitionOutcome.REFUSED, "MARKET_NOT_RETURNED")
                    continue
                if not await self._record(built, pair, target, recorder, ledger, window):
                    return False
        return True

    async def _discovery(
        self,
        plan: AcquisitionPlan,
        adapter: Callable[[Chain, int], GeckoTerminalAdapter],
        recorder: MarketRecorder,
        ledger: Ledger,
        window: Window,
    ) -> bool:
        """One bounded new-pool read per permitted chain, with the budget left.

        The request itself carries the bound: the adapter is built to ask for at
        most as many pools as this run may still record, so everything that
        comes back is recorded and nothing fetched is discarded.
        """
        for chain in plan.discovery:
            if ledger.room == 0:
                ledger.stop = AcquisitionStop.MARKET_BUDGET_REACHED
                return False
            if window.expired:
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return False
            built = adapter(chain, min(self._settings.geckoterminal_pools_per_chain, ledger.room))
            try:
                found = await asyncio.wait_for(
                    built.discover(), timeout=max(0.001, window.remaining)
                )
            except TimeoutError:
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return False
            except ProviderError as error:
                ledger.entries.append(
                    AcquiredMarket(
                        pair_id="*",
                        chain=chain.name,
                        need=AcquisitionNeed.NEW_CANDIDATE.value,
                        outcome=AcquisitionOutcome.FAILED.value,
                        reason=_provider_code(error),
                    )
                )
                if isinstance(error, UnsupportedNetworkError):
                    continue
                ledger.stop = AcquisitionStop.PROVIDER_FAILED
                return False
            ledger.requested += len(found)
            for pair in found:
                target = AcquisitionTarget(
                    identity=pair.market_identity,
                    need=AcquisitionNeed.NEW_CANDIDATE,
                    chain=chain,
                )
                if not await self._record(built, pair, target, recorder, ledger, window):
                    return False
        return True

    async def _record(
        self,
        provider: GeckoTerminalAdapter,
        pair: MarketPair,
        target: AcquisitionTarget,
        recorder: MarketRecorder,
        ledger: Ledger,
        window: Window,
    ) -> bool:
        """Store one validated observation, through the existing binding.

        Only what the adapter normalized and confirmed reaches the recorder, and
        only what the recorder confirmed durable is counted as this run's. An
        event that was already stored — because two needs pointed at one market,
        or because something recorded it before — is a replay and is reported as
        one rather than as an observation this run made.
        """
        try:
            written = await asyncio.wait_for(
                record_pair_reporting(provider, pair, recorder),
                timeout=max(0.001, window.remaining),
            )
        except TimeoutError:
            # It may have committed and it may not, and this run cannot tell.
            # Saying so is the only honest answer, and it is also what stops the
            # trading half from proceeding over data of unstated provenance.
            ledger.note(target, AcquisitionOutcome.UNKNOWN, "RECORD_OUTCOME_UNKNOWN")
            ledger.stop = AcquisitionStop.OUTCOME_UNKNOWN
            return False
        except ObservationConflict:
            ledger.note(target, AcquisitionOutcome.REFUSED, "OBSERVATION_CONFLICT")
            return True
        except ValueError:
            # Provenance or identity did not match what was asked for. A typed
            # refusal, and never a market substituted for another.
            ledger.note(target, AcquisitionOutcome.REFUSED, "MARKET_IDENTITY_MISMATCH")
            return True
        ledger.acquired.add(target.identity.pair_id)
        if written.inserted:
            ledger.note(target, AcquisitionOutcome.RECORDED)
        else:
            ledger.note(target, AcquisitionOutcome.UNCHANGED, "EVENT_ALREADY_RECORDED")
        return True

    def _affordable(self, ledger: Ledger, window: Window) -> str | None:
        """Which budget, if any, stops this run from asking about more markets."""
        if window.expired:
            ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
            return "TIME_BUDGET_REACHED"
        if ledger.room == 0:
            ledger.stop = AcquisitionStop.MARKET_BUDGET_REACHED
            return "MARKET_BUDGET_REACHED"
        return None

    def _unattempted(self, targets: list[AcquisitionTarget], ledger: Ledger, reason: str) -> None:
        """Planned, never asked about, and which budget decided that."""
        for target in targets:
            ledger.note(target, AcquisitionOutcome.NOT_ATTEMPTED, reason)

    def _failed(self, targets: list[AcquisitionTarget], ledger: Ledger, reason: str) -> None:
        for target in targets:
            ledger.note(target, AcquisitionOutcome.FAILED, reason)

    def _summary(
        self, ledger: Ledger, transport: GeckoTerminalTransport | None
    ) -> MarketAcquisition:
        """One structured account, in this system's own codes and counts."""
        unknown = ledger.counted(AcquisitionOutcome.UNKNOWN)
        return MarketAcquisition(
            enabled=True,
            stop=(
                AcquisitionStop.OUTCOME_UNKNOWN.value
                if unknown and ledger.stop is AcquisitionStop.COMPLETED
                else ledger.stop.value
            ),
            limits=self._limits,
            requested=ledger.requested,
            recorded=ledger.counted(AcquisitionOutcome.RECORDED),
            unchanged=ledger.counted(AcquisitionOutcome.UNCHANGED),
            refused=ledger.counted(AcquisitionOutcome.REFUSED),
            failed=ledger.counted(AcquisitionOutcome.FAILED),
            unknown=unknown,
            not_attempted=ledger.counted(AcquisitionOutcome.NOT_ATTEMPTED),
            # The provider's own counters, not a tally kept beside them. What was
            # actually spent includes every retry and every helper query, and a
            # number this stage incremented itself would miss both.
            provider_requests=0 if transport is None else transport.logical_requests,
            http_attempts=0 if transport is None else transport.http_attempts,
            markets=tuple(ledger.entries[:64]),
        )


def disabled(reason: AcquisitionStop = AcquisitionStop.NOT_ENABLED) -> MarketAcquisition:
    """The account of a stage that did not run. Reported rather than omitted."""
    return MarketAcquisition(enabled=False, stop=reason.value)
