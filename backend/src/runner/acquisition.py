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
from src.markets.geckoterminal.networks import (
    Chain,
    NetworkDirectory,
    VerifiedNetworkRegistry,
    selected_chains,
)
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import MarketIdentity, MarketPair, PoolLocatorIdentity
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder, ObservationConflict, record_pair_reporting
from src.markets.scope import MarketScope, describes_market
from src.orchestration.commander.context import SystemPausePort, SystemPauseUnavailable
from src.orchestration.workflow.models import TERMINAL_CASE_STATUSES
from src.runner.models import (
    AcquiredMarket,
    AcquisitionLimits,
    AcquisitionNeed,
    AcquisitionOutcome,
    AcquisitionStop,
    DiscoveryRead,
    DiscoveryRejection,
    MarketAcquisition,
    PositionCoverage,
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
    # Every open holding's market, one target per distinct full identity, in
    # their own budget; and the ones beyond it, reported rather than dropped.
    positions: tuple[AcquisitionTarget, ...] = ()
    position_overflow: tuple[AcquisitionTarget, ...] = ()
    open_positions: int = 0
    unaddressable_positions: int = 0


def _provider_code(error: ProviderError) -> str:
    """The provider boundary's own fixed code, in the run summary's vocabulary.

    A case change and nothing else. The codes are a closed set defined in
    `src.markets.geckoterminal.errors`, so nothing from a response can reach a
    summary through here — which is the whole reason the boundary has codes
    rather than messages.
    """
    return error.code.upper()


def unaddressable(identity: MarketIdentity, chains: "dict[str, Chain]") -> str | None:
    """Why this market cannot be asked about by exact locator, if it cannot.

    Every answer is a refusal to *substitute*. A market on a chain nobody
    configured is not served from another chain, one observed by a different
    provider is not asked of this one, and a market recorded before pool
    locators existed is not addressed by taking its address out of its own
    identifier — that string is a derived name, and reading coordinates back
    out of a name is exactly the guess this system does not make. Shared by the
    run-start acquisition and the pre-risk refresh, so both refuse alike.
    """
    if identity.provider != PROVIDER:
        return "PROVIDER_NOT_CONFIGURED"
    if identity.chain not in chains:
        return "CHAIN_NOT_CONFIGURED"
    if identity.network != NETWORK:
        return "NETWORK_NOT_SUPPORTED"
    if identity.pool_locator is None:
        return "POOL_LOCATOR_UNKNOWN"
    if identity.is_fixture:
        return "FIXTURE_MARKET"
    return None


async def position_markets(
    sessions: async_sessionmaker[AsyncSession], markets: MarketReader, limit: int | None
) -> list[tuple[str, str | None, MarketIdentity | None, str | None]]:
    """Every open holding's own market, by its full identity.

    The authority is the case that bought the holding — position, cycle,
    entry, case — whose `MarketIdentity` names provider, chain, network, pool,
    both assets, venue and pool locator. The holding's own recorded fields must
    agree with it. A holding without a case-bound entry (one booked before
    cycles existed) falls back to the identity recorded for its pool, held to
    the holding's chain, network and provider. Ordered by asset; bounded by
    `limit` when one is given. Shared by acquisition and the pre-risk refresh.
    """
    from src.data.repository import read_position
    from src.orchestration.valuation.service import held_market_identities

    async with sessions() as session:
        statement = (
            select(PositionRow).where(PositionRow.quantity != 0).order_by(PositionRow.asset_id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await session.scalars(statement)).all()
        held = [read_position(row) for row in rows]
        from_cases = await held_market_identities(session, held)
    # Recorded identities are needed for holdings without a case, and to
    # complete a case identity recorded before pool locators existed.
    legacy = [
        item.market_pair_id
        for item in held
        if item.market_pair_id is not None
        and (item.asset_id not in from_cases or from_cases[item.asset_id].pool_locator is None)
    ]
    recorded = (
        {identity.pair_id: identity for identity in await markets.identities(legacy, limit=100)}
        if legacy
        else {}
    )
    found: list[tuple[str, str | None, MarketIdentity | None, str | None]] = []
    for holding in held:
        pair_id = holding.market_pair_id
        if pair_id is None:
            # The holding never recorded which market it came from. Choosing
            # one for it would resolve an ambiguity silently.
            found.append((holding.asset_id, None, None, "POSITION_MARKET_UNKNOWN"))
            continue
        identity = from_cases.get(holding.asset_id)
        observed = recorded.get(pair_id)
        if identity is None:
            # No case-bound entry: the identity recorded for the pool, held to
            # the holding's own chain, network and provider exactly as before.
            if observed is None:
                found.append((holding.asset_id, pair_id, None, "MARKET_NEVER_RECORDED"))
                continue
            if (observed.chain, observed.network, observed.provider) != (
                holding.market_chain,
                holding.market_network,
                holding.market_provider,
            ):
                found.append((holding.asset_id, pair_id, None, "MARKET_IDENTITY_MISMATCH"))
                continue
            found.append((holding.asset_id, pair_id, observed, None))
            continue
        if identity.pool_locator is None and observed is not None:
            # A case opened before pool locators existed names no locator. The
            # recorded identity may complete it — the scout's own rule — and
            # only if it is otherwise exactly this market; nothing is derived.
            if describes_market(observed, identity):
                identity = observed
        scope = MarketScope.held(holding)
        if scope is None or not scope.matches_identity(identity):
            found.append((holding.asset_id, pair_id, None, "MARKET_IDENTITY_MISMATCH"))
            continue
        found.append((holding.asset_id, pair_id, identity, None))
    return found


async def record_observed(
    provider: GeckoTerminalAdapter,
    pair: MarketPair,
    expected: MarketIdentity | None,
    recorder: MarketRecorder,
    within: float,
) -> tuple[AcquisitionOutcome, str | None]:
    """Store one validated observation, held to the identity it was asked as.

    `expected` is the recorded identity the market was planned under, or None
    for a discovered market, which *is* what the answer said. A planned market
    whose answer names anything else — base, payment asset, venue — is refused
    and never stored against a market it is not. Only what the recorder
    confirmed durable counts as written; an event already stored is a replay
    and says so. A write whose outcome is unknown is reported as unknown.
    """
    if expected is not None and pair.market_identity != expected:
        return AcquisitionOutcome.REFUSED, "MARKET_IDENTITY_MISMATCH"
    try:
        written = await asyncio.wait_for(
            record_pair_reporting(provider, pair, recorder), timeout=max(0.001, within)
        )
    except TimeoutError:
        return AcquisitionOutcome.UNKNOWN, "RECORD_OUTCOME_UNKNOWN"
    except ObservationConflict:
        return AcquisitionOutcome.REFUSED, "OBSERVATION_CONFLICT"
    except ValueError:
        return AcquisitionOutcome.REFUSED, "MARKET_IDENTITY_MISMATCH"
    if written.inserted:
        return AcquisitionOutcome.RECORDED, None
    return AcquisitionOutcome.UNCHANGED, "EVENT_ALREADY_RECORDED"


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

        holdings = await self._position_markets()
        held: list[AcquisitionTarget] = []
        unaddressable_positions = 0
        for asset_id, pair_id, identity, reason in holdings:
            if identity is None:
                unaddressable_positions += 1
                refused.append(
                    _refusal(
                        pair_id or asset_id,
                        "unknown",
                        AcquisitionNeed.POSITION_VALUATION,
                        reason or "MARKET_NEVER_RECORDED",
                    )
                )
                continue
            unusable = self._unusable(identity)
            if unusable is not None:
                unaddressable_positions += 1
                refused.append(
                    _refusal(
                        identity.pair_id,
                        identity.chain,
                        AcquisitionNeed.POSITION_VALUATION,
                        unusable,
                    )
                )
                continue
            if any(item.identity == identity for item in held):
                # Several holdings, one market: one observation answers all.
                continue
            held.append(
                AcquisitionTarget(
                    identity=identity,
                    need=AcquisitionNeed.POSITION_VALUATION,
                    chain=self._chains[identity.chain],
                )
            )
        capacity = self._limits.max_position_markets

        # A case needs its own pool and nothing else: a version-3 observation of
        # it carries both the base and the quote asset's USD price, so ANCHOR
        # no longer needs a second market for the payment asset.
        for identity in await self._case_markets():
            admit(identity, AcquisitionNeed.CASE_MARKET)

        return AcquisitionPlan(
            targets=tuple(targets),
            discovery=tuple(self._chains.values())[: self._limits.max_discovery_requests],
            refused=tuple(refused),
            positions=tuple(held[:capacity]),
            position_overflow=tuple(held[capacity:]),
            open_positions=len(holdings),
            unaddressable_positions=unaddressable_positions,
        )

    def _unusable(self, identity: MarketIdentity) -> str | None:
        return unaddressable(identity, self._chains)

    async def _position_markets(
        self,
    ) -> list[tuple[str, str | None, MarketIdentity | None, str | None]]:
        """Every open holding's market, all of them.

        Not bounded by the case budget: a holding that is not observed again
        cannot be marked, and an exit cannot be judged without its mark. The
        plan applies the position budget afterwards and reports what lies
        beyond it.
        """
        return await position_markets(self._sessions, self._markets, None)

    async def _case_markets(self) -> list[MarketIdentity]:
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
        return [MarketIdentity.model_validate(row.market_payload) for row in rows]


@dataclass
class Ledger:
    """What the stage has done, accumulated as it happens.

    Never assembled at the end. An observation that was durably recorded stays
    recorded whatever fails afterwards, and a summary built after a failure
    would report zero and be wrong about the world.
    """

    limits: AcquisitionLimits
    stop: AcquisitionStop = AcquisitionStop.COMPLETED
    # A second fact about the stop, where there is one.
    detail: str | None = None
    entries: list[AcquiredMarket] = field(default_factory=list)
    # One account per bounded discovery read, appended the moment the read is
    # over. Never rebuilt at the end: a read that finished stays reported
    # whatever the next one does.
    reads: list[DiscoveryRead] = field(default_factory=list)
    # Distinct markets asked about by identity, counted as the request goes out.
    requested: int = 0
    # Market-budget slots committed. Committed when work is *triggered*, never
    # when it succeeds: an answer that did not arrive, and one that arrived and
    # was refused, both cost exactly what asking cost, and releasing the slot
    # would let one market's budget buy a second request.
    spent: int = 0
    asked: set[str] = field(default_factory=set)
    # What the plan said about the open portfolio, once there is a plan.
    planned: bool = False
    open_positions: int = 0
    position_markets: int = 0
    unaddressable_positions: int = 0
    # Holding markets put into a provider request, counted as it goes out.
    position_asked: int = 0

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

    def read(
        self, chain: Chain, reserved: int, adapter: GeckoTerminalAdapter, returned: int
    ) -> None:
        """The account of one read that returned, taken from the adapter once.

        Read here and nowhere else, immediately after the read is over and
        before anything is recorded from it. The adapter is this read's own —
        one is built per read and `discover()` clears its counters when it
        starts — so these numbers describe this read and no other, and taking
        them a second time somewhere later could only double-count.
        """
        self.reads.append(
            DiscoveryRead(
                chain=chain.name,
                reserved=reserved,
                completed=True,
                considered=adapter.discovered,
                rejected=adapter.failed,
                returned=returned,
                rejections=tuple(
                    DiscoveryRejection(reason=code.upper(), count=count)
                    for code, count in sorted(adapter.rejections.items())
                ),
            )
        )

    def unfinished_read(self, chain: Chain, reserved: int, reason: str) -> None:
        """A read that did not return, reported as only what is known of it.

        The chain and the capacity it committed are facts this stage established
        before asking. The adapter's counters are not: `discover()` clears them
        on entry and fills them as it parses, so a read that raised or was cut
        off leaves tallies of a pass that never finished. They are left absent
        rather than reported as zero.
        """
        self.reads.append(
            DiscoveryRead(chain=chain.name, reserved=reserved, completed=False, reason=reason)
        )

    @property
    def room(self) -> int:
        """How much of the market budget is still uncommitted."""
        return max(0, self.limits.max_markets - self.spent)

    def commit(self, targets: list[AcquisitionTarget]) -> None:
        """Spend one slot per distinct market, before a single one is asked for.

        A market already asked about in this pass costs nothing further: two
        needs pointing at one market are one request, and deduplication is what
        makes that true rather than an accident of ordering.
        """
        for target in targets:
            if target.identity.pair_id in self.asked:
                continue
            self.asked.add(target.identity.pair_id)
            self.spent += 1
            self.requested += 1

    def reserve(self, capacity: int) -> None:
        """Commit capacity a read is permitted to bring back, before it runs.

        Discovery names no markets in advance, so what is committed is the size
        of the answer it was allowed to return. Whatever it actually returns,
        the request was made under that permission and the permission is spent —
        the alternative is a pass that asks for three and, on being handed one,
        believes it may go and ask for three more.
        """
        self.spent += capacity


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

    async def execute(
        self, deadline: RunDeadline, *, networks: VerifiedNetworkRegistry | None = None
    ) -> MarketAcquisition:
        """Acquire what the open work needs, then hand back an honest account.

        `networks` is the run's registry of chain bindings already validated in
        this pass. Every chain this stage validates is written to it, so the
        pre-risk refresh later in the same run can reuse the binding instead of
        paying for the paginated network scan again.
        """
        ledger = Ledger(limits=self._limits)
        window = Window(self._limits.max_seconds, deadline)
        transport: GeckoTerminalTransport | None = None
        try:
            chains = selected_chains(self._configured)
            if window.expired:
                # Out of time before the first question. Asking one now would
                # start work that is already over budget, and the stop source is
                # somebody else's database.
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return self._summary(ledger, transport)
            halted, detail = await self._halted(window)
            if halted is not None:
                # Read before anything is asked. A stop that is in force, or one
                # that cannot be read at all, means no provider is called —
                # unknown is not permission here either.
                ledger.stop = halted
                ledger.detail = detail
                return self._summary(ledger, transport)
            plan = await self._plan(chains, window)
            ledger.entries.extend(plan.refused)
            ledger.planned = True
            ledger.open_positions = plan.open_positions
            ledger.position_markets = len(plan.positions) + len(plan.position_overflow)
            ledger.unaddressable_positions = plan.unaddressable_positions
            for target in plan.position_overflow:
                # Beyond what one bounded request may carry: visibly not asked
                # about, so the coverage below cannot read as complete.
                ledger.note(target, AcquisitionOutcome.NOT_ATTEMPTED, "POSITION_CAPACITY_EXCEEDED")
            if not plan.targets and not plan.discovery and not plan.positions:
                ledger.stop = AcquisitionStop.NOTHING_TO_ACQUIRE
                return self._summary(ledger, transport)
            transport = GeckoTerminalTransport(
                self._settings, transport=self._http, clock=self._clock
            )
            directory = NetworkDirectory(transport, self._settings, registry=networks)
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

    async def _halted(self, window: Window) -> tuple[AcquisitionStop | None, str | None]:
        """Whether a durable stop forbids calling anybody, and which kind.

        Fails closed on every uncertainty, exactly as intake does: a missing
        stop source and an unreadable one are both *unknown*, and unknown is not
        permission. A provider request is attention and money spent on behalf of
        a system that may have been stopped.

        The kinds are still reported apart. Being stopped is an operator's
        decision working as intended; not being able to tell is a deployment
        that cannot answer a safety question, and reading one as the other would
        hide a broken installation behind a deliberate pause.

        The question itself is bounded by the same window everything else here
        is. It is a read against a database that may be unreachable, and an
        unbounded await on it would hold the whole pass open for as long as that
        database liked — past the run's own deadline, with the run unable to end
        or to report anything. A query cut off that way is *both* facts at once:
        the deadline was reached, and the stop was never confirmed. The second
        decides what happens, because unconfirmed is not permission; the first
        travels beside it, because an operator investigating a slow database and
        one investigating a broken pause source do different things.

        `wait_for` cancels the query and awaits that cancellation, so nothing
        continues against the database once this returns.
        """
        if self._pause is None:
            return AcquisitionStop.SYSTEM_STOP_UNREADABLE, None
        try:
            paused = await asyncio.wait_for(
                self._pause.system_paused(), timeout=max(0.001, window.remaining)
            )
        except TimeoutError:
            return (
                AcquisitionStop.SYSTEM_STOP_UNREADABLE,
                AcquisitionStop.TIME_BUDGET_REACHED.value,
            )
        return (AcquisitionStop.SYSTEM_STOPPED if paused else None), None

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
        held = await self._positions(chains, plan, adapter, recorder, ledger, window)
        if held is None:
            return False
        for chain in chains:
            wanted = [item for item in plan.targets if item.identity.chain == chain.name]
            # A case on a holding's market is answered by the holding's reading
            # — one market, one observation — and asks nothing further.
            for target in [item for item in wanted if item.identity.pair_id in held]:
                prior = held[target.identity.pair_id]
                if isinstance(prior, str):
                    ledger.note(target, AcquisitionOutcome.REFUSED, prior)
                elif not await self._record(prior[0], prior[1], target, recorder, ledger, window):
                    return False
            wanted = [item for item in wanted if item.identity.pair_id not in held]
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
            # Committed before the request leaves, so nothing about the answer
            # can give the budget back.
            ledger.commit(batch)
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

    async def _positions(
        self,
        chains: tuple[Chain, ...],
        plan: AcquisitionPlan,
        adapter: Callable[[Chain, int], GeckoTerminalAdapter],
        recorder: MarketRecorder,
        ledger: Ledger,
        window: Window,
    ) -> "dict[str, tuple[GeckoTerminalAdapter, MarketPair] | str] | None":
        """Every open holding's market, asked about first and in its own budget.

        One bounded `pools/multi` request per chain carries them all; the case
        budget is not touched, so neither a case nor discovery can take a
        holding's slot. Returns, per pool asked about, the reading it got or
        why it got none — or None when the pass must end here.
        """
        answered: dict[str, tuple[GeckoTerminalAdapter, MarketPair] | str] = {}
        for chain in chains:
            batch = [item for item in plan.positions if item.identity.chain == chain.name]
            if not batch:
                continue
            if window.expired:
                self._unattempted(batch, ledger, "TIME_BUDGET_REACHED")
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return None
            pools: dict[str, AcquisitionTarget] = {}
            for target in batch:
                pools.setdefault(target.identity.pair_id, target)
            # Counted before the request leaves, like every other request here.
            ledger.asked.update(pools)
            ledger.requested += len(pools)
            ledger.position_asked += len(batch)
            built = adapter(chain, self._settings.geckoterminal_pools_per_chain)
            try:
                confirmed = await asyncio.wait_for(
                    built.observe(tuple(item.locator for item in pools.values())),
                    timeout=max(0.001, window.remaining),
                )
            except TimeoutError:
                self._failed(batch, ledger, "TIME_BUDGET_REACHED")
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return None
            except ProviderError as error:
                code = _provider_code(error)
                self._failed(batch, ledger, code)
                answered.update(dict.fromkeys(pools, code))
                if isinstance(error, UnsupportedNetworkError):
                    continue
                ledger.stop = AcquisitionStop.PROVIDER_FAILED
                return None
            returned = {pair.pair_id: pair for pair in confirmed}
            for target in batch:
                pair = returned.get(target.identity.pair_id)
                if pair is None:
                    # Asked about and not returned: left exactly as it was,
                    # never answered with something older or something else.
                    ledger.note(target, AcquisitionOutcome.REFUSED, "MARKET_NOT_RETURNED")
                    answered[target.identity.pair_id] = "MARKET_NOT_RETURNED"
                    continue
                answered[target.identity.pair_id] = (built, pair)
                if not await self._record(built, pair, target, recorder, ledger, window):
                    return None
        return answered

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
            allowance = min(self._settings.geckoterminal_pools_per_chain, ledger.room)
            # Reserved before the read, for the same reason the targeted batch
            # is: the request is made under this permission whatever comes back.
            ledger.reserve(allowance)
            built = adapter(chain, allowance)
            try:
                found = await asyncio.wait_for(
                    built.discover(), timeout=max(0.001, window.remaining)
                )
            except TimeoutError:
                ledger.unfinished_read(chain, allowance, AcquisitionStop.TIME_BUDGET_REACHED.value)
                ledger.stop = AcquisitionStop.TIME_BUDGET_REACHED
                return False
            except ProviderError as error:
                ledger.unfinished_read(chain, allowance, _provider_code(error))
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
            # Before a single recording, so that what the read established
            # survives whatever the recorder then meets.
            ledger.read(chain, allowance, built, len(found))
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

        A market that was *planned* is additionally held to the identity it was
        planned as. The checks below it are real but narrower than they look:
        `observe` compares the pair identifier, which for a pool address carries
        the chain, the network and the pool and nothing else; and
        `record_pair_reporting` compares the snapshot against the pair from the
        same answer, which agrees with itself by construction. So an answer that
        kept the requested pool and named a different base asset, payment asset
        or venue would pass both and be stored against a market it is not. The
        comparison here is the recorded identity's own equality — one contract,
        not a second definition — and nothing is repaired, mapped or guessed:
        a disagreement is refused and the market is left as it was.

        A discovered market has no planned identity to be held to. It *is* what
        the answer said, judged by intake under its own rules.
        """
        outcome, reason = await record_observed(
            provider,
            pair,
            None if target.need is AcquisitionNeed.NEW_CANDIDATE else target.identity,
            recorder,
            window.remaining,
        )
        ledger.note(target, outcome, reason)
        if outcome is AcquisitionOutcome.UNKNOWN:
            # It may have committed and it may not, and this run cannot tell.
            # Saying so is the only honest answer, and it is also what stops the
            # trading half from proceeding over data of unstated provenance.
            ledger.stop = AcquisitionStop.OUTCOME_UNKNOWN
            return False
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
            detail=ledger.detail,
            limits=self._limits,
            requested=ledger.requested,
            budget_spent=ledger.spent,
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
            # What each bounded discovery read asked for and what the adapter
            # made of the answer. Reported apart from the totals above, which
            # mix discovery with the targeted reads and cannot be split back up.
            discovery=tuple(ledger.reads[:4]),
            positions=self._coverage(ledger) if ledger.planned else None,
        )

    @staticmethod
    def _coverage(ledger: Ledger) -> PositionCoverage:
        held = [
            item for item in ledger.entries if item.need == AcquisitionNeed.POSITION_VALUATION.value
        ]

        def count(*outcomes: AcquisitionOutcome) -> int:
            values = {item.value for item in outcomes}
            return len([item for item in held if item.outcome in values])

        return PositionCoverage(
            open_positions=ledger.open_positions,
            markets=ledger.position_markets,
            unaddressable=ledger.unaddressable_positions,
            asked=ledger.position_asked,
            answered=count(AcquisitionOutcome.RECORDED, AcquisitionOutcome.UNCHANGED),
            # Planning refusals are counted as unaddressable, not here.
            refused=max(0, count(AcquisitionOutcome.REFUSED) - ledger.unaddressable_positions),
            failed=count(AcquisitionOutcome.FAILED),
            unknown=count(AcquisitionOutcome.UNKNOWN),
            not_attempted=count(AcquisitionOutcome.NOT_ATTEMPTED),
        )


def disabled(reason: AcquisitionStop = AcquisitionStop.NOT_ENABLED) -> MarketAcquisition:
    """The account of a stage that did not run. Reported rather than omitted."""
    return MarketAcquisition(enabled=False, stop=reason.value)
