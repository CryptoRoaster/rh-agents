"""What an explicit re-entry is, and every reason there is not one.

One completed trading cycle, one successor. The caller names the exit that
closed the previous cycle and an idempotent key; what comes back is a new
TradeCase in the workflow's own starting state, and nothing else.

Four things this is not.

**Not a purchase.** The call opens a case. Evidence, sizing, the risk request
and the fill all run again afterwards, through exactly the checks they always
run through. Nothing here buys anything.

**Not an inherited authorization.** The previous cycle's binding, risk request
and evidence stay where they are. A new cycle proves itself from nothing.

**Not a strategy.** There is no cooldown, no schedule, no condition on price and
no automatic reopening. A human or an operator asks; this answers yes or no.

**Not a second chance.** One completed exit gets one successor. A refused
successor is not retried under a new key, and a new key is not a new permission.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ReentryRefusal(StrEnum):
    """Why no successor cycle was opened. None of these is a failed opening."""

    # ------------------------------------------------- the predecessor
    # No exit with that identity exists, so there is no completed cycle to
    # succeed. A closed position row is not evidence of an exit: a row at zero
    # looks exactly like one that was never opened.
    PREDECESSOR_EXIT_NOT_FOUND = "PREDECESSOR_EXIT_NOT_FOUND"
    # The exit names a cycle, entry or case that does not agree with it. One of
    # the records is wrong, and opening a new cycle is not where that is settled.
    PREDECESSOR_MISMATCH = "PREDECESSOR_MISMATCH"
    # The cycle the exit belongs to never reached a booked entry, or its case is
    # not the terminal executed case a completed cycle leaves behind. A risk
    # rejection, an abandoned entry and a lapsed case all end a cycle without
    # one, and none of them is a completed trade.
    PREDECESSOR_NOT_EXECUTED = "PREDECESSOR_NOT_EXECUTED"
    # ------------------------------------------------- the holding
    # The exit names a holding nothing can resolve. A row this system cannot
    # read is not a holding it has shown to be closed, and an absent record is
    # not an absent objection: nothing about the previous cycle follows from it.
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    # The asset is still held, or still carries a cost basis. A cycle that still
    # owns something has not ended, and a second entry would be a top-up.
    POSITION_STILL_OPEN = "POSITION_STILL_OPEN"
    # The holding is attributed to a different cycle than the exit closed, or
    # its asset or recorded market disagrees with the entry's own case. One of
    # the records is wrong, and opening a new cycle is not where that is settled.
    POSITION_CYCLE_MISMATCH = "POSITION_CYCLE_MISMATCH"
    # ------------------------------------------------- succession
    # This cycle already has a successor. One completed exit gets one, and a
    # different key is not a second permission — least of all after the first
    # successor was refused on its merits.
    SUCCESSOR_ALREADY_EXISTS = "SUCCESSOR_ALREADY_EXISTS"
    # The key names a different predecessor than the one it was first used for.
    REENTRY_KEY_MISMATCH = "REENTRY_KEY_MISMATCH"
    # ------------------------------------------------- the stops
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


class ReentryOpened(Immutable):
    """One successor cycle, and the case the workflow opened for it."""

    kind: Literal["reentry_opened"] = "reentry_opened"
    cycle_id: UUID
    trade_case_id: UUID
    request_key: Identifier
    sequence: int = Field(ge=2)
    asset_id: Identifier
    market_pair_id: Identifier
    # What this succeeds. Both stay terminal and unrewritten.
    predecessor_exit_id: UUID
    predecessor_cycle_id: UUID
    predecessor_trade_case_id: UUID
    trade_case_status: Identifier
    opened_at: AwareDatetime
    # True when this call found the successor rather than opening one.
    replayed: bool = Field(strict=True)

    @property
    def authorizes_execution(self) -> bool:
        """Whether this permits a purchase. Never.

        The case starts where every case starts. Evidence has to be submitted,
        the completeness check has to pass, SENTINEL has to approve and the fill
        has to re-check — none of which this call does, or shortens.
        """
        return False


class ReentryRefused(Immutable):
    """No successor cycle, and the typed reason why."""

    kind: Literal["reentry_refused"] = "reentry_refused"
    reason: ReentryRefusal
    predecessor_exit_id: UUID
    predecessor_cycle_id: UUID | None = None
    detail: Code | None = None
    # True when the refusal is a stored answer returned unchanged.
    replayed: bool = Field(default=False, strict=True)


ReentryReading = ReentryOpened | ReentryRefused
