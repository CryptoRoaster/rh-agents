"""The one narrow server-side call that re-asks a market refused by the LIQ/FDV gate.

It opens a TradeCase. That is all it does.

Everything happens in a single transaction, in the order the rest of this system
already established: paper account, then trade case. The account lock is what
serialises every re-check, so the per-market count is read and raised by one
caller at a time; the predecessor is locked before the successor exists.

Three decisions this module rests on, recorded here because they are the reason
it looks the way it does.

**The rejected case is not reopened.** It stays `RISK_REJECTED` and terminal;
its evidence, binding, risk request and events are read and never rewritten.
The successor is a *new* case, opened through the central workflow in the state
every case starts in, under the predecessor's own workflow and strategy, with
`open_trade_case_in_session` — no forced status, no copied evidence, no
inherited authorization. Everything the first attempt had to prove, the second
proves again, against fresh evidence.

**Intake stays barred.** `market_barring` keeps returning `RISK_REJECTED` for
the market, because the predecessor keeps its status. While the successor is
live intake refuses the market as an active case; once it ends, the rejection
bars it again. Only this call opens a successor, and only at the end of a
history made of the gate's own refusals.

**No separate table.** The successor's open key is derived from the
predecessor's id, and the case id from that key, so the unique open key already
says "at most one successor per rejected case" — a second call finds the same
case. The per-market cap counts those keys. What the successor was eligible on
is written as its first event beside `CASE_OPENED`, so the audit trail needs no
schema of its own and no runtime migration.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import RiskLimits, RiskOutcome, TradingMode
from src.data.repository import aware
from src.data.tables import AccountRow, TradeCaseEventRow, TradeCaseRiskBindingRow, TradeCaseRow
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.entryhalt.models import entry_halt
from src.orchestration.recheck.models import (
    LIQ_FDV_RECHECK_V1,
    RECHECK_KEY_PREFIX,
    RECHECK_OPENED_EVENT,
    RecheckOpened,
    RecheckPolicy,
    RecheckReading,
    RecheckRefusal,
    RecheckRefused,
)
from src.orchestration.strategy.early import is_early
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import policy_for
from src.orchestration.workflow.service import TradeCaseService, case_from_row


class RecheckUnavailable(Exception):
    """The call could not be attempted at all. Safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def recheck_key(predecessor_case_id: UUID) -> str:
    """The open key of the one successor a rejected case may have."""
    return f"{RECHECK_KEY_PREFIX}{predecessor_case_id}"


@dataclass(frozen=True)
class LiquidityFdvRecheckService:
    """Opens the one successor case a gate-only rejection is allowed."""

    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    limits: RiskLimits
    # Intake's own operational bars, so a re-check opens nowhere intake would
    # not. Both are required: a default would be a guess about the deployment,
    # and a forgotten early-only flag would open a case nobody works.
    enabled_chains: frozenset[str]
    # `PAPER_EARLY_ONLY`: only early cases are worked, so only they are re-checked.
    early_only: bool
    trading_mode: TradingMode = TradingMode.OBSERVE
    kill_switch: bool = False
    # Supplied by a deployment that also runs the accounting subsystem. Its
    # presence says the stop is configured; its *value* is never consulted,
    # because the locked account row is the authoritative read.
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    policy: RecheckPolicy = LIQ_FDV_RECHECK_V1

    async def open_recheck(self, predecessor_case_id: UUID) -> RecheckReading:
        """Open the successor for one rejected case, once, or say why not.

        No market read happens anywhere in this call: nothing here prices,
        values or judges anything. The successor does that work later, through
        the ordinary path, with evidence gathered after this call.
        """
        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise RecheckUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
            try:
                previous = await self.cases._locked_case(session, predecessor_case_id)
            except WorkflowFailure:
                return _refused(predecessor_case_id, RecheckRefusal.PREDECESSOR_NOT_FOUND)

            # History first, before every precondition. A successor already
            # opened for this case comes back as it was recorded.
            existing = await session.scalar(
                select(TradeCaseRow).where(
                    TradeCaseRow.open_idempotency_key == recheck_key(predecessor_case_id)
                )
            )
            if existing is not None:
                return await self._opened(session, existing, previous, replayed=True)

            refusal = self._stops(predecessor_case_id, account)
            if refusal is not None:
                return refusal

            predecessor = case_from_row(previous)
            if predecessor.market.chain not in self.enabled_chains:
                return _refused(predecessor_case_id, RecheckRefusal.CHAIN_NOT_ENABLED)
            if predecessor.market.is_fixture:
                return _refused(predecessor_case_id, RecheckRefusal.FIXTURE_MARKET)
            if self.early_only and not is_early(predecessor.strategy_policy_id):
                return _refused(predecessor_case_id, RecheckRefusal.STRATEGY_NOT_RUN)
            if previous.status != TradeCaseStatus.RISK_REJECTED.value:
                return _refused(
                    predecessor_case_id,
                    RecheckRefusal.PREDECESSOR_NOT_RISK_REJECTED,
                    detail=previous.status,
                )
            binding = await _latest_binding(session, previous.id)
            if binding is None or binding.risk_input_digest != previous.risk_input_digest:
                # The case's status was derived from a decision this cannot find,
                # so the rejection cannot be placed and is not re-checked.
                return _refused(predecessor_case_id, RecheckRefusal.PREDECESSOR_DECISION_UNREADABLE)
            if binding.outcome != RiskOutcome.REJECT.value:
                return _refused(
                    predecessor_case_id,
                    RecheckRefusal.PREDECESSOR_NOT_REJECT,
                    detail=binding.outcome,
                )
            outside = self._outside(binding.reason_codes)
            if outside is not None:
                return _refused(
                    predecessor_case_id, RecheckRefusal.REASON_NOT_ELIGIBLE, detail=outside
                )

            refusal = await self._market(session, previous)
            if refusal is not None:
                return refusal

            now = self.clock.now()
            rejected_at = aware(binding.evaluated_at)
            if now < rejected_at + self.policy.min_interval:
                return _refused(predecessor_case_id, RecheckRefusal.RECHECK_TOO_EARLY)
            if now > rejected_at + self.policy.max_window:
                return _refused(predecessor_case_id, RecheckRefusal.RECHECK_WINDOW_CLOSED)

            case, created = await self.cases.open_trade_case_in_session(
                session,
                predecessor.market,
                originating_discovery_reference=predecessor.id,
                correlation_id=predecessor.correlation_id,
                idempotency_key=recheck_key(predecessor.id),
                expires_at=now + self.policy.case_lifetime,
                # The predecessor's own rules: an early attempt is re-checked as
                # an early attempt, under the same strategy caps and age bound.
                strategy_policy_id=predecessor.strategy_policy_id,
                workflow=policy_for(predecessor.workflow_version),
            )
            if not created:  # pragma: no cover - the key is checked above
                raise RecheckUnavailable("TRADE_CASE_ALREADY_OPEN")
            successor = await self.cases._locked_case(session, case.id)
            self.cases._event(
                session,
                successor,
                RECHECK_OPENED_EVENT,
                RECHECK_OPENED_EVENT,
                {
                    "policy_version": self.policy.version,
                    "predecessor_trade_case_id": str(predecessor.id),
                    "predecessor_binding_id": str(binding.binding_id),
                    "reason_codes": sorted(binding.reason_codes),
                },
            )
            await session.flush()
            return await self._opened(session, successor, previous, replayed=False)

    def _outside(self, codes: list[str]) -> str | None:
        """The first code outside the gate's own, or None when every one is in it."""
        if not codes:
            # A rejection without a reason is not one this can place.
            return "NO_REASON_CODES"
        for code in sorted(codes):
            if code not in self.policy.eligible_codes:
                return code
        return None

    async def _market(self, session: AsyncSession, previous: TradeCaseRow) -> RecheckRefused | None:
        """Whether the predecessor ends a history this contract may continue.

        Every case of the market is read, not only the newest: an execution or
        an ordinary rejection anywhere in its history keeps it barred, exactly
        as intake would.
        """
        rows = (
            await session.scalars(
                select(TradeCaseRow)
                .where(TradeCaseRow.market_key == previous.market_key)
                .order_by(TradeCaseRow.opened_at.desc(), TradeCaseRow.id.desc())
            )
        ).all()
        terminal = {item.value for item in TERMINAL_CASE_STATUSES}
        if any(row.status not in terminal for row in rows):
            return _refused(previous.id, RecheckRefusal.ACTIVE_CASE_EXISTS)
        if any(row.status == TradeCaseStatus.EXECUTED.value for row in rows):
            return _refused(previous.id, RecheckRefusal.MARKET_EXECUTED)
        if not rows or rows[0].id != previous.id:
            return _refused(previous.id, RecheckRefusal.PREDECESSOR_NOT_LATEST)
        for row in rows[1:]:
            if row.status != TradeCaseStatus.RISK_REJECTED.value:
                continue
            binding = await _latest_binding(session, row.id)
            if (
                binding is None
                or binding.outcome != RiskOutcome.REJECT.value
                or self._outside(binding.reason_codes) is not None
            ):
                return _refused(previous.id, RecheckRefusal.MARKET_REJECTED_OTHERWISE)
        opened = await session.scalar(
            select(func.count())
            .select_from(TradeCaseRow)
            .where(
                TradeCaseRow.market_key == previous.market_key,
                TradeCaseRow.open_idempotency_key.startswith(RECHECK_KEY_PREFIX, autoescape=True),
            )
        )
        if (opened or 0) >= self.policy.max_per_market:
            return _refused(previous.id, RecheckRefusal.RECHECK_LIMIT_REACHED)
        return None

    async def _opened(
        self,
        session: AsyncSession,
        successor: TradeCaseRow,
        previous: TradeCaseRow,
        *,
        replayed: bool,
    ) -> RecheckOpened:
        """The successor as its own opening event recorded it."""
        event = await session.scalar(
            select(TradeCaseEventRow)
            .where(
                TradeCaseEventRow.trade_case_id == successor.id,
                TradeCaseEventRow.event_type == RECHECK_OPENED_EVENT,
            )
            .order_by(TradeCaseEventRow.sequence)
            .limit(1)
        )
        if event is None or event.payload.get("predecessor_trade_case_id") != str(previous.id):
            # A case under this key that does not say what it succeeds is a
            # record at odds with itself, and nothing here repairs it.
            raise RecheckUnavailable("RECHECK_RECORD_INCONSISTENT")
        case = case_from_row(successor)
        return RecheckOpened(
            trade_case_id=case.id,
            predecessor_trade_case_id=previous.id,
            predecessor_binding_id=UUID(str(event.payload["predecessor_binding_id"])),
            reason_codes=tuple(event.payload["reason_codes"]),
            policy_version=str(event.payload["policy_version"]),
            market_pair_id=case.market.pair_id,
            trade_case_status=case.status.value,
            opened_at=case.opened_at,
            replayed=replayed,
        )

    def _stops(self, predecessor_case_id: UUID, account: AccountRow) -> RecheckRefused | None:
        """Every stop an entry honours, checked before anything is opened.

        A successor is a case that could become a BUY, so it opens only where a
        BUY could.
        """
        if self.kill_switch or self.limits.kill_switch:
            return _refused(predecessor_case_id, RecheckRefusal.KILL_SWITCH_ENGAGED)
        if self.trading_mode is not TradingMode.PAPER:
            return _refused(
                predecessor_case_id,
                RecheckRefusal.MODE_NOT_PAPER,
                detail=self.trading_mode.value,
            )
        if self.pause is None:
            return _refused(predecessor_case_id, RecheckRefusal.SYSTEM_STOP_UNREADABLE)
        if bool(account.paused):
            return _refused(predecessor_case_id, RecheckRefusal.SYSTEM_PAUSED)
        halted = entry_halt(account)
        if halted is not None:
            return _refused(predecessor_case_id, RecheckRefusal.ENTRY_HALTED, detail=halted)
        return None


async def _latest_binding(
    session: AsyncSession, trade_case_id: UUID
) -> TradeCaseRiskBindingRow | None:
    """The decision a case's status was last derived from: its highest revision."""
    rows = await session.scalars(
        select(TradeCaseRiskBindingRow)
        .where(TradeCaseRiskBindingRow.trade_case_id == trade_case_id)
        .order_by(TradeCaseRiskBindingRow.case_revision.desc())
        .limit(1)
    )
    return rows.first()


def _refused(
    predecessor_case_id: UUID, reason: RecheckRefusal, *, detail: str | None = None
) -> RecheckRefused:
    return RecheckRefused(
        reason=reason, predecessor_trade_case_id=predecessor_case_id, detail=detail
    )
