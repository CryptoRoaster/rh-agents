"""Where a durable pause came from, read from SENTINEL's own record.

`paper_accounts.paused` carries no reason. Two things have ever set it, both in
the same transaction as the SENTINEL verdict that caused it, and both write that
verdict down — `risk_decisions` (the paper executor) or
`trade_case_risk_bindings` (the risk request):

- a `PAUSE_SYSTEM` verdict naming `KILL_SWITCH` — the kill switch, or an account
  that was already paused (SENTINEL folds the pause into its kill switch);
- before #85, a `PAUSE_SYSTEM` verdict naming `DAILY_LOSS_LIMIT` alone — the
  daily loss limit, which since then halts entries instead of pausing.

So the pause's origin is the **first** `PAUSE_SYSTEM` verdict after the last
time it was lifted, and the record is only consistent with one uninterrupted
pause if every verdict after that one is a `PAUSE_SYSTEM` naming `KILL_SWITCH`:
a verdict reached while paused always is. Anything else — no verdict at all, a
later verdict that was not a pause — means the record does not explain the
pause, and nothing may be inferred from it.

What this cannot see is a pause set or lifted by hand before 0024: it then
reads as `UNKNOWN` (no verdict) or `INCONSISTENT`, never as a daily-loss pause.
A hand-written pause *after* a genuine daily-loss pause, with nothing lifting it
in between, is indistinguishable from the original and is the one residual case
(see `docs/paper-minimum-release-gate.md`).
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.tables import PaperControlEventRow, RiskRow, TradeCaseRiskBindingRow
from src.orchestration.entryhalt.models import ControlAction, Frozen, HaltControl
from src.risk.engine import DAILY_LOSS_LIMIT

PAUSE_SYSTEM = "PAUSE_SYSTEM"
KILL_SWITCH = "KILL_SWITCH"
# How many verdicts after the origin are read to check the record. More than
# this many after a pause is itself a reason not to trust a reading of it.
MAX_FOLLOWING = 500
# Read from just before the origin's instant, so a verdict sharing it is seen.
_TICK = timedelta(microseconds=1)


class PauseOriginKind(StrEnum):
    NOT_PAUSED = "NOT_PAUSED"
    # The only origin this system lifts: the pre-#85 daily loss pause.
    LEGACY_DAILY_LOSS_LIMIT = "LEGACY_DAILY_LOSS_LIMIT"
    KILL_SWITCH = "KILL_SWITCH"
    UNKNOWN = "UNKNOWN"
    INCONSISTENT = "INCONSISTENT"


class PauseOrigin(Frozen):
    kind: PauseOriginKind
    # The verdict that set the pause, when one does.
    decision_id: UUID | None = None
    decided_at: datetime | None = None
    reason_codes: tuple[str, ...] = ()
    # When the pause was last lifted through the audit, if ever.
    since: datetime | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _last_clear(session: AsyncSession) -> datetime | None:
    value = await session.scalar(
        select(PaperControlEventRow.occurred_at)
        .where(
            PaperControlEventRow.control == HaltControl.PAUSE.value,
            PaperControlEventRow.action == ControlAction.CLEAR.value,
        )
        .order_by(PaperControlEventRow.sequence.desc())
        .limit(1)
    )
    return None if value is None else _aware(value)


Verdict = tuple[datetime, UUID, str, tuple[str, ...]]


async def _verdicts(
    session: AsyncSession, since: datetime | None, *, pauses_only: bool, limit: int
) -> list[Verdict]:
    """Recorded verdicts after `since`, oldest first: (instant, decision id, outcome, reasons).

    Both records are read — the paper executor's and the risk request's — each
    up to `limit`, and merged.
    """
    found: list[Verdict] = []
    risk = select(RiskRow.created_at, RiskRow.id, RiskRow.payload)
    if since is not None:
        risk = risk.where(RiskRow.created_at > since)
    if pauses_only:
        risk = risk.where(RiskRow.payload["outcome"].as_string() == PAUSE_SYSTEM)
    for created_at, decision_id, payload in (
        await session.execute(risk.order_by(RiskRow.created_at, RiskRow.id).limit(limit))
    ).all():
        found.append(
            (
                _aware(created_at),
                decision_id,
                str(_get(payload, "outcome")),
                tuple(str(item) for item in _get(payload, "reason_codes") or ()),
            )
        )
    bound = select(
        TradeCaseRiskBindingRow.evaluated_at,
        TradeCaseRiskBindingRow.risk_decision_id,
        TradeCaseRiskBindingRow.outcome,
        TradeCaseRiskBindingRow.reason_codes,
    )
    if since is not None:
        bound = bound.where(TradeCaseRiskBindingRow.evaluated_at > since)
    if pauses_only:
        bound = bound.where(TradeCaseRiskBindingRow.outcome == PAUSE_SYSTEM)
    for evaluated_at, decision_id, outcome, reasons in (
        await session.execute(
            bound.order_by(
                TradeCaseRiskBindingRow.evaluated_at, TradeCaseRiskBindingRow.risk_decision_id
            ).limit(limit)
        )
    ).all():
        found.append(
            (_aware(evaluated_at), decision_id, str(outcome), tuple(str(r) for r in reasons or ()))
        )
    found.sort(key=lambda item: (item[0], str(item[1])))
    return found


def _get(payload: Any, key: str) -> Any:
    return payload.get(key) if isinstance(payload, dict) else None


async def pause_origin(session: AsyncSession, *, paused: bool) -> PauseOrigin:
    """The verdict that set the current pause, judged against everything after it."""
    if not paused:
        return PauseOrigin(kind=PauseOriginKind.NOT_PAUSED)
    since = await _last_clear(session)
    pauses = await _verdicts(session, since, pauses_only=True, limit=1)
    if not pauses:
        return PauseOrigin(kind=PauseOriginKind.UNKNOWN, since=since)
    decided_at, decision_id, _, reasons = pauses[0]

    def origin(kind: PauseOriginKind) -> PauseOrigin:
        return PauseOrigin(
            kind=kind,
            decision_id=decision_id,
            decided_at=decided_at,
            reason_codes=reasons,
            since=since,
        )

    # Everything recorded from the origin on. A verdict reached while paused is
    # always a PAUSE_SYSTEM naming KILL_SWITCH; anything else means the pause
    # was lifted and set again without the audit, or never was this one.
    following = [
        item
        for item in await _verdicts(
            session, decided_at - _TICK, pauses_only=False, limit=MAX_FOLLOWING + 2
        )
        if item[1] != decision_id
    ]
    if len(following) > MAX_FOLLOWING or any(
        outcome != PAUSE_SYSTEM or KILL_SWITCH not in codes for _, _, outcome, codes in following
    ):
        return origin(PauseOriginKind.INCONSISTENT)
    if KILL_SWITCH in reasons:
        return origin(PauseOriginKind.KILL_SWITCH)
    if DAILY_LOSS_LIMIT in reasons:
        return origin(PauseOriginKind.LEGACY_DAILY_LOSS_LIMIT)
    return origin(PauseOriginKind.UNKNOWN)


__all__ = ["PauseOrigin", "PauseOriginKind", "pause_origin"]
