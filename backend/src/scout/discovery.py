"""Early discovery page coverage: the overlap reference and the page-2 budget.

Issue #77. At a five-minute cadence one `new_pools` page (20 pools) covers a
poll on average, but about one poll in five shares no pool with the previous
one: the page boundary was not observed, and pools listed in between may have
been missed. Such a poll is a *possible gap*, and only then may a second page
be read — once, bounded.

**The reference is the database.** A pool counts as already seen when this
system recorded an observation of it before the run started. No cursor or
pool list is kept beside it, so a restart neither loses the reference nor
re-creates a watch (a watch is unique per market stream anyway), and a scout
that was down for an hour correctly finds no overlap.

**The page-2 budget is durable.** Its daily count is the sum of the page-2
reads recorded on the UTC day's scout runs. A read that cannot be counted is
not made: the database error ends the run like any other, before a request.

Nothing here calls a provider.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.tables import MarketObservationRow, ScoutRunRow
from src.scout.budget import utc_day

PROVIDER = "geckoterminal"

# Why a possible gap was left open. Codes only.
PAGE2_DISABLED = "DISCOVERY_PAGE2_DISABLED"
PAGE_TRUNCATED = "DISCOVERY_PAGE_TRUNCATED"
PAGE2_DAILY_BUDGET = "DISCOVERY_PAGE2_DAILY_BUDGET"
PAGE2_RUN_BUDGET = "DISCOVERY_PAGE2_RUN_BUDGET"
PAGE2_NO_OVERLAP = "DISCOVERY_PAGE2_NO_OVERLAP"
PAGE2_FAILED_PREFIX = "DISCOVERY_PAGE2_"


@dataclass(frozen=True)
class DiscoveryReference:
    """What this system had recorded before a run, as discovery's overlap reference."""

    sessions: async_sessionmaker[AsyncSession]

    async def has_reference(self, chain: str, before: datetime) -> bool:
        """Whether any real observation on this chain predates `before`.

        Without one — a first run ever — nothing can be compared, so no poll
        can be proven saturated and no second page is asked for.
        """
        async with self.sessions() as session:
            found = await session.scalar(
                select(MarketObservationRow.id)
                .where(
                    MarketObservationRow.provider == PROVIDER,
                    MarketObservationRow.chain == chain,
                    MarketObservationRow.is_fixture.is_(False),
                    MarketObservationRow.observed_at < before,
                )
                .limit(1)
            )
        return found is not None

    async def seen_before(self, chain: str, pair_ids: Iterable[str], before: datetime) -> bool:
        """Whether any of these pools was observed before `before`."""
        wanted = sorted(set(pair_ids))
        if not wanted:
            return False
        async with self.sessions() as session:
            found = await session.scalar(
                select(MarketObservationRow.id)
                .where(
                    MarketObservationRow.provider == PROVIDER,
                    MarketObservationRow.chain == chain,
                    MarketObservationRow.is_fixture.is_(False),
                    MarketObservationRow.pair_id.in_(wanted),
                    MarketObservationRow.observed_at < before,
                )
                .limit(1)
            )
        return found is not None

    async def page2_used(self, now: datetime) -> int:
        """Page-2 reads recorded on the scout runs of `now`'s UTC day."""
        start = datetime.combine(utc_day(now), time(), tzinfo=UTC)
        async with self.sessions() as session:
            used = await session.scalar(
                select(func.coalesce(func.sum(ScoutRunRow.discovery_page2_requests), 0)).where(
                    ScoutRunRow.started_at >= start,
                    ScoutRunRow.started_at < start + timedelta(days=1),
                )
            )
        return int(used or 0)


__all__ = [
    "PAGE2_DAILY_BUDGET",
    "PAGE2_DISABLED",
    "PAGE2_FAILED_PREFIX",
    "PAGE2_NO_OVERLAP",
    "PAGE2_RUN_BUDGET",
    "PAGE_TRUNCATED",
    "DiscoveryReference",
]
