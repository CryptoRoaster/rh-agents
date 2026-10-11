"""The entry-only halt's vocabulary: which halt, what happened to it, and why not.

Two halts live on the paper account and are set and cleared apart:

- **Automatic** (`ENTRY_HALT_AUTO`) — set by the system for a UTC loss day.
  Nothing lifts it but an operator, and only once that loss day is over and the
  day's realised loss is back under SENTINEL's daily limit. It never lapses on
  its own.
- **Manual** (`ENTRY_HALT_MANUAL`) — set and lifted by an operator.

Either stops every new BUY on every path. Neither stops a SELL: the durable
pause (`paused`) and the kill switch are the full stops, and they are untouched.
"""

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# The refusal every BUY path answers with while a halt is in force; the detail
# says which (`MANUAL`, `AUTO`, `MANUAL_AND_AUTO`).
ENTRY_HALTED = "ENTRY_HALTED"
REASON_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class HaltControl(StrEnum):
    AUTO = "ENTRY_HALT_AUTO"
    MANUAL = "ENTRY_HALT_MANUAL"


class ControlAction(StrEnum):
    SET = "SET"
    CLEAR = "CLEAR"
    REFUSED = "REFUSED"


class ActorKind(StrEnum):
    OPERATOR = "OPERATOR"
    SENTINEL = "SENTINEL"
    LEDGER = "LEDGER"


class ControlRefusal(StrEnum):
    ALREADY_HALTED = "ALREADY_HALTED"
    NOT_HALTED = "NOT_HALTED"
    # The automatic halt's loss day has not ended yet (UTC).
    SAME_LOSS_DAY = "SAME_LOSS_DAY"
    # The day's realised loss is still at or above SENTINEL's daily limit.
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    # The operator named a loss day that is not the halt's own.
    LOSS_DAY_NOT_CONFIRMED = "LOSS_DAY_NOT_CONFIRMED"


class EntryHaltUnavailable(Exception):
    """The halt could not be read or written. Never the same as "no halt"."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def halt_detail(manual: object, auto: object) -> str | None:
    """Which halt is in force, as a detail code, or `None` when neither is.

    Anything that is not exactly `False` counts as in force: a value this code
    cannot read as "not halted" is not permission.
    """
    manual_on = manual is not False
    auto_on = auto is not False
    if manual_on and auto_on:
        return "MANUAL_AND_AUTO"
    if manual_on:
        return "MANUAL"
    if auto_on:
        return "AUTO"
    return None


def entry_halt(account: object) -> str | None:
    """The halt in force on a loaded account row, read as `halt_detail` reads it."""
    return halt_detail(
        getattr(account, "entry_halt_manual", None), getattr(account, "entry_halt_auto", None)
    )


def reason_code(value: str) -> str:
    """A reason as the audit stores it, or `ValueError`: never free text."""
    if not REASON_PATTERN.fullmatch(value):
        raise ValueError("A reason must be a code: A-Z, 0-9 and _, starting with a letter")
    return value


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EntryHaltStatus(Frozen):
    """Every control that decides whether a BUY or a SELL may happen, as stored."""

    paused: bool
    entry_halt: str | None
    entry_halt_manual: bool
    entry_halt_manual_reason: str | None = None
    entry_halt_manual_at: datetime | None = None
    entry_halt_auto: bool
    entry_halt_auto_reason: str | None = None
    entry_halt_auto_at: datetime | None = None
    entry_halt_auto_loss_day: date | None = None
    entry_halt_auto_decision_id: UUID | None = None
    loss_day: date
    realized_loss_today_usd: Decimal
    daily_loss_limit_usd: Decimal


class ControlEvent(Frozen):
    sequence: int
    event_id: UUID
    occurred_at: datetime
    control: HaltControl
    action: ControlAction
    reason_code: str
    refusal_code: str | None = None
    actor_kind: ActorKind
    actor_ref: str | None = None
    halted_before: bool
    halted_after: bool
    loss_day: date | None = None
    realized_loss_today_usd: Decimal | None = None
    daily_loss_limit_usd: Decimal | None = None
    risk_decision_id: UUID | None = None


class ControlOutcome(Frozen):
    """What one operator request did: changed a halt, or was refused and recorded."""

    kind: Literal["entry_halt_changed", "entry_halt_refused"]
    control: HaltControl
    action: ControlAction
    refusal: ControlRefusal | None = None
    status: EntryHaltStatus

    @property
    def changed(self) -> bool:
        return self.kind == "entry_halt_changed"


__all__ = [
    "ENTRY_HALTED",
    "ActorKind",
    "ControlAction",
    "ControlEvent",
    "ControlOutcome",
    "ControlRefusal",
    "EntryHaltStatus",
    "EntryHaltUnavailable",
    "HaltControl",
    "entry_halt",
    "halt_detail",
    "reason_code",
]
