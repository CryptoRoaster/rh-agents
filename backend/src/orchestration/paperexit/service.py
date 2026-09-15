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
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import (
    ExecutionTiming,
    Position,
    RiskDecision,
    RiskLimits,
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
    TradeCaseRow,
)
from src.ledger.portfolio import portfolio_basis, portfolio_state
from src.markets.models import MarketIdentity
from src.orchestration.casefill.models import notional_of
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.paper import PaperOutcome, PaperTradingService
from src.orchestration.paperexit.models import (
    ExitReading,
    ExitRefusal,
    ExitRefused,
    PaperExitRecorded,
)
from src.orchestration.riskdata.context import RiskDataReader
from src.orchestration.riskdata.models import RiskDataReadiness
from src.orchestration.riskrequest.service import OneSnapshot, risk_market, too_old_for
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.valuation.models import PortfolioValuation, unvaluable_reason
from src.orchestration.valuation.service import PositionValuationReader
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

    @property
    def limits(self) -> RiskLimits:
        """SENTINEL's limits, from the one place that owns them."""
        return self.paper.limits

    async def execute_position_exit(self, position_id: UUID, *, request_key: str) -> ExitReading:
        """Sell the whole open holding of one position, once, or say why not."""
        try:
            return await self._attempt(position_id, request_key=request_key)
        except _Abort as abort:
            # The transaction has rolled back; the answer survives it.
            return abort.refusal

    async def _attempt(self, position_id: UUID, *, request_key: str) -> ExitReading:
        feed = OneSnapshot(self.markets, self.include_fixtures)
        intent_id = _intent_identity(request_key)
        # History needs no current prices. Checked before anything is valued, so
        # replaying a stored outcome — a completed sale or a stored refusal —
        # never depends on the market layer being reachable. The authoritative
        # replay still happens under the locks, on the intent identity.
        if await self._already_decided(intent_id):
            valuation = PortfolioValuation()
        else:
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

            readiness = await RiskDataReader(
                cases=self.cases,
                markets=feed,
                costs=self.costs,
                clock=self.clock,
                pause=self.pause,
                include_fixtures=self.include_fixtures,
            ).readiness(trade_case.id)
            snapshot = await feed.latest(trade_case.market.pair_id)
            current = active_evidence(await self.cases.evidence(trade_case.id))
            positions = await self.paper.positions_in_session(session)
            held = {item.asset_id for item in positions if item.quantity != 0}
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
            if (
                price is None
                or metadata is None
                or not isinstance(self.costs, PaperCostAssumptions)
            ):
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
            )
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

            # The whole open holding, as it stands under this lock, fixed into
            # the order now. A later different holding does not become a
            # different order: the intent identity is the key's, so a changed
            # quantity is a conflicting reuse rather than a second sale.
            intent = _exit_intent(position, trade_case, market, self.costs, request_key, now)

            def still_authorised(at: datetime) -> str | None:
                """Every governing validity, re-checked at the actual boundary.

                Synchronous and over inputs already loaded under the locks, so
                nothing between this answer and the fill touches the database.
                """
                if not readiness.is_current_at(at):
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
                session, position, trade_case, entry, outcome, market, now, request_key
            )

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
        return await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
        ).value(positions, self.clock.now())

    async def _entry(
        self, session: AsyncSession, position: Position
    ) -> TradeCaseExecutionRow | ExitRefusal:
        """The one case-bound PAPER entry that accounts for this holding.

        Selling something this system cannot say it bought would be a trade with
        no origin, and attributing a sale to whichever case happens to mention
        the asset would be worse: the exit is bound durably to the entry, and a
        wrong binding is a false record rather than a missing one.
        """
        if position.market_pair_id is None:
            return ExitRefusal.POSITION_MARKET_UNKNOWN
        found = (
            await session.scalars(
                select(TradeCaseExecutionRow)
                .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExecutionRow.trade_case_id)
                .where(
                    TradeCaseRow.chain == position.market_chain,
                    TradeCaseRow.network == position.market_network,
                )
            )
        ).all()
        cases = {item.trade_case_id: item for item in found}
        rows = (await session.scalars(select(TradeCaseRow).where(TradeCaseRow.id.in_(cases)))).all()
        matching = [
            cases[item.id]
            for item in rows
            if MarketIdentity.model_validate(item.market_payload).base_asset_id == position.asset_id
        ]
        if not matching:
            return ExitRefusal.POSITION_ORIGIN_UNKNOWN
        if len(matching) > 1:
            return ExitRefusal.POSITION_ORIGIN_AMBIGUOUS
        return matching[0]

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
            },
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
