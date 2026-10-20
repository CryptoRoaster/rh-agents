"""Pool age at first discovery, from the provider's own creation time only.

Age is `first_seen_at - pool_created_at`: how old a pool already was when this
system first recorded it. A watch without a creation time, or with one after
its first sighting, has an UNKNOWN age. It is counted as unknown and never
enters a statistic; a statistic over no known age is UNKNOWN, never zero.

V4 pools (32-byte pool ids) are reported apart from V2/V3-style pools (20-byte
pool addresses). Read-only: one query over `discovery_watches`.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import DiscoveryWatchRow
from src.markets.models import PoolLocatorKind

FIVE_MINUTES = 300
TEN_MINUTES = 600


@dataclass(frozen=True)
class AgeStats:
    known: int
    unknown: int
    median_seconds: int | None
    p90_seconds: int | None
    share_under_5_minutes: Decimal | None
    share_under_10_minutes: Decimal | None

    @property
    def status(self) -> Literal["KNOWN", "UNKNOWN"]:
        return "KNOWN" if self.known else "UNKNOWN"


def _share(count: int, total: int) -> Decimal:
    return (Decimal(count) / Decimal(total)).quantize(Decimal("0.0001")).normalize()


def age_stats(ages: Iterable[int | None]) -> AgeStats:
    """Median (the lower middle value), P90 (nearest rank) and shares, known ages only."""
    known: list[int] = []
    unknown = 0
    for age in ages:
        if age is None or age < 0:
            unknown += 1
        else:
            known.append(age)
    if not known:
        return AgeStats(0, unknown, None, None, None, None)
    known.sort()
    n = len(known)
    return AgeStats(
        known=n,
        unknown=unknown,
        median_seconds=known[(n - 1) // 2],
        p90_seconds=known[math.ceil(0.9 * n) - 1],
        share_under_5_minutes=_share(sum(1 for age in known if age < FIVE_MINUTES), n),
        share_under_10_minutes=_share(sum(1 for age in known if age < TEN_MINUTES), n),
    )


@dataclass(frozen=True)
class PoolAgeReport:
    since: datetime
    until: datetime
    all: AgeStats
    v4: AgeStats
    v2_v3: AgeStats


def _age(first_seen: datetime, created: datetime | None) -> int | None:
    if created is None:
        return None
    return int((aware(first_seen) - aware(created)).total_seconds())


def _is_v4(payload: dict[str, object]) -> bool:
    locator = payload.get("pool_locator")
    return isinstance(locator, dict) and locator.get("kind") == PoolLocatorKind.BYTES32_POOL_ID


async def pool_age_report(
    sessions: async_sessionmaker[AsyncSession], *, since: datetime, until: datetime
) -> PoolAgeReport:
    """Watches first seen in [since, until), real provider data only."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    DiscoveryWatchRow.first_seen_at,
                    DiscoveryWatchRow.pool_created_at,
                    DiscoveryWatchRow.market_payload,
                ).where(
                    DiscoveryWatchRow.is_fixture.is_(False),
                    DiscoveryWatchRow.first_seen_at >= since,
                    DiscoveryWatchRow.first_seen_at < until,
                )
            )
        ).all()
    v4: list[int | None] = []
    other: list[int | None] = []
    for first_seen, created, payload in rows:
        (v4 if _is_v4(payload) else other).append(_age(first_seen, created))
    return PoolAgeReport(since, until, age_stats(v4 + other), age_stats(v4), age_stats(other))
