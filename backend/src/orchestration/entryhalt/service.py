"""Setting, lifting and reading the entry-only halt — always with its audit event.

Every change of a halt happens in one transaction that holds the paper account
row `FOR UPDATE` — the lock every BUY and every SELL takes first — and writes
the halt fields and one `paper_control_events` row together. A BUY that took the
lock before the change was committed happened before the halt; one that takes
it after sees the halt and is refused. There is no third ordering.

A refused request is recorded too (`REFUSED`, with the refusal), so an operator
reading the audit sees what was asked as well as what happened.

**Lifting the automatic halt** is checked under the lock, after the account's
loss day has been rolled exactly as a trade would roll it:

- the halt's own UTC loss day must have ended, and
- the day's realised loss must be below SENTINEL's daily limit.

Neither is ever assumed, and nothing lifts the automatic halt on its own: a new
UTC day resets the day's realised loss, not the halt.

Nothing here judges a trade, values a position or calls a provider.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import RiskDecision, RiskLimits
from src.data.tables import AccountRow, PaperControlEventRow
from src.ledger.portfolio import loss_day, roll_loss_day
from src.orchestration.entryhalt.models import (
    ActorKind,
    ControlAction,
    ControlEvent,
    ControlOutcome,
    ControlRefusal,
    EntryHaltStatus,
    EntryHaltUnavailable,
    HaltControl,
    entry_halt,
    reason_code,
)
from src.risk.engine import DAILY_LOSS_LIMIT

# The longest an operator's self-chosen name may be. Recorded, never verified.
ACTOR_LENGTH = 64


def actor_reference(value: str | None) -> str | None:
    """The caller's self-chosen name, bounded. Recorded as given; identifies nobody."""
    if value is None:
        return None
    if not 0 < len(value) <= ACTOR_LENGTH or not value.isprintable():
        raise ValueError("An actor reference is 1 to 64 printable characters")
    return value


async def _locked(session: AsyncSession) -> AccountRow:
    account = await session.scalar(select(AccountRow).where(AccountRow.id == 1).with_for_update())
    if account is None:
        raise EntryHaltUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
    return account


def _record(
    session: AsyncSession,
    account: AccountRow,
    *,
    control: HaltControl,
    action: ControlAction,
    reason: str,
    actor_kind: ActorKind,
    actor_ref: str | None,
    before: bool,
    after: bool,
    at: datetime,
    limits: RiskLimits,
    refusal: ControlRefusal | None = None,
    decision_id: UUID | None = None,
    run_id: UUID | None = None,
) -> None:
    session.add(
        PaperControlEventRow(
            event_id=uuid4(),
            occurred_at=at,
            control=control.value,
            action=action.value,
            reason_code=reason,
            refusal_code=None if refusal is None else refusal.value,
            actor_kind=actor_kind.value,
            actor_ref=actor_ref,
            halted_before=before,
            halted_after=after,
            loss_day=account.loss_day,
            realized_loss_today_usd=account.realized_loss_today_usd,
            daily_loss_limit_usd=limits.daily_loss_limit_usd,
            risk_decision_id=decision_id,
            run_id=run_id,
        )
    )


async def halt_entries_automatically(
    session: AsyncSession,
    account: AccountRow,
    *,
    reason: str,
    at: datetime,
    actor_kind: ActorKind,
    limits: RiskLimits,
    decision_id: UUID | None = None,
    run_id: UUID | None = None,
) -> bool:
    """Set the automatic halt for `at`'s UTC loss day, in the caller's locked transaction.

    The caller holds the account row `FOR UPDATE`. Returns whether the halt was
    set now; one already in force is left exactly as it is — its reason, its
    instant and its loss day are the first ones, and are not overwritten.
    """
    if actor_kind is ActorKind.OPERATOR:
        raise ValueError("The automatic halt is set by the system, never by an operator")
    code = reason_code(reason)
    if account.entry_halt_auto:
        return False
    _record(
        session,
        account,
        control=HaltControl.AUTO,
        action=ControlAction.SET,
        reason=code,
        actor_kind=actor_kind,
        actor_ref=None,
        before=False,
        after=True,
        at=at,
        limits=limits,
        decision_id=decision_id,
        run_id=run_id,
    )
    account.entry_halt_auto = True
    account.entry_halt_auto_reason = code
    account.entry_halt_auto_at = at
    account.entry_halt_auto_loss_day = loss_day(at)
    account.entry_halt_auto_decision_id = decision_id
    await session.flush()
    return True


async def halt_on_daily_loss(
    session: AsyncSession,
    account: AccountRow,
    decision: RiskDecision,
    *,
    limits: RiskLimits,
    at: datetime,
) -> bool:
    """Set the automatic halt when SENTINEL refused a BUY at the daily loss limit.

    In the caller's transaction, beside the verdict it comes from, so the halt
    and the refusal that caused it commit together or not at all.
    """
    if DAILY_LOSS_LIMIT not in decision.reason_codes:
        return False
    return await halt_entries_automatically(
        session,
        account,
        reason=DAILY_LOSS_LIMIT,
        at=at,
        actor_kind=ActorKind.SENTINEL,
        limits=limits,
        decision_id=decision.id,
    )


async def halt_on_realized_loss(
    session: AsyncSession,
    account: AccountRow,
    *,
    limits: RiskLimits,
    at: datetime,
    decision_id: UUID | None = None,
) -> bool:
    """Set the automatic halt when a booked sale took the day's realised loss to the limit.

    Called after the sale's loss has been added to the account, in the booking's
    own transaction: a sale that is not booked leaves no halt behind. Measured on
    the realised loss alone — what the ledger now knows for certain; the next
    BUY is still judged on realised and unrealised loss together by SENTINEL.
    """
    if account.realized_loss_today_usd < limits.daily_loss_limit_usd:
        return False
    return await halt_entries_automatically(
        session,
        account,
        reason=DAILY_LOSS_LIMIT,
        at=at,
        actor_kind=ActorKind.LEDGER,
        limits=limits,
        decision_id=decision_id,
    )


@dataclass(frozen=True)
class EntryHaltService:
    """The operator's side of the entry-only halt: read it, set it, lift it."""

    sessions: async_sessionmaker[AsyncSession]
    # SENTINEL's limits as the paper service is built with them
    # (`runner.composition`: `PaperTradingService(..., RiskLimits(), ...)`).
    limits: RiskLimits = field(default_factory=RiskLimits)
    clock: Clock = SystemClock()

    # ------------------------------------------------------------------ reads

    async def status(self) -> EntryHaltStatus:
        """The controls as stored now. A snapshot: it authorises nothing."""
        async with self.sessions() as session:
            account = await session.scalar(select(AccountRow).where(AccountRow.id == 1))
        if account is None:
            raise EntryHaltUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
        return self._status(account)

    async def events(self, limit: int = 20) -> tuple[ControlEvent, ...]:
        """The latest audit events, newest first."""
        if not 1 <= limit <= 500:
            raise ValueError("Between 1 and 500 events may be listed")
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(PaperControlEventRow)
                    .order_by(PaperControlEventRow.sequence.desc())
                    .limit(limit)
                )
            ).all()
        return tuple(_event(row) for row in rows)

    # ----------------------------------------------------------------- writes

    async def set_manual(self, *, reason: str, actor: str | None) -> ControlOutcome:
        """Stop every new BUY until an operator lifts this halt again."""
        code, ref = reason_code(reason), actor_reference(actor)
        async with self.sessions.begin() as session:
            account = await _locked(session)
            now = self.clock.now()
            if account.entry_halt_manual:
                return self._refused(
                    session,
                    account,
                    HaltControl.MANUAL,
                    code,
                    ref,
                    now,
                    ControlRefusal.ALREADY_HALTED,
                )
            _record(
                session,
                account,
                control=HaltControl.MANUAL,
                action=ControlAction.SET,
                reason=code,
                actor_kind=ActorKind.OPERATOR,
                actor_ref=ref,
                before=False,
                after=True,
                at=now,
                limits=self.limits,
            )
            account.entry_halt_manual = True
            account.entry_halt_manual_reason = code
            account.entry_halt_manual_at = now
            await session.flush()
            return self._changed(HaltControl.MANUAL, ControlAction.SET, account)

    async def clear_manual(self, *, reason: str, actor: str | None) -> ControlOutcome:
        """Lift the manual halt. The automatic one, if any, stays."""
        code, ref = reason_code(reason), actor_reference(actor)
        async with self.sessions.begin() as session:
            account = await _locked(session)
            now = self.clock.now()
            if not account.entry_halt_manual:
                return self._refused(
                    session,
                    account,
                    HaltControl.MANUAL,
                    code,
                    ref,
                    now,
                    ControlRefusal.NOT_HALTED,
                )
            _record(
                session,
                account,
                control=HaltControl.MANUAL,
                action=ControlAction.CLEAR,
                reason=code,
                actor_kind=ActorKind.OPERATOR,
                actor_ref=ref,
                before=True,
                after=False,
                at=now,
                limits=self.limits,
            )
            account.entry_halt_manual = False
            account.entry_halt_manual_reason = None
            account.entry_halt_manual_at = None
            await session.flush()
            return self._changed(HaltControl.MANUAL, ControlAction.CLEAR, account)

    async def clear_auto(
        self, *, reason: str, actor: str | None, confirm_loss_day: date | None = None
    ) -> ControlOutcome:
        """Lift the automatic halt — only after its loss day, and only under the limit.

        Both conditions are read under the account lock, after the loss day has
        been rolled to now exactly as a trade rolls it. `confirm_loss_day`, when
        given, must name the halt's own loss day: an operator says which halt
        they mean rather than lifting whatever happens to be there.
        """
        code, ref = reason_code(reason), actor_reference(actor)
        async with self.sessions.begin() as session:
            account = await _locked(session)
            now = self.clock.now()
            roll_loss_day(account, now)
            refusal: ControlRefusal | None = None
            halted_on = account.entry_halt_auto_loss_day
            if not account.entry_halt_auto:
                refusal = ControlRefusal.NOT_HALTED
            elif confirm_loss_day is not None and confirm_loss_day != halted_on:
                refusal = ControlRefusal.LOSS_DAY_NOT_CONFIRMED
            elif halted_on is None or halted_on >= loss_day(now):
                refusal = ControlRefusal.SAME_LOSS_DAY
            elif account.realized_loss_today_usd >= self.limits.daily_loss_limit_usd:
                refusal = ControlRefusal.DAILY_LOSS_LIMIT_REACHED
            if refusal is not None:
                return self._refused(session, account, HaltControl.AUTO, code, ref, now, refusal)
            _record(
                session,
                account,
                control=HaltControl.AUTO,
                action=ControlAction.CLEAR,
                reason=code,
                actor_kind=ActorKind.OPERATOR,
                actor_ref=ref,
                before=True,
                after=False,
                at=now,
                limits=self.limits,
                decision_id=account.entry_halt_auto_decision_id,
            )
            account.entry_halt_auto = False
            account.entry_halt_auto_reason = None
            account.entry_halt_auto_at = None
            account.entry_halt_auto_loss_day = None
            account.entry_halt_auto_decision_id = None
            await session.flush()
            return self._changed(HaltControl.AUTO, ControlAction.CLEAR, account)

    # --------------------------------------------------------------- helpers

    def _refused(
        self,
        session: AsyncSession,
        account: AccountRow,
        control: HaltControl,
        code: str,
        ref: str | None,
        at: datetime,
        refusal: ControlRefusal,
    ) -> ControlOutcome:
        current = bool(
            account.entry_halt_auto if control is HaltControl.AUTO else account.entry_halt_manual
        )
        _record(
            session,
            account,
            control=control,
            action=ControlAction.REFUSED,
            reason=code,
            actor_kind=ActorKind.OPERATOR,
            actor_ref=ref,
            before=current,
            after=current,
            at=at,
            limits=self.limits,
            refusal=refusal,
        )
        return ControlOutcome(
            kind="entry_halt_refused",
            control=control,
            action=ControlAction.REFUSED,
            refusal=refusal,
            status=self._status(account),
        )

    def _changed(
        self, control: HaltControl, action: ControlAction, account: AccountRow
    ) -> ControlOutcome:
        return ControlOutcome(
            kind="entry_halt_changed", control=control, action=action, status=self._status(account)
        )

    def _status(self, account: AccountRow) -> EntryHaltStatus:
        return EntryHaltStatus(
            paused=bool(account.paused),
            entry_halt=entry_halt(account),
            entry_halt_manual=bool(account.entry_halt_manual),
            entry_halt_manual_reason=account.entry_halt_manual_reason,
            entry_halt_manual_at=account.entry_halt_manual_at,
            entry_halt_auto=bool(account.entry_halt_auto),
            entry_halt_auto_reason=account.entry_halt_auto_reason,
            entry_halt_auto_at=account.entry_halt_auto_at,
            entry_halt_auto_loss_day=account.entry_halt_auto_loss_day,
            entry_halt_auto_decision_id=account.entry_halt_auto_decision_id,
            loss_day=account.loss_day,
            realized_loss_today_usd=account.realized_loss_today_usd,
            daily_loss_limit_usd=self.limits.daily_loss_limit_usd,
        )


def _event(row: Any) -> ControlEvent:
    return ControlEvent(
        sequence=row.sequence,
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        control=HaltControl(row.control),
        action=ControlAction(row.action),
        reason_code=row.reason_code,
        refusal_code=row.refusal_code,
        actor_kind=ActorKind(row.actor_kind),
        actor_ref=row.actor_ref,
        halted_before=row.halted_before,
        halted_after=row.halted_after,
        loss_day=row.loss_day,
        realized_loss_today_usd=row.realized_loss_today_usd,
        daily_loss_limit_usd=row.daily_loss_limit_usd,
        risk_decision_id=row.risk_decision_id,
    )


__all__ = [
    "EntryHaltService",
    "actor_reference",
    "halt_entries_automatically",
    "halt_on_daily_loss",
    "halt_on_realized_loss",
]
