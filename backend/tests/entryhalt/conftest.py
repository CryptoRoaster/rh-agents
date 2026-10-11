from datetime import datetime

from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.data.tables import AccountRow, PaperControlEventRow
from src.orchestration.entryhalt.models import ActorKind
from src.orchestration.entryhalt.service import EntryHaltService, halt_entries_automatically
from tests.riskrequest.conftest import risk_db  # noqa: F401

KINDS = ("manual", "auto")


async def halt(sessions, kind: str, at: datetime) -> None:
    """Put an entry-only halt in force the way the system would: with its event."""
    if kind == "manual":
        outcome = await EntryHaltService(sessions, clock=FixedClock(at)).set_manual(
            reason="OPERATOR_REVIEW", actor="test"
        )
        assert outcome.changed, outcome
        return
    from src.core.models import RiskLimits

    async with sessions.begin() as session:
        account = await session.scalar(
            select(AccountRow).where(AccountRow.id == 1).with_for_update()
        )
        assert await halt_entries_automatically(
            session,
            account,
            reason="DAILY_LOSS_LIMIT",
            at=at,
            actor_kind=ActorKind.SENTINEL,
            limits=RiskLimits(),
        )


async def account_of(sessions):
    async with sessions() as session:
        return await session.scalar(select(AccountRow).where(AccountRow.id == 1))


async def event_count(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(PaperControlEventRow))


async def rows(sessions, table) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))
