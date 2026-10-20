"""Pool age at first discovery, from the provider's own creation time only.

An unknown creation time stays UNKNOWN: it is counted, never estimated, and a
statistic over no known value is UNKNOWN rather than zero.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.scout.pool_age import age_stats, pool_age_report
from tests.scout.conftest import scout
from tests.scout.test_discovery_optimization import PagedNetworks, cached, created
from tests.scout.test_discovery_pages import v3, v4

T0 = datetime(2026, 10, 20, 6, tzinfo=UTC)


def test_median_p90_and_shares_over_known_ages_only():
    stats = age_stats([60, 120, 180, 240, 290, 310, 420, 590, 610, 1200, None, None])
    assert stats.known == 10
    assert stats.unknown == 2
    assert stats.median_seconds == 290
    assert stats.p90_seconds == 610
    assert stats.share_under_5_minutes == Decimal("0.5")
    assert stats.share_under_10_minutes == Decimal("0.8")


def test_no_known_age_is_unknown_not_zero():
    stats = age_stats([None, None])
    assert stats.known == 0
    assert stats.unknown == 2
    assert stats.median_seconds is None
    assert stats.p90_seconds is None
    assert stats.share_under_5_minutes is None
    assert stats.share_under_10_minutes is None
    assert stats.status == "UNKNOWN"


def test_a_negative_age_is_inconsistent_and_counted_as_unknown():
    stats = age_stats([-5, 30])
    assert (stats.known, stats.unknown) == (1, 1)
    assert stats.median_seconds == 30


async def test_the_report_separates_v4_from_v2_and_v3(db):
    _, sessions = db
    pools = [
        created(v4(1), "2026-10-20T05:58:00Z"),  # 2 min
        created(v4(2), "2026-10-20T05:52:00Z"),  # 8 min
        v4(3),
        created(v3(4), "2026-10-20T05:45:00Z"),  # 15 min
        created(v3(5), "2026-10-20T05:57:00Z"),  # 3 min
    ]
    await scout(sessions, T0, provider=PagedNetworks(discovery_pages={1: pools}), settings=cached())
    report = await pool_age_report(sessions, since=T0 - timedelta(hours=1), until=T0 + timedelta(1))
    assert (report.all.known, report.all.unknown) == (4, 1)
    assert (report.v4.known, report.v4.unknown) == (2, 1)
    assert report.v4.median_seconds == 120
    assert report.v4.share_under_5_minutes == Decimal("0.5")
    assert (report.v2_v3.known, report.v2_v3.unknown) == (2, 0)
    assert report.v2_v3.median_seconds == 180
    assert report.v2_v3.share_under_10_minutes == Decimal("0.5")


async def test_an_empty_window_reports_unknown(db):
    _, sessions = db
    report = await pool_age_report(sessions, since=T0, until=T0 + timedelta(hours=1))
    assert report.all.status == "UNKNOWN"
    assert report.all.known == report.all.unknown == 0
