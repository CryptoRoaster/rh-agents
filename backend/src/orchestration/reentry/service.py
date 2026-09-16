"""The one narrow server-side call that opens a cycle after a completed exit.

It opens a TradeCase. That is all it does.

Everything happens in a single transaction, in the order the rest of this system
already established: paper account, then trade case. Where two cases are
involved the predecessor is locked before the successor — the successor does not
exist yet when the predecessor is checked, so the order is not a choice, and it
is the order every caller here uses.

Three decisions this module rests on, recorded here because they are the reason
it looks the way it does.

**The previous cycle is not reopened.** Its case stays `EXECUTED` and terminal;
its evidence, binding, risk request, intent and bookings are read and never
rewritten. The successor is a *new* case, opened through the central workflow in
the state every case starts in, with `open_trade_case_in_session` — no forced
status, no copied evidence, no inherited authorization. Everything a first entry
had to prove, a second entry proves again.

**Intake stays barred.** A market that has executed is spoken for, and observing
it again is not new information. Only this call may open the successor, and only
for a cycle that actually completed. While that successor is live intake refuses
it as an active case; once it ends, the executed history bars it again.

**One completed exit gets one successor.** `trade_cycles.predecessor_exit_id` is
unique, so a refused or abandoned successor cannot be retried under a new key.
Whether a *further* attempt should ever be possible is a question this phase
does not answer, and answering it accidentally — by letting a second key through
— would be a strategy nobody wrote down.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import RiskLimits, TradingMode
from src.data.repository import aware
from src.data.tables import (
    AccountRow,
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
    TradeCycleRow,
)
from src.markets.models import MarketIdentity
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.cycles import cycle_of
from src.orchestration.paper import PaperTradingService
from src.orchestration.reentry.models import (
    ReentryOpened,
    ReentryReading,
    ReentryRefusal,
    ReentryRefused,
)
from src.orchestration.workflow.models import TradeCaseStatus, WorkflowFailure
from src.orchestration.workflow.service import TradeCaseService, case_from_row


class ReentryUnavailable(Exception):
    """The call could not be attempted at all. Safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class PaperReentryService:
    """Opens the one successor cycle a completed exit is allowed."""

    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    paper: PaperTradingService
    trading_mode: TradingMode = TradingMode.OBSERVE
    kill_switch: bool = False
    # Supplied by a deployment that also runs the accounting subsystem. Its
    # presence says the stop is configured; its *value* is never consulted,
    # because the locked account row is the authoritative read.
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    # How long the successor case has to reach a decision. The workflow's own
    # lifetime rule, not a trading one: a case that proves nothing expires, and
    # an expired case leaves the market barred by its executed history.
    case_lifetime: timedelta = timedelta(hours=1)

    @property
    def limits(self) -> RiskLimits:
        """SENTINEL's limits, from the one place that owns them."""
        return self.paper.limits

    async def open_reentry(self, exit_id: UUID, *, request_key: str) -> ReentryReading:
        """Open the successor cycle for one completed exit, once, or say why not.

        No market read happens anywhere in this call: nothing here prices,
        values or judges anything, so there is no provider to wait for under a
        lock. The successor case does that work later, through the ordinary
        path.
        """
        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise ReentryUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")

            closed = await session.get(TradeCaseExitRow, exit_id)
            if closed is None:
                # A position row at zero is not evidence of an exit — it looks
                # exactly like one that was never opened. Only a recorded exit is.
                return _refused(exit_id, ReentryRefusal.PREDECESSOR_EXIT_NOT_FOUND)

            # History first, before every precondition. A successor already
            # opened under this key comes back as it was recorded, and a
            # predecessor that already has one is refused rather than given a
            # second.
            existing = await session.scalar(
                select(TradeCycleRow).where(TradeCycleRow.request_key == request_key)
            )
            if existing is not None:
                if existing.predecessor_exit_id != exit_id:
                    # Two callers must not end up believing they own one cycle.
                    return _refused(exit_id, ReentryRefusal.REENTRY_KEY_MISMATCH)
                return await self._opened(session, existing, closed, replayed=True)
            successor = await session.scalar(
                select(TradeCycleRow).where(TradeCycleRow.predecessor_exit_id == exit_id)
            )
            if successor is not None:
                return _refused(
                    exit_id,
                    ReentryRefusal.SUCCESSOR_ALREADY_EXISTS,
                    cycle_id=closed.cycle_id,
                )

            refusal = self._stops(exit_id, closed, account)
            if refusal is not None:
                return refusal

            cycle = await session.get(TradeCycleRow, closed.cycle_id)
            if cycle is None or cycle.trade_case_id != closed.trade_case_id:
                return _refused(exit_id, ReentryRefusal.PREDECESSOR_MISMATCH)
            entry = await session.scalar(
                select(TradeCaseExecutionRow).where(
                    TradeCaseExecutionRow.cycle_id == cycle.cycle_id
                )
            )
            if (
                entry is None
                or entry.case_execution_id != closed.case_execution_id
                or entry.trade_case_id != cycle.trade_case_id
            ):
                # The exit must close the entry of its own cycle, and that entry
                # must belong to the cycle's own case. A chain checked in two of
                # three places is a chain with a link missing: the entry is found
                # *by* cycle, so its own case reference is the one thing that
                # nothing else here would notice. Anything that disagrees is a
                # record at odds with itself.
                return _refused(
                    exit_id, ReentryRefusal.PREDECESSOR_MISMATCH, cycle_id=cycle.cycle_id
                )

            try:
                previous_row = await self.cases._locked_case(session, cycle.trade_case_id)
            except WorkflowFailure:
                raise ReentryUnavailable("TRADE_CASE_NOT_FOUND") from None
            previous = case_from_row(previous_row)
            if previous.status is not TradeCaseStatus.EXECUTED:
                # A completed cycle is one that bought and sold. A risk
                # rejection, an abandoned entry and a lapsed case each end a
                # cycle without that, and none of them is a completed trade.
                return _refused(
                    exit_id,
                    ReentryRefusal.PREDECESSOR_NOT_EXECUTED,
                    cycle_id=cycle.cycle_id,
                    detail=previous.status.value,
                )

            # The holding, and every way it has to agree with what closed it.
            # None of this is optional: a check that only runs when the record
            # happens to be readable is not a precondition, and the one case it
            # skips is the one where nothing at all is established.
            holding = await session.scalar(
                select(PositionRow).where(PositionRow.id == closed.position_id)
            )
            if holding is None:
                return _refused(exit_id, ReentryRefusal.POSITION_NOT_FOUND, cycle_id=cycle.cycle_id)
            if holding.quantity != 0 or holding.cost_basis_usd != 0:
                # Still owned, so the cycle has not ended. A second entry here
                # would be a top-up, which has no contract.
                return _refused(
                    exit_id, ReentryRefusal.POSITION_STILL_OPEN, cycle_id=cycle.cycle_id
                )
            if not _describes_one_trade(holding, cycle, previous.market, closed):
                # The holding, the cycle it names, the exit that closed it and
                # the case that opened it must all describe one trade in one
                # market. Where they do not, one of the records is wrong, and
                # nothing here guesses which or repairs it.
                return _refused(
                    exit_id, ReentryRefusal.POSITION_CYCLE_MISMATCH, cycle_id=cycle.cycle_id
                )

            now = self.clock.now()
            case, created = await self.cases.open_trade_case_in_session(
                session,
                previous.market,
                originating_discovery_reference=closed.exit_id,
                correlation_id=previous.correlation_id,
                # A namespace of its own, so a successor can never collide with
                # an intake generation, an entry request or an exit order.
                idempotency_key=f"rh-agents:paper-reentry:{request_key}",
                expires_at=now + self.case_lifetime,
            )
            if not created:  # pragma: no cover - the key is checked above
                raise ReentryUnavailable("TRADE_CASE_ALREADY_OPEN")
            opened = TradeCycleRow(
                cycle_id=cycle_of(case.id),
                trade_case_id=case.id,
                asset_id=cycle.asset_id,
                market_pair_id=cycle.market_pair_id,
                sequence=cycle.sequence + 1,
                predecessor_exit_id=closed.exit_id,
                request_key=request_key,
                opened_at=now,
                correlation_id=previous.correlation_id,
            )
            session.add(opened)
            await session.flush()
            return await self._opened(session, opened, closed, replayed=False)

    async def _opened(
        self,
        session: AsyncSession,
        cycle: TradeCycleRow,
        closed: TradeCaseExitRow,
        *,
        replayed: bool,
    ) -> ReentryOpened:
        row = await self.cases._locked_case(session, cycle.trade_case_id)
        case = case_from_row(row)
        return ReentryOpened(
            cycle_id=cycle.cycle_id,
            trade_case_id=cycle.trade_case_id,
            request_key=cycle.request_key or "",
            sequence=cycle.sequence,
            asset_id=cycle.asset_id,
            market_pair_id=cycle.market_pair_id,
            predecessor_exit_id=closed.exit_id,
            predecessor_cycle_id=closed.cycle_id,
            predecessor_trade_case_id=closed.trade_case_id,
            trade_case_status=case.status.value,
            opened_at=aware(cycle.opened_at),
            replayed=replayed,
        )

    def _stops(
        self, exit_id: UUID, closed: TradeCaseExitRow, account: AccountRow
    ) -> ReentryRefused | None:
        """Every stop this path must honour, checked before anything is opened.

        The same stops an entry and an exit honour. A system that can always
        start a new trade is a system whose stops do not stop anything.
        """
        cycle_id = closed.cycle_id
        if self.kill_switch or self.limits.kill_switch:
            return _refused(exit_id, ReentryRefusal.KILL_SWITCH_ENGAGED, cycle_id=cycle_id)
        if self.trading_mode is not TradingMode.PAPER:
            return _refused(
                exit_id, ReentryRefusal.KILL_SWITCH_ENGAGED, cycle_id=cycle_id, detail="OBSERVE"
            )
        if self.pause is None:
            return _refused(exit_id, ReentryRefusal.SYSTEM_STOP_UNREADABLE, cycle_id=cycle_id)
        if bool(account.paused):
            return _refused(exit_id, ReentryRefusal.SYSTEM_PAUSED, cycle_id=cycle_id)
        return None


def _describes_one_trade(
    holding: PositionRow,
    cycle: TradeCycleRow,
    market: MarketIdentity,
    closed: TradeCaseExitRow,
) -> bool:
    """Whether the holding, its cycle, the exit and the entry's market agree.

    The whole recorded identity, not just the pair: an address means nothing
    across chains, and two providers observing one pool are two sources. A
    partial match is exactly the kind of agreement that looks like one.
    """
    return (
        holding.cycle_id == cycle.cycle_id
        and holding.asset_id == cycle.asset_id
        and holding.asset_id == closed.asset_id
        and holding.asset_id == market.base_asset_id
        and holding.market_pair_id == cycle.market_pair_id
        and holding.market_pair_id == closed.market_pair_id
        and holding.market_pair_id == market.pair_id
        and holding.market_chain == market.chain
        and holding.market_network == market.network
        and holding.market_provider == market.provider
    )


def _refused(
    exit_id: UUID,
    reason: ReentryRefusal,
    *,
    cycle_id: UUID | None = None,
    detail: str | None = None,
    replayed: bool = False,
) -> ReentryRefused:
    return ReentryRefused(
        reason=reason,
        predecessor_exit_id=exit_id,
        predecessor_cycle_id=cycle_id,
        detail=detail,
        replayed=replayed,
    )
