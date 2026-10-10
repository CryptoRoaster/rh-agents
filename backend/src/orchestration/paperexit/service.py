"""The one narrow server-side call that closes an open PAPER position.

Everything happens in a single transaction, in the order the rest of this system
already established: paper account, then trade case. The SELL fill, the ledger
postings, the realised result and the exit's own durable record commit together
or not at all.

The caller names the position and an idempotent key. It supplies no quantity,
price, mark, limit or portfolio value: the quantity is the whole open holding,
read under the account lock and bound to the order that read it.

Three decisions this module rests on, recorded here because they are the
reason it looks the way it does.

**The entry's approval authorises nothing.** It permitted a purchase, was spent
on one, and is referenced by the entry's own record. The exit asks
`src.risk.engine.evaluate` again, for a SELL, at its own instant. What SENTINEL
already checks for a sale it still checks; what it checks only for a purchase is
not reinstated here, and nothing it refuses is overridden.

**There is no new workflow status.** The entry case stays `EXECUTED`, which is
terminal and still bars its market from opening another case. A transition out
of a terminal status would have to be invented, and the only thing it could
mean — "this market is available again" — is the re-entry contract that
deliberately does not exist yet. The exit is recorded beside the case instead.

**Nothing here is an emergency.** A kill switch, `OBSERVE`, an unreadable stop
and a durable pause all refuse the exit exactly as they refuse an entry. A
position that cannot be sold within the limits is not sold.

**An automatic trigger is judged again, on fresh evidence.** A sweep decides a
trigger on the marks recorded when it started, and the exit's own on-chain read
then takes seconds — tens of them on a slow chain. By the time the sale is
judged, the mark the trigger saw can be older than SENTINEL's bound, and the
price that fired a stop may have recovered. So a sweep passes a `confirm` step,
and the order of work is fixed:

1. the exit's own on-chain read (slow, before any lock);
2. if what it measured is already older than SENTINEL's bound, nothing more is
   asked: the sale cannot pass the final check, and no provider is called for it;
3. `confirm`: the held market observed again — one bounded, exact-locator
   refresh — and the policy evaluated again on that reading, with its historical
   peak; a trigger that has gone is no sale, a changed one is recorded as changed;
4. the portfolio valued on what was just recorded, then the locks;
5. under the locks, the held market's reading must be exactly the one `confirm`
   judged, and SENTINEL's bound is applied at the decision and again at the
   execution boundary — a reading that aged past it in between is not booked.

No lock is held across the chain read or the refresh, a stop in force asks no
provider, and nothing a refresh failed to show fresh is replaced by older data.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import (
    ExecutionTiming,
    MarketSnapshot,
    Position,
    RiskDecision,
    RiskLimits,
    SafetyStatus,
    Side,
    TradeIntent,
    TradingMode,
)
from src.data.repository import aware, read_position
from src.data.tables import (
    AccountRow,
    IntentRow,
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
    TradeCycleRow,
)
from src.ledger.portfolio import portfolio_basis, portfolio_state
from src.markets.models import Availability, MarketIdentity
from src.markets.models import MarketSnapshot as RecordedSnapshot
from src.markets.scope import HeldMarketFeed, MarketScope, describes_market
from src.orchestration.casefill.models import notional_of
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.paper import PaperOutcome, PaperTradingService
from src.orchestration.paperexit.exitread import (
    ExitOnchainRead,
    ExitOnchainReadPort,
    ExitReadUnavailable,
)
from src.orchestration.paperexit.models import (
    ExitFreshness,
    ExitReading,
    ExitRefusal,
    ExitRefused,
    PaperExitRecorded,
)
from src.orchestration.riskdata.context import RiskDataReader
from src.orchestration.riskdata.models import RiskDataReadiness
from src.orchestration.riskrequest.service import (
    OneSnapshot,
    market_view,
    risk_market,
    too_old_for,
)
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.valuation.models import PortfolioValuation, unvaluable_reason
from src.orchestration.valuation.service import PositionValuationReader, held_market_identities
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    EvidenceType,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService, case_from_row


class _Abort(Exception):  # noqa: N818 - carries a reading, not an error condition
    """Roll the transaction back and answer with this refusal.

    The decision, the intent and the market are already staged by the time the
    execution boundary is reached. Returning normally would commit them;
    raising rolls them back, and the reading survives so the caller still gets a
    typed answer rather than an exception.
    """

    def __init__(self, refusal: ExitRefused) -> None:
        self.refusal = refusal
        super().__init__(refusal.reason.value)


@dataclass(frozen=True)
class ExitTriggerRecord:
    """Why an automatic policy asked for this exit. Recorded, never obeyed.

    Plain values so this module depends on no policy: the policy depends on it.
    """

    trigger: str
    policy_version: str
    basis: dict[str, Any]


@dataclass(frozen=True)
class ExitConfirmation:
    """A trigger judged again on the held market observed after the slow read.

    `trigger` is the policy's answer on that evidence: `None` means the trigger
    has gone. `refusal` means nothing could be judged — the refresh failed or
    its budget ended — and names why. `snapshot_id` is the held market's reading
    the answer rests on; the sale is bound to exactly that reading.
    """

    trigger: ExitTriggerRecord | None
    snapshot_id: UUID | None = None
    refusal: str | None = None
    original_trigger: str | None = None
    refresh_attempts: int = 0
    refresh_provider_requests: int = 0
    refresh_seconds: float | None = None
    refresh_reason: str | None = None
    mark_age_seconds: float | None = None


ExitConfirm = Callable[[TradeCase], Awaitable[ExitConfirmation]]


@dataclass
class _Trace:
    """What one attempt measured about its own evidence. Counts and ages only."""

    atlas_read_seconds: float | None = None
    confirmation: ExitConfirmation | None = None
    mark_age_at_final_seconds: float | None = None
    skipped: str | None = None

    def freshness(self, trigger: ExitTriggerRecord | None) -> ExitFreshness | None:
        confirmation = self.confirmation
        if confirmation is None and trigger is None:
            return None
        if confirmation is None:
            return ExitFreshness(
                original_trigger=_code_or_none(trigger.trigger if trigger else None),
                atlas_read_seconds=self.atlas_read_seconds,
                refresh_reason=_code_or_none(self.skipped),
                mark_age_at_final_seconds=self.mark_age_at_final_seconds,
            )
        reevaluated = None if confirmation.trigger is None else confirmation.trigger.trigger
        return ExitFreshness(
            original_trigger=_code_or_none(confirmation.original_trigger),
            reevaluated_trigger=_code_or_none(reevaluated),
            trigger_cleared=confirmation.refusal is None and confirmation.trigger is None,
            atlas_read_seconds=self.atlas_read_seconds,
            refresh_attempts=min(1, confirmation.refresh_attempts),
            refresh_provider_requests=confirmation.refresh_provider_requests,
            refresh_seconds=confirmation.refresh_seconds,
            refresh_reason=_code_or_none(confirmation.refresh_reason),
            mark_age_at_reevaluation_seconds=confirmation.mark_age_seconds,
            mark_age_at_final_seconds=self.mark_age_at_final_seconds,
        )


class PaperExitUnavailable(Exception):
    """The call could not be attempted at all. Safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class PaperExitService:
    """Closes one open, case-bound PAPER position, once."""

    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    paper: PaperTradingService
    markets: Any
    costs: PaperCostReading
    trading_mode: TradingMode = TradingMode.OBSERVE
    kill_switch: bool = False
    # Supplied by a deployment that also runs the accounting subsystem. Its
    # presence says the stop is configured; its *value* is never consulted,
    # because the locked account row is the authoritative read.
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    include_fixtures: bool = False
    # When supplied, a sale is judged on its own fresh on-chain read rather than
    # on the entry's evidence. The entry's evidence belongs to a terminal case
    # and ages past SENTINEL's bound within seconds; the fresh read is taken
    # when the sale is asked for and never written to the case.
    exit_read: ExitOnchainReadPort | None = None

    @property
    def limits(self) -> RiskLimits:
        """SENTINEL's limits, from the one place that owns them."""
        return self.paper.limits

    async def execute_position_exit(
        self,
        position_id: UUID,
        *,
        request_key: str,
        trigger: ExitTriggerRecord | None = None,
        confirm: ExitConfirm | None = None,
    ) -> ExitReading:
        """Sell the whole open holding of one position, once, or say why not.

        `trigger` is recorded with the exit when an automatic policy asked for
        it. It authorises nothing: every check below runs exactly as for a
        direct request, SENTINEL's SELL verdict included.

        `confirm`, supplied by an automatic policy, observes the held market
        again after the exit's own on-chain read and judges the trigger again on
        that reading. Its answer replaces `trigger`; no answer, no sale.
        """
        trace = _Trace()
        try:
            reading = await self._attempt(
                position_id, request_key=request_key, trigger=trigger, confirm=confirm, trace=trace
            )
        except _Abort as abort:
            # The transaction has rolled back; the answer survives it.
            reading = abort.refusal
        freshness = trace.freshness(trigger)
        if freshness is None or reading.replayed:
            return reading
        return reading.model_copy(update={"freshness": freshness})

    async def _attempt(
        self,
        position_id: UUID,
        *,
        request_key: str,
        trigger: ExitTriggerRecord | None,
        confirm: ExitConfirm | None = None,
        trace: _Trace | None = None,
    ) -> ExitReading:
        trace = trace if trace is not None else _Trace()
        feed = OneSnapshot(self.markets, self.include_fixtures)
        intent_id = _intent_identity(request_key)
        # History needs no current prices. Checked before anything is valued, so
        # replaying a stored outcome — a completed sale or a stored refusal —
        # never depends on the market layer being reachable. The authoritative
        # replay still happens under the locks, on the intent identity.
        fresh: ExitOnchainRead | str | None = None
        if await self._already_decided(intent_id):
            valuation = PortfolioValuation()
        else:
            if self.exit_read is not None:
                # Before any lock: a chain read is a network call, and holding
                # the account lock across one is a latency every other writer
                # pays for. What it read is bound to the case again under the
                # lock.
                started = time.monotonic()
                fresh = await self._fresh_read(position_id, request_key)
                trace.atlas_read_seconds = round(time.monotonic() - started, 3)
            if confirm is not None:
                # After the slow read, never before it: an observation taken
                # first would age by exactly the read's duration.
                trace.confirmation = await self._confirm(position_id, fresh, confirm, trace)
            # Every open holding is priced before the account lock is taken.
            # Holding a portfolio-wide lock across an injected port is a latency
            # somebody else pays for; what that costs is the chance of a
            # position appearing in between, and the check under the lock closes
            # it.
            valuation = await self._value_portfolio(feed)

        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise PaperExitUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
            row = await session.get(PositionRow, position_id)
            if row is None:
                return _refused(position_id, ExitRefusal.POSITION_NOT_FOUND)
            position = read_position(row)

            # History first, and deliberately before every authorization check.
            # A completed sale comes back as it was recorded even once the
            # approval behind it has long expired: that is what happened, not a
            # new permission. A refusal comes back the same way — one order gets
            # one verdict, and a refused sale is not retried into a yes.
            stored_intent = await session.get(IntentRow, intent_id)
            if stored_intent is not None:
                return await self._replay(session, position, stored_intent, request_key)

            if position.quantity <= 0:
                # Nothing is held. Not a second sale — no sale at all.
                return _refused(position_id, ExitRefusal.POSITION_ALREADY_CLOSED)

            entry = await self._entry(session, position)
            if isinstance(entry, ExitRefusal):
                return _refused(position_id, entry)
            try:
                case_row = await self.cases._locked_case(session, entry.trade_case_id)
            except WorkflowFailure:
                raise PaperExitUnavailable("TRADE_CASE_NOT_FOUND") from None
            trade_case = case_from_row(case_row)
            if trade_case.status is not TradeCaseStatus.EXECUTED:
                # `EXECUTED` is terminal, so unlike an approval it cannot lapse
                # between being read and being relied on. The status read under
                # this lock is therefore the whole eligibility question, and no
                # second workflow opinion is formed here.
                return _refused(
                    position_id,
                    ExitRefusal.ENTRY_NOT_EXECUTED,
                    trade_case_id=trade_case.id,
                    detail=trade_case.status.value,
                )
            if not _same_market(position, trade_case.market):
                return _refused(
                    position_id, ExitRefusal.POSITION_MARKET_MISMATCH, trade_case_id=trade_case.id
                )

            refusal = self._stops(position_id, trade_case, account)
            if refusal is not None:
                return refusal
            confirmation = trace.confirmation
            if confirm is not None:
                # The trigger as judged on fresh evidence, after every stop: a
                # stop in force is reported as the stop, whatever the trigger.
                answer = self._confirmed(position_id, trade_case, trace)
                if isinstance(answer, ExitRefused):
                    return answer
                trigger = answer

            positions = await self.paper.positions_in_session(session)
            held = {item.asset_id for item in positions if item.quantity != 0}
            bound = None if confirmation is None else _Bound(confirmation.snapshot_id)
            if self.exit_read is not None:
                basis = await self._fresh_basis(
                    feed, position_id, trade_case, fresh, request_key, valuation, held, bound
                )
            else:
                basis = await self._entry_basis(
                    feed, position_id, trade_case, request_key, valuation, held, bound
                )
            if isinstance(basis, ExitRefused):
                return basis
            market, readiness, now = basis
            trace.mark_age_at_final_seconds = _age(now, market.observed_at)
            stale = too_old_for(market, now, self.limits)
            if stale is not None:
                # Present, provable and still older than SENTINEL's own bound.
                return _refused(
                    position_id,
                    ExitRefusal.SOURCE_OLDER_THAN_RISK_LIMIT,
                    trade_case_id=trade_case.id,
                    readiness=readiness,
                    detail=stale,
                )
            valued = portfolio_state(
                cash_usd=account.cash_usd,
                realized_loss_today_usd=account.realized_loss_today_usd,
                positions=positions,
                asset_id=market.asset_id,
                price_usd=market.price_usd,
                marks=valuation.by_asset,
                now=now,
                max_snapshot_age_seconds=self.limits.max_snapshot_age_seconds,
                correlation_id=trade_case.correlation_id,
                market=trade_case.market,
                cycle_id=position.cycle_id,
            )
            if valued.unmarked_assets:
                # SENTINEL would answer `PORTFOLIO_DATA_UNKNOWN`. A holding this
                # system cannot value is a missing capability, not a verdict.
                return _refused(
                    position_id,
                    ExitRefusal.PORTFOLIO_MARKS_UNAVAILABLE,
                    trade_case_id=trade_case.id,
                    readiness=readiness,
                    detail=unvaluable_reason(valuation, valued.unmarked_assets),
                )
            if valued.accounting_issues:
                # Every mark is known; the portfolio's figure is not
                # representable in the ledger. An exit that cannot be valued is
                # not executed, and this is no emergency exception to that.
                return _refused(
                    position_id,
                    ExitRefusal.PORTFOLIO_ACCOUNTING_UNREPRESENTABLE,
                    trade_case_id=trade_case.id,
                    readiness=readiness,
                    detail=valued.accounting_issues[0].value,
                )

            # The whole open holding, as it stands under this lock, fixed into
            # the order now. A later different holding does not become a
            # different order: the intent identity is the key's, so a changed
            # quantity is a conflicting reuse rather than a second sale.
            if not isinstance(self.costs, PaperCostAssumptions):  # pragma: no cover
                raise PaperExitUnavailable("CANONICAL_INPUTS_INCONSISTENT")  # guarded by the basis
            intent = _exit_intent(position, trade_case, market, self.costs, request_key, now)

            def still_authorised(at: datetime) -> str | None:
                """Every governing validity, re-checked at the actual boundary.

                Synchronous and over inputs already loaded under the locks, so
                nothing between this answer and the fill touches the database.
                """
                trace.mark_age_at_final_seconds = _age(at, market.observed_at)
                if readiness is not None and not readiness.is_current_at(at):
                    return "DECISION_BASIS_EXPIRED"
                if valuation.stale_at(at, self.limits.max_snapshot_age_seconds):
                    return "POSITION_VALUATION_STALE"
                return too_old_for(market, at, self.limits)

            outcome = await self.paper.execute_in_session(
                session,
                account,
                intent,
                market,
                positions=positions,
                marks=valuation.by_asset,
                now=now,
                market_identity=trade_case.market,
                cycle_id=position.cycle_id,
                authorize=still_authorised,
            )
            if outcome.stop_reason is not None:
                # Approved, and the world moved on before it could be acted on.
                # Everything started is rolled back, and no artificial final
                # risk rejection is written in its place.
                raise _Abort(
                    _refused(
                        position_id,
                        ExitRefusal.EXECUTION_WINDOW_EXPIRED,
                        trade_case_id=trade_case.id,
                        readiness=readiness,
                        detail=outcome.stop_reason,
                    )
                )
            if outcome.fill is None:
                return _refused(
                    position_id,
                    ExitRefusal.EXIT_RISK_REFUSED,
                    trade_case_id=trade_case.id,
                    readiness=readiness,
                    outcome=outcome.decision,
                )
            return self._record(
                session,
                position,
                trade_case,
                entry,
                outcome,
                market,
                now,
                request_key,
                trigger,
                fresh if isinstance(fresh, ExitOnchainRead) else None,
                trace,
            )

    # ------------------------------------------------------------ the basis

    async def _entry_basis(
        self,
        feed: OneSnapshot,
        position_id: UUID,
        trade_case: TradeCase,
        request_key: str,
        valuation: PortfolioValuation,
        held: set[str],
        bound: "_Bound | None" = None,
    ) -> "tuple[MarketSnapshot, RiskDataReadiness | None, datetime] | ExitRefused":
        """The sale judged on the entry's own evidence, while it is current.

        Only for a service composed without a fresh exit read. The entry's
        on-chain evidence ages past SENTINEL's bound within seconds of the fill,
        so this basis refuses almost every later exit, fail closed.
        """
        # Every read of the held pool in this basis — the completeness check's
        # included — is of the held market: its provider, chain, network,
        # pool and assets. Another source's reading is never selected.
        held_market = HeldMarketFeed(feed, MarketScope.of(trade_case.market))
        readiness = await RiskDataReader(
            cases=self.cases,
            markets=held_market,
            costs=self.costs,
            clock=self.clock,
            pause=self.pause,
            include_fixtures=self.include_fixtures,
        ).readiness(trade_case.id)
        snapshot = await held_market.latest(trade_case.market.pair_id)
        current = active_evidence(await self.cases.evidence(trade_case.id))
        appeared = valuation.unconsidered(held)
        if appeared:
            # The portfolio moved while it was being valued, so the figure
            # SENTINEL would judge covers only part of it.
            return _refused(
                position_id,
                ExitRefusal.PORTFOLIO_CHANGED_DURING_VALUATION,
                trade_case_id=trade_case.id,
                readiness=readiness,
            )
        # The reading every check below would describe must be of the held
        # market, before completeness or freshness is even asked about it.
        if snapshot is not None and not _reading_of(snapshot, trade_case.market):
            return _refused(
                position_id,
                ExitRefusal.EXIT_MARKET_IDENTITY_MISMATCH,
                trade_case_id=trade_case.id,
            )
        if bound is not None and not bound.holds(snapshot):
            return _refused(
                position_id, ExitRefusal.EXIT_EVIDENCE_CHANGED, trade_case_id=trade_case.id
            )

        # The last clock read, after the last input read. Everything from
        # here to the verdict is synchronous, so one instant governs every
        # source age, the UTC loss day and SENTINEL itself.
        now = self.clock.now()

        if not readiness.complete:
            return _refused(
                position_id,
                ExitRefusal.RISK_DATA_INCOMPLETE,
                trade_case_id=trade_case.id,
                readiness=readiness,
            )
        if not readiness.is_current_at(now):
            return _refused(
                position_id,
                ExitRefusal.DECISION_BASIS_EXPIRED,
                trade_case_id=trade_case.id,
                readiness=readiness,
            )
        onchain = current.get(EvidenceType.ONCHAIN)
        anchor = current.get(EvidenceType.LIQUIDITY_EXECUTION)
        if snapshot is None or onchain is None or anchor is None:
            raise PaperExitUnavailable("CANONICAL_INPUTS_INCONSISTENT")
        price = reference_price(snapshot)
        metadata = base_asset_metadata(snapshot)
        if price is None or metadata is None or not isinstance(self.costs, PaperCostAssumptions):
            raise PaperExitUnavailable("CANONICAL_INPUTS_INCONSISTENT")
        market = risk_market(
            base_asset_id=trade_case.market.base_asset_id,
            price=price,
            base_asset=metadata,
            snapshot=snapshot,
            onchain=onchain,
            anchor=anchor,
            costs=self.costs,
            correlation_id=trade_case.correlation_id,
            # Bound to this exit: a different reading at a different instant
            # from the entry's, and giving it the entry's identity would
            # make two snapshots look like one.
            identity_key=f"{request_key}:exit",
            side=Side.SELL,
        )
        return market, readiness, now

    async def _fresh_read(
        self, position_id: UUID, request_key: str
    ) -> ExitOnchainRead | str | None:
        """The exit's own on-chain read of the held token, or why none exists.

        `None` means the position cannot be placed in a case here; the locked
        path answers that with its own, authoritative refusal.
        """
        if self.exit_read is None:  # pragma: no cover - only called when configured
            return None
        async with self.sessions() as session:
            row = await session.get(PositionRow, position_id)
            if row is None:
                return None
            position = read_position(row)
            if position.quantity <= 0:
                return None
            entry = await self._entry(session, position)
        if isinstance(entry, ExitRefusal):
            return None
        try:
            trade_case = await self.cases.get_trade_case(entry.trade_case_id)
        except WorkflowFailure:
            return None
        try:
            return await self.exit_read.read(trade_case.id, trade_case.market, request_key)
        except ExitReadUnavailable as error:
            return error.reason_code

    async def _fresh_basis(
        self,
        feed: OneSnapshot,
        position_id: UUID,
        trade_case: TradeCase,
        fresh: ExitOnchainRead | str | None,
        request_key: str,
        valuation: PortfolioValuation,
        held: set[str],
        bound: "_Bound | None" = None,
    ) -> "tuple[MarketSnapshot, RiskDataReadiness | None, datetime] | ExitRefused":
        """The sale judged on a fresh read: the held market now, the chain now.

        Every input is this exit's own. Anything missing is a refusal, never a
        default: no market, no price, no metadata, no cost basis, no read or no
        holder measurement means no sale. What remains is SENTINEL's to judge,
        with the SELL semantics it owns.
        """
        # Every read of the held pool in this basis — the completeness check's
        # included — is of the held market: its provider, chain, network,
        # pool and assets. Another source's reading is never selected.
        held_market = HeldMarketFeed(feed, MarketScope.of(trade_case.market))
        snapshot = await held_market.latest(trade_case.market.pair_id)
        appeared = valuation.unconsidered(held)
        if appeared:
            return _refused(
                position_id,
                ExitRefusal.PORTFOLIO_CHANGED_DURING_VALUATION,
                trade_case_id=trade_case.id,
            )
        now = self.clock.now()

        def incomplete(detail: str) -> ExitRefused:
            return _refused(
                position_id,
                ExitRefusal.EXIT_DATA_INCOMPLETE,
                trade_case_id=trade_case.id,
                detail=detail,
            )

        if not isinstance(fresh, ExitOnchainRead):
            return _refused(
                position_id,
                ExitRefusal.EXIT_READ_UNAVAILABLE,
                trade_case_id=trade_case.id,
                detail=fresh if isinstance(fresh, str) else "EXIT_READ_NOT_TAKEN",
            )
        if fresh.trade_case_id != trade_case.id:
            # Read for a different case than the one now locked.
            return _refused(
                position_id,
                ExitRefusal.EXIT_READ_UNAVAILABLE,
                trade_case_id=trade_case.id,
                detail="EXIT_READ_CASE_MISMATCH",
            )
        if snapshot is None:
            return incomplete("MARKET_UNAVAILABLE")
        if not _reading_of(snapshot, trade_case.market):
            return _refused(
                position_id,
                ExitRefusal.EXIT_MARKET_IDENTITY_MISMATCH,
                trade_case_id=trade_case.id,
            )
        if bound is not None and not bound.holds(snapshot):
            # Not the reading the trigger was judged again on. A newer one may
            # say something else, and it was never asked.
            return _refused(
                position_id, ExitRefusal.EXIT_EVIDENCE_CHANGED, trade_case_id=trade_case.id
            )
        price = reference_price(snapshot)
        if price is None:
            return incomplete("REFERENCE_PRICE_UNAVAILABLE")
        metadata = base_asset_metadata(snapshot)
        if metadata is None:
            return incomplete("TOKEN_METADATA_UNAVAILABLE")
        if not isinstance(self.costs, PaperCostAssumptions):
            return incomplete("COST_BASIS_UNAVAILABLE")
        intelligence = fresh.payload.intelligence
        holders = None if intelligence is None else intelligence.holders
        if holders is None:
            return incomplete("HOLDER_FACTS_UNAVAILABLE")
        liquidity = snapshot.liquidity
        # The route a sale needs is the pool the holding was bought in, observed
        # again and still holding liquidity. Unobserved or empty is unknown.
        routing = (
            SafetyStatus.PASS
            if liquidity.status is Availability.AVAILABLE
            and liquidity.value_usd is not None
            and liquidity.value_usd > 0
            else SafetyStatus.UNKNOWN
        )
        market = market_view(
            base_asset_id=trade_case.market.base_asset_id,
            price=price,
            base_asset=metadata,
            snapshot=snapshot,
            onchain=fresh.payload,
            holders=holders,
            routing=routing,
            costs=self.costs,
            correlation_id=trade_case.correlation_id,
            identity_key=f"{request_key}:exit",
            side=Side.SELL,
        )
        return market, None, now

    # ------------------------------------------------------------- the trigger

    async def _confirm(
        self,
        position_id: UUID,
        fresh: ExitOnchainRead | str | None,
        confirm: ExitConfirm,
        trace: _Trace,
    ) -> ExitConfirmation | None:
        """Observe the held market again and judge the trigger again, or don't.

        Nothing is asked of a provider when the sale is already known to fail:
        a stop in force, a chain read that was not taken, or a chain read whose
        holder measurement is already past SENTINEL's bound. Each of those is
        refused under the lock by its own, authoritative name.
        """
        if not isinstance(fresh, ExitOnchainRead) and self.exit_read is not None:
            trace.skipped = "EXIT_READ_NOT_TAKEN"
            return None
        if await self._stop_in_force():
            trace.skipped = "STOP_IN_FORCE"
            return None
        if isinstance(fresh, ExitOnchainRead):
            intelligence = fresh.payload.intelligence
            holders = None if intelligence is None else intelligence.holders
            if holders is None:
                trace.skipped = "HOLDER_FACTS_UNAVAILABLE"
                return None
            age = _age(self.clock.now(), holders.observed_at)
            if age is None or age > self.limits.max_snapshot_age_seconds:
                # The chain read itself outlived the bound. A market refresh
                # now could not make this sale pass the final check.
                trace.skipped = "HOLDERS_OLDER_THAN_RISK_LIMIT"
                return None
        async with self.sessions() as session:
            row = await session.get(PositionRow, position_id)
            entry = None if row is None else await self._entry(session, read_position(row))
        if entry is None or isinstance(entry, ExitRefusal):
            trace.skipped = "POSITION_NOT_PLACEABLE"
            return None
        try:
            trade_case = await self.cases.get_trade_case(entry.trade_case_id)
        except WorkflowFailure:
            trace.skipped = "TRADE_CASE_NOT_FOUND"
            return None
        return await confirm(trade_case)

    def _confirmed(
        self, position_id: UUID, trade_case: TradeCase, trace: _Trace
    ) -> ExitTriggerRecord | ExitRefused:
        """The trigger to sell on, as judged again — or why there is none."""
        confirmation = trace.confirmation
        if confirmation is None:
            skipped = trace.skipped or "PRE_EXIT_REFRESH_NOT_TAKEN"
            if skipped == "HOLDERS_OLDER_THAN_RISK_LIMIT":
                return _refused(
                    position_id,
                    ExitRefusal.SOURCE_OLDER_THAN_RISK_LIMIT,
                    trade_case_id=trade_case.id,
                    detail=skipped,
                )
            if skipped == "EXIT_READ_NOT_TAKEN" or skipped == "HOLDER_FACTS_UNAVAILABLE":
                # Refused below by the basis, by its own name.
                return _refused(
                    position_id,
                    ExitRefusal.EXIT_READ_UNAVAILABLE
                    if skipped == "EXIT_READ_NOT_TAKEN"
                    else ExitRefusal.EXIT_DATA_INCOMPLETE,
                    trade_case_id=trade_case.id,
                    detail=skipped,
                )
            return _refused(
                position_id,
                ExitRefusal.PRE_EXIT_REFRESH_FAILED,
                trade_case_id=trade_case.id,
                detail=skipped,
            )
        if confirmation.refusal is not None:
            return _refused(
                position_id,
                ExitRefusal.PRE_EXIT_REFRESH_FAILED,
                trade_case_id=trade_case.id,
                detail=_code_or_none(confirmation.refusal) or "PRE_EXIT_REFRESH_FAILED",
            )
        if confirmation.trigger is None:
            return _refused(
                position_id,
                ExitRefusal.EXIT_TRIGGER_CLEARED,
                trade_case_id=trade_case.id,
                detail=_code_or_none(confirmation.original_trigger),
            )
        return confirmation.trigger

    async def _stop_in_force(self) -> bool:
        """Whether a stop already refuses this sale. Unlocked: a hint, never the verdict."""
        if self.kill_switch or self.limits.kill_switch:
            return True
        if self.trading_mode is not TradingMode.PAPER or self.pause is None:
            return True
        async with self.sessions() as session:
            paused = await session.scalar(select(AccountRow.paused).where(AccountRow.id == 1))
        return paused is None or bool(paused)

    # ------------------------------------------------------------------ reads

    async def _already_decided(self, intent_id: UUID) -> bool:
        """Whether this key's one order already has an outcome on file.

        Not authoritative and not meant to be — `replay_in_session` answers
        under the locks. This only decides whether to do work that cannot change
        the answer, at a moment the market layer may not even be reachable.
        """
        async with self.sessions() as session:
            return (
                await session.scalar(select(IntentRow.id).where(IntentRow.id == intent_id))
                is not None
            )

    async def _value_portfolio(self, feed: OneSnapshot) -> PortfolioValuation:
        """Price every open holding from the market it was acquired in."""
        async with self.sessions() as session:
            positions = await self.paper.positions_in_session(session)
            identities = await held_market_identities(session, positions)
        return await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
            identities=identities,
        ).value(positions, self.clock.now())

    async def _entry(
        self, session: AsyncSession, position: Position
    ) -> TradeCaseExecutionRow | ExitRefusal:
        """The case-bound PAPER entry of the cycle this holding belongs to.

        Read through the holding's own cycle rather than by searching the
        market's history. After a re-entry the same position row has been opened
        twice, and every executed entry for that asset would otherwise look like
        an equally good origin — so a search would report ambiguity exactly when
        the answer is in fact recorded.

        Selling something this system cannot place in a cycle would be a trade
        with no origin, and attributing it to whichever entry happens to match
        would be worse: the binding is durable, and a wrong one is a false
        record rather than a missing one.
        """
        if position.market_pair_id is None:
            return ExitRefusal.POSITION_MARKET_UNKNOWN
        if position.cycle_id is None:
            return ExitRefusal.POSITION_ORIGIN_UNKNOWN
        cycle = await session.get(TradeCycleRow, position.cycle_id)
        if cycle is None:
            return ExitRefusal.POSITION_ORIGIN_UNKNOWN
        if cycle.asset_id != position.asset_id or cycle.market_pair_id != position.market_pair_id:
            # The holding and the cycle it names describe different markets. One
            # of the two records is wrong, and a sale is not where that is
            # settled.
            return ExitRefusal.POSITION_MARKET_MISMATCH
        entry = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.cycle_id == cycle.cycle_id)
        )
        if entry is None:
            return ExitRefusal.POSITION_ORIGIN_UNKNOWN
        return entry

    async def _replay(
        self,
        session: AsyncSession,
        position: Position,
        stored_intent: IntentRow,
        request_key: str,
    ) -> ExitReading:
        """Return what this key's order already produced, unchanged."""
        intent = TradeIntent.model_validate(stored_intent.payload)
        if intent.asset_id != position.asset_id or intent.side is not Side.SELL:
            # The key belongs to a different order. Refused rather than
            # answered with somebody else's outcome.
            return _refused(position.id, ExitRefusal.EXIT_KEY_MISMATCH)
        outcome = await self.paper.replay_in_session(session, intent)
        if outcome is None:  # pragma: no cover - an intent row implies an outcome
            raise PaperExitUnavailable("EXIT_OUTCOME_MISSING")
        if isinstance(outcome, RiskDecision):
            return _refused(
                position.id,
                ExitRefusal.EXIT_RISK_REFUSED,
                outcome=outcome,
                replayed=True,
            )
        stored = await session.scalar(
            select(TradeCaseExitRow).where(TradeCaseExitRow.request_key == request_key)
        )
        if stored is None:  # pragma: no cover - written in the same transaction
            raise PaperExitUnavailable("EXIT_NOT_BOUND_TO_CASE")
        if stored.position_id != position.id:
            return _refused(position.id, ExitRefusal.EXIT_KEY_MISMATCH)
        return _recorded(stored, replayed=True)

    # ------------------------------------------------------------------ stops

    def _stops(
        self, position_id: UUID, trade_case: TradeCase, account: AccountRow
    ) -> ExitRefused | None:
        """Every stop this path must honour, checked before anything else.

        The same stops the entry honours, deliberately. An exit with special
        rights would be an emergency path, and a system that can always sell is
        a system whose stops do not stop anything.
        """
        case_id = trade_case.id
        if self.kill_switch or self.limits.kill_switch:
            return _refused(position_id, ExitRefusal.KILL_SWITCH_ENGAGED, trade_case_id=case_id)
        if self.trading_mode is not TradingMode.PAPER:
            return _refused(
                position_id,
                ExitRefusal.KILL_SWITCH_ENGAGED,
                trade_case_id=case_id,
                detail="OBSERVE",
            )
        if self.pause is None:
            return _refused(position_id, ExitRefusal.SYSTEM_STOP_UNREADABLE, trade_case_id=case_id)
        if bool(account.paused):
            return _refused(position_id, ExitRefusal.SYSTEM_PAUSED, trade_case_id=case_id)
        return None

    # ----------------------------------------------------------------- record

    def _record(
        self,
        session: AsyncSession,
        position: Position,
        trade_case: TradeCase,
        entry: TradeCaseExecutionRow,
        outcome: PaperOutcome,
        market: Any,
        now: datetime,
        request_key: str,
        trigger: "ExitTriggerRecord | None" = None,
        fresh: ExitOnchainRead | None = None,
        trace: _Trace | None = None,
    ) -> PaperExitRecorded:
        """Bind the sale to the holding, the entry and the decision behind it."""
        fill, order, trade = outcome.fill, outcome.order, outcome.trade
        state, closed = outcome.state, outcome.position
        if fill is None or order is None or trade is None or state is None or closed is None:
            raise PaperExitUnavailable("EXIT_NOT_FILLED")  # pragma: no cover - guarded above
        exit_id = uuid5(NAMESPACE_URL, f"rh-agents:trade-case-exit:{entry.request_key}:{fill.id}")
        released = state.position.cost_basis_usd - closed.cost_basis_usd
        row = TradeCaseExitRow(
            exit_id=exit_id,
            trade_case_id=trade_case.id,
            cycle_id=entry.cycle_id,
            case_execution_id=entry.case_execution_id,
            request_key=request_key,
            position_id=position.id,
            asset_id=position.asset_id,
            market_pair_id=trade_case.market.pair_id,
            intent_id=fill.intent_id,
            order_id=order.id,
            execution_id=fill.id,
            # The decision this sale was actually built on. `OrderRow.risk_id`
            # and `RiskRow.id` are this same value.
            risk_decision_id=outcome.decision.id,
            quantity=fill.quantity,
            execution_price_usd=fill.execution_price,
            notional_usd=notional_of(fill.quantity, fill.execution_price),
            fees_usd=fill.fees_usd + fill.gas_usd,
            # Straight from the ledger. Recomputing a realised result beside the
            # accounting would be a second implementation of the only number
            # this phase exists to produce.
            realized_pnl_usd=trade.realized_pnl_usd,
            cost_basis_released_usd=released,
            filled_at=fill.created_at,
            recorded_at=now,
            correlation_id=trade_case.correlation_id,
            basis={
                "request_key": request_key,
                "trade_case_id": str(trade_case.id),
                "cycle_id": str(entry.cycle_id),
                "case_execution_id": str(entry.case_execution_id),
                "entry_execution_id": str(entry.execution_id),
                "position_id": str(position.id),
                "market": trade_case.market.model_dump(mode="json"),
                # The market the decision judged, whole, so its own fingerprint
                # can be checked again without re-reading sources that moved.
                "market_snapshot": market.model_dump(mode="json"),
                "risk_limits": self.limits.model_dump(mode="json"),
                "cost_assumptions": self.costs.model_dump(mode="json"),
                "intent": order.intent.model_dump(mode="json"),
                # The valuation the decision rested on, with the holdings it
                # applied to, recomputable through the same computation.
                "portfolio": portfolio_basis(state),
                "risk_context": state.context.model_dump(mode="json"),
                "decision": outcome.decision.model_dump(mode="json"),
                "fill": fill.model_dump(mode="json"),
                "trade": trade.model_dump(mode="json"),
                "position_before": state.position.model_dump(mode="json"),
                "position_after": closed.model_dump(mode="json"),
                # Which on-chain basis the sale was judged on: its own fresh
                # read, or the entry's evidence while that was still current.
                "exit_basis": "ENTRY_EVIDENCE" if fresh is None else "FRESH_EXIT_READ",
                "exit_onchain": None if fresh is None else fresh.basis(),
                # How fresh the evidence was when the trigger was judged again
                # and when the sale was booked. Counts, codes and ages only.
                "pre_exit": None
                if trace is None or (freshness := trace.freshness(trigger)) is None
                else freshness.model_dump(mode="json"),
            },
            exit_trigger=None if trigger is None else trigger.trigger,
            exit_policy_version=None if trigger is None else trigger.policy_version,
            exit_trigger_basis=None if trigger is None else trigger.basis,
        )
        session.add(row)
        return _recorded(row, replayed=False)


# ---------------------------------------------------------------------- parts


def _intent_identity(request_key: str) -> UUID:
    """One order per key, allocated from the key rather than from live state.

    Derived rather than generated, so a retry finds the same order instead of
    minting a second one — and so a retry that finds a different holding is a
    conflicting reuse of the key rather than a new sale.
    """
    return uuid5(NAMESPACE_URL, f"rh-agents:paper-exit-intent:{request_key}")


def _exit_intent(
    position: Position,
    trade_case: TradeCase,
    market: Any,
    costs: PaperCostAssumptions,
    request_key: str,
    now: datetime,
) -> TradeIntent:
    """The SELL this system constructs, for the whole holding.

    `quantity` is the open position as read under the account lock. No caller
    supplies it, nothing rounds it, and no fraction of it is offered: a chosen
    size would be a strategy, and a residue would be a position nobody decided
    to keep.
    """
    return TradeIntent(
        id=_intent_identity(request_key),
        created_at=now,
        updated_at=now,
        source=f"PAPER_EXIT:{request_key}",
        correlation_id=trade_case.correlation_id,
        asset_id=position.asset_id,
        side=Side.SELL,
        quantity=position.quantity,
        signal_price=market.price_usd,
        # The tolerance being asked about is exactly the move the simulation
        # assumes. SENTINEL still narrows it to its own configured ceiling.
        max_slippage_bps=costs.slippage_bps,
        mode=TradingMode.PAPER,
        timing=ExecutionTiming(detected_at=now, decision_at=now),
    )


@dataclass(frozen=True)
class _Bound:
    """The held market's reading a re-judged trigger rests on."""

    snapshot_id: UUID | None

    def holds(self, snapshot: RecordedSnapshot | None) -> bool:
        return (None if snapshot is None else snapshot.id) == self.snapshot_id


def _age(now: datetime, instant: datetime | None) -> float | None:
    if instant is None:
        return None
    return round((now - instant).total_seconds(), 3)


def _code_or_none(value: str | None) -> str | None:
    """A reason code only when it really is one; anything else is dropped."""
    if value is None:
        return None
    safe = value.strip().upper()[:80]
    return safe if safe[:1].isalpha() and safe.replace("_", "").isalnum() else None


def _reading_of(snapshot: RecordedSnapshot, market: MarketIdentity) -> bool:
    """Whether a market reading describes exactly the market the case bought in.

    Kept as the last check before anything is priced or sold, independent of
    how the reading was selected: the one identity rule, `describes_market`.
    """
    return describes_market(snapshot.pair.market_identity, market)


def _same_market(position: Position, market: MarketIdentity) -> bool:
    """Whether the holding was acquired in the market its entry happened in."""
    return (
        position.market_pair_id == market.pair_id
        and position.market_chain == market.chain
        and position.market_network == market.network
        and position.market_provider == market.provider
    )


def _recorded(row: TradeCaseExitRow, *, replayed: bool) -> PaperExitRecorded:
    return PaperExitRecorded(
        exit_id=row.exit_id,
        trade_case_id=row.trade_case_id,
        case_execution_id=row.case_execution_id,
        request_key=row.request_key,
        position_id=row.position_id,
        asset_id=row.asset_id,
        market_pair_id=row.market_pair_id,
        intent_id=row.intent_id,
        order_id=row.order_id,
        execution_id=row.execution_id,
        risk_decision_id=row.risk_decision_id,
        quantity=row.quantity,
        execution_price_usd=row.execution_price_usd,
        notional_usd=row.notional_usd,
        fees_usd=row.fees_usd,
        realized_pnl_usd=row.realized_pnl_usd,
        cost_basis_released_usd=row.cost_basis_released_usd,
        filled_at=aware(row.filled_at),
        exit_trigger=row.exit_trigger,
        exit_policy_version=row.exit_policy_version,
        replayed=replayed,
    )


def _refused(
    position_id: UUID,
    reason: ExitRefusal,
    *,
    trade_case_id: UUID | None = None,
    readiness: RiskDataReadiness | None = None,
    outcome: RiskDecision | None = None,
    detail: str | None = None,
    replayed: bool = False,
) -> ExitRefused:
    return ExitRefused(
        reason=reason,
        position_id=position_id,
        trade_case_id=trade_case_id,
        outcome=None if outcome is None else outcome.outcome,
        reason_codes=() if outcome is None else outcome.reason_codes,
        data_gaps=() if readiness is None else readiness.gaps,
        blockers=() if readiness is None else readiness.blockers,
        detail=detail,
        replayed=replayed,
    )
