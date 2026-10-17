"""Early discovery at a five-minute cadence: the whole page, and page 2 on saturation.

Issue #77, shadow run B. One `new_pools` page holds 20 pools; at a five-minute
cadence one page is enough on average, but about one poll in five shows no
overlap with what the previous poll saw. Such a poll is recorded as a possible
gap, and — only when switched on, within the run's request budget and the day's
page-2 budget — a second page is read once.

The overlap reference is the database itself: a pool counts as already seen
when this system recorded an observation of it before the run started. A
restart therefore neither loses the reference nor re-creates a watch.

Every test drives the real scout cycle against a scripted provider. Nothing
reaches a network or a model.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from src.data.tables import DiscoveryWatchRow
from src.scout.runs import ScoutRunRepository
from tests.atlas.conftest import QUOTE
from tests.runner.provider import MarketProvider, pool
from tests.scout.conftest import scout, scout_settings

T0 = datetime(2026, 10, 17, 6, tzinfo=UTC)
FIVE = timedelta(minutes=5)
V4_VENUE = "uniswap-v4-robinhood"


def v3(index: int) -> dict[str, Any]:
    """A 20-byte (V2/V3-style) pool with its own base token."""
    return pool(
        "0x" + f"{index:040x}",
        base="0x" + f"{index + 100_000:040x}",
        quote=QUOTE,
        price="0.001",
        liquidity="5000",
        volume="100",
    )


def v4(index: int) -> dict[str, Any]:
    """A 32-byte (V4-style) pool id with its own base token."""
    return pool(
        "0x" + f"{index:064x}",
        base="0x" + f"{index + 200_000:040x}",
        quote=QUOTE,
        price="0.001",
        liquidity="5000",
        volume="100",
        venue=V4_VENUE,
    )


def page(start: int, count: int = 20) -> list[dict[str, Any]]:
    return [v3(index) for index in range(start, start + count)]


def settings(**overrides: Any):
    base: dict[str, Any] = {
        "early_scout_max_discovery_pools": 20,
        "early_scout_max_new_watches_per_run": 50,
        "early_scout_max_orbit_reviews_per_run": 0,
        "early_scout_history_max_requests_per_run": 0,
        "early_scout_max_refresh_markets_per_run": 0,
        "geckoterminal_retries": 0,
        "early_scout_discovery_page2_enabled": True,
    }
    return scout_settings(**{**base, **overrides})


async def watches(sessions) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(DiscoveryWatchRow)))


async def seed(sessions, pools: list[dict[str, Any]], at: datetime = T0) -> None:
    """A previous poll: these pools were seen before the run under test."""
    await scout(
        sessions,
        at,
        provider=MarketProvider(discovery_pages={1: pools}),
        settings=settings(early_scout_discovery_page2_enabled=False),
    )


# ------------------------------------------------------------ the whole page


async def test_all_twenty_pools_of_a_page_become_watches(db):
    _, sessions = db
    provider = MarketProvider(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0, provider=provider, settings=settings())
    assert summary.discovered == 20
    assert summary.valid_markets == 20
    assert summary.watches_created == 20
    assert await watches(sessions) == 20


# ------------------------------------------------------------ the overlap reference


async def test_the_first_poll_ever_has_no_reference_and_asks_for_no_second_page(db):
    _, sessions = db
    provider = MarketProvider(discovery_pages={1: page(1), 2: page(21)})
    summary = await scout(sessions, T0, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_page2_requests == 0
    assert summary.discovery_no_overlap_polls == 0
    assert summary.discovery_possible_gaps == 0


async def test_page_one_overlapping_the_previous_poll_reads_no_second_page(db):
    _, sessions = db
    await seed(sessions, page(1))
    # Seven pools carried over from the previous poll, thirteen new ones.
    provider = MarketProvider(discovery_pages={1: page(14), 2: page(34)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_no_overlap_polls == 0
    assert summary.discovery_possible_gaps == 0
    assert summary.watches_created == 13


async def test_no_overlap_reads_page_two_once_and_page_two_closes_the_gap(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41), 2: page(18)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_no_overlap_polls == 1
    assert summary.discovery_page2_requests == 1
    assert summary.discovery_possible_gaps == 0
    # Page 2 is discovery data like page 1: its new pools become watches too.
    assert summary.watches_created == 20 + 17
    assert await watches(sessions) == 20 + 37


async def test_page_two_without_overlap_is_recorded_as_a_possible_gap(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(61), 2: page(41)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_page2_requests == 1
    assert summary.discovery_possible_gaps == 1
    assert "DISCOVERY_PAGE2_NO_OVERLAP" in summary.discovery_gap_reasons


async def test_a_short_page_one_is_the_whole_list_and_never_a_gap(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41, 12)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_possible_gaps == 0


async def test_pools_repeated_across_the_page_boundary_are_counted_once(db):
    _, sessions = db
    await seed(sessions, page(1))
    # Pools shifted down by new listings appear on both pages.
    provider = MarketProvider(discovery_pages={1: page(41), 2: [*page(55, 6), *page(15, 14)]})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert summary.discovery_possible_gaps == 0
    assert summary.discovered == 20 + 14
    # Page 2's new pools only: 21..28. Its repeats of page 1 are not new twice.
    assert summary.watches_created == 20 + 8


# ------------------------------------------------------------ off by default, and budgets


async def test_page_two_is_off_by_default_and_the_gap_is_still_recorded(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41), 2: page(18)})
    summary = await scout(
        sessions,
        T0 + FIVE,
        provider=provider,
        settings=settings(early_scout_discovery_page2_enabled=False),
    )
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_no_overlap_polls == 1
    assert summary.discovery_page2_requests == 0
    assert summary.discovery_possible_gaps == 1
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_DISABLED",)


async def test_a_truncated_page_one_never_reads_page_two(db):
    """With only the first ten pools processed, page 2 would skip the other ten."""
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41), 2: page(18)})
    summary = await scout(
        sessions,
        T0 + FIVE,
        provider=provider,
        settings=settings(early_scout_max_discovery_pools=10),
    )
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_possible_gaps == 1
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE_TRUNCATED",)


async def test_the_run_request_budget_fails_closed_before_page_two(db):
    """Page 2 never takes the request the run still needs for its refresh."""
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41), 2: page(18)})
    summary = await scout(
        sessions,
        T0 + FIVE,
        provider=provider,
        settings=settings(geckoterminal_max_requests=3),
    )
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_page2_requests == 0
    assert summary.discovery_possible_gaps == 1
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_RUN_BUDGET",)


async def test_the_daily_page_two_budget_fails_closed(db):
    _, sessions = db
    await seed(sessions, page(1))
    cap = settings(early_scout_max_discovery_page2_per_day=1)
    first = await scout(
        sessions,
        T0 + FIVE,
        provider=MarketProvider(discovery_pages={1: page(41), 2: page(18)}),
        settings=cap,
    )
    assert first.discovery_page2_requests == 1
    provider = MarketProvider(discovery_pages={1: page(101), 2: page(81)})
    second = await scout(sessions, T0 + 2 * FIVE, provider=provider, settings=cap)
    assert provider.discovery_page_numbers == [1]
    assert second.discovery_page2_requests == 0
    assert second.discovery_possible_gaps == 1
    assert second.discovery_gap_reasons == ("DISCOVERY_PAGE2_DAILY_BUDGET",)


async def test_the_daily_page_two_budget_resets_at_midnight_utc(db):
    _, sessions = db
    late = datetime(2026, 10, 17, 23, 50, tzinfo=UTC)
    await seed(sessions, page(1), at=late)
    cap = settings(early_scout_max_discovery_page2_per_day=1)
    await scout(
        sessions,
        late + FIVE,
        provider=MarketProvider(discovery_pages={1: page(41), 2: page(18)}),
        settings=cap,
    )
    provider = MarketProvider(discovery_pages={1: page(101), 2: page(58)})
    summary = await scout(sessions, late + 3 * FIVE, provider=provider, settings=cap)
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_page2_requests == 1


async def test_a_failed_page_two_keeps_page_one_and_records_the_gap(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={1: page(41)}, page_status={2: 429})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_page2_requests == 1
    assert summary.discovery_possible_gaps == 1
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_PROVIDER_RATE_LIMITED",)
    assert summary.watches_created == 20
    assert summary.provider_failures == 1


async def test_a_failed_page_one_asks_for_no_second_page(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = MarketProvider(discovery_pages={2: page(18)}, page_status={1: 429})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_page2_requests == 0


# ------------------------------------------------------------ restart safety and history


async def test_a_restart_keeps_the_reference_and_creates_no_watch_twice(db):
    """Two separate cycles, as two processes would run them: the database is the state."""
    _, sessions = db
    provider = MarketProvider(discovery_pages={1: page(1)})
    await scout(sessions, T0, provider=provider, settings=settings())
    again = MarketProvider(discovery_pages={1: page(1), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=again, settings=settings())
    assert again.discovery_page_numbers == [1]
    assert summary.watches_created == 0
    assert summary.watches_updated == 20
    assert summary.discovery_possible_gaps == 0
    assert await watches(sessions) == 20


async def test_the_run_history_keeps_the_discovery_counters(db):
    _, sessions = db
    await seed(sessions, page(1))
    await scout(
        sessions,
        T0 + FIVE,
        provider=MarketProvider(discovery_pages={1: page(61), 2: page(41)}),
        settings=settings(),
    )
    latest, *_ = await ScoutRunRepository(sessions).recent(1)
    assert latest.discovery_no_overlap_polls == 1
    assert latest.discovery_page2_requests == 1
    assert latest.discovery_possible_gaps == 1
