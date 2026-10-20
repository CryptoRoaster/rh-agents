"""Local discovery optimisation: the network binding cache, page 2 and pool age.

The live observation (2026-10-19) spent three of the run's five requests on the
paginated `/networks` directory — GeckoTerminal lists Robinhood on page 3 — so
page 2 of `new_pools` was always refused for the run budget. A validated
binding is now kept for a bounded time and reused by later runs; anything that
cannot be shown to be that same validated binding is scanned for again.

Every test drives the real scout cycle against a scripted provider whose
network directory has the real three-page layout. Nothing reaches a network,
an RPC or a model.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from src.data.repository import aware
from src.data.tables import (
    DiscoveryWatchRow,
    ProviderNetworkBindingRow,
    TradeCaseRow,
)
from src.scout.runs import ScoutRunRepository
from tests.runner.provider import NETWORK_ID, MarketProvider
from tests.scout.conftest import scout
from tests.scout.test_discovery_pages import page, settings, v3, v4

T0 = datetime(2026, 10, 20, 6, tzinfo=UTC)
FIVE = timedelta(minutes=5)
HOUR = timedelta(hours=1)

BSC = {
    "id": "bsc",
    "type": "network",
    "attributes": {"name": "BNB Chain", "coingecko_asset_platform_id": "binance-smart-chain"},
}
FILLER = {
    "id": "eth",
    "type": "network",
    "attributes": {"name": "Ethereum", "coingecko_asset_platform_id": "ethereum"},
}
RH = {
    "id": NETWORK_ID,
    "type": "network",
    "attributes": {"name": "Robinhood", "coingecko_asset_platform_id": "robinhood"},
}


class PagedNetworks(MarketProvider):
    """The provider with the real directory layout: Robinhood on page 3 of 3."""

    def __init__(self, *, rh: dict[str, Any] = RH, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.directory = [[BSC], [FILLER], [rh]]
        self.network_pages: list[int] = []

    def handle(self, request):
        if request.url.path.endswith("/networks"):
            self.requests.append(request)
            self.paths.append(request.url.path)
            number = int(request.url.params.get("page", "1"))
            self.network_pages.append(number)
            last = number >= len(self.directory)
            return self._json(
                {
                    "data": self.directory[number - 1] if number <= len(self.directory) else [],
                    "links": {"next": None if last else f"page={number + 1}"},
                }
            )
        return super().handle(request)


def cached(**overrides: Any):
    return settings(early_scout_network_cache_ttl_seconds=21600, **overrides)


async def bindings(sessions) -> list[ProviderNetworkBindingRow]:
    async with sessions() as session:
        return list((await session.scalars(select(ProviderNetworkBindingRow))).all())


async def count(sessions, table) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(table)))


async def seed(sessions, pools, at=T0, **overrides: Any) -> PagedNetworks:
    """A previous poll on a cached profile; it also writes the binding."""
    provider = PagedNetworks(discovery_pages={1: pools})
    await scout(
        sessions,
        at,
        provider=provider,
        settings=cached(early_scout_discovery_page2_enabled=False, **overrides),
    )
    return provider


# ------------------------------------------------------------ why three /networks requests


async def test_without_the_cache_every_run_scans_three_network_pages_and_page_two_is_refused(db):
    """The observed live behaviour, reproduced: 3 + 1 of 5 requests, page 2 blocked."""
    _, sessions = db
    first = PagedNetworks(discovery_pages={1: page(1)})
    await scout(sessions, T0, provider=first, settings=settings())
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.network_pages == [1, 2, 3]
    assert provider.discovery_page_numbers == [1]
    assert summary.network_requests == 3
    assert summary.network_cache_hits == 0
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_RUN_BUDGET",)
    assert await bindings(sessions) == []


# ------------------------------------------------------------ the binding cache


async def test_a_validated_binding_is_stored_and_the_next_run_spends_no_network_request(db):
    _, sessions = db
    first = await seed(sessions, page(1))
    assert first.network_pages == [1, 2, 3]
    (row,) = await bindings(sessions)
    assert (row.provider, row.chain, row.network_id, row.platform) == (
        "geckoterminal",
        "robinhood",
        NETWORK_ID,
        "robinhood",
    )
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert provider.network_pages == []
    assert summary.network_requests == 0
    assert summary.network_cache_hits == 1
    # Page 1 and page 2 together: two requests, with the run budget to spare.
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_page2_requests == 1
    assert summary.provider_requests == 2


async def test_page_two_closes_the_gap_when_it_reaches_the_previous_poll(db):
    _, sessions = db
    await seed(sessions, page(1))
    # 30 new pools since the last poll: page 1 holds 20, page 2 the other 10
    # and then ten that were already seen.
    provider = PagedNetworks(discovery_pages={1: page(31), 2: page(11)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_no_overlap_polls == 1
    assert summary.discovery_possible_gaps == 0
    assert summary.watches_created == 30


async def test_an_expired_binding_is_validated_again(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0 + 7 * HOUR, provider=provider, settings=cached())
    assert provider.network_pages == [1, 2, 3]
    assert summary.network_cache_hits == 0
    (row,) = await bindings(sessions)
    assert aware(row.verified_at) == T0 + 7 * HOUR


async def test_the_cache_is_off_unless_a_ttl_is_set(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=settings())
    assert provider.network_pages == [1, 2, 3]
    assert summary.network_cache_hits == 0


@pytest.mark.parametrize(
    "change",
    [
        {"network_id": "robinhood-old"},
        {"platform": "binance-smart-chain"},
        {"verified_at": T0 + 2 * HOUR},
    ],
    ids=["other-network-id", "wrong-platform", "from-the-future"],
)
async def test_an_invalid_cache_entry_is_never_trusted(db, change):
    _, sessions = db
    await seed(sessions, page(1))
    async with sessions.begin() as session:
        await session.execute(update(ProviderNetworkBindingRow).values(**change))
    provider = PagedNetworks(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert provider.network_pages == [1, 2, 3]
    assert summary.network_cache_hits == 0
    assert summary.network_cache_rejected == 1


async def test_a_chain_missing_from_the_directory_fails_closed_and_stores_nothing(db):
    _, sessions = db
    provider = PagedNetworks(discovery_pages={1: page(1)}, rh=FILLER)
    summary = await scout(sessions, T0, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == []
    assert "UNSUPPORTED_NETWORK" in summary.rejections
    assert await bindings(sessions) == []
    assert await count(sessions, DiscoveryWatchRow) == 0


async def test_a_mapping_to_the_wrong_platform_fails_closed_and_stores_nothing(db):
    _, sessions = db
    wrong = {**RH, "attributes": {"name": "Robinhood", "coingecko_asset_platform_id": "ethereum"}}
    provider = PagedNetworks(discovery_pages={1: page(1)}, rh=wrong)
    summary = await scout(sessions, T0, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == []
    assert "UNSUPPORTED_NETWORK" in summary.rejections
    assert await bindings(sessions) == []


async def test_a_network_error_while_validating_stores_nothing(db):
    _, sessions = db

    class Broken(PagedNetworks):
        def handle(self, request):
            if request.url.path.endswith("/networks") and request.url.params.get("page") == "3":
                raise __import__("httpx").ConnectError("down", request=request)
            return super().handle(request)

    provider = Broken(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == []
    assert summary.provider_failures == 1
    assert await bindings(sessions) == []


async def test_a_cached_binding_the_provider_no_longer_answers_is_dropped(db):
    """A 404 on new_pools under a cached id: the next run validates again."""
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={}, page_status={1: 404})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert summary.network_cache_hits == 1
    assert await bindings(sessions) == []
    again = PagedNetworks(discovery_pages={1: page(1)})
    await scout(sessions, T0 + 2 * FIVE, provider=again, settings=cached())
    assert again.network_pages == [1, 2, 3]


async def test_a_binding_validated_in_this_run_is_no_cache_hit(db):
    """Page 2 resolving what page 1 validated a moment ago is not the cache at work."""
    _, sessions = db
    await scout(
        sessions, T0, provider=PagedNetworks(discovery_pages={1: page(1)}), settings=settings()
    )
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(
        sessions, T0 + FIVE, provider=provider, settings=cached(geckoterminal_max_requests=10)
    )
    assert provider.network_pages == [1, 2, 3]
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.network_cache_hits == 0


async def test_a_cached_binding_refused_on_page_two_is_dropped(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(41)}, page_status={2: 404})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_PROVIDER_CLIENT_ERROR",)
    assert await bindings(sessions) == []


async def test_a_dropped_binding_is_never_used_again_in_the_same_run():
    from src.markets.geckoterminal.networks import CHAINS, VerifiedNetworkRegistry

    registry = VerifiedNetworkRegistry()
    registry.seed("robinhood", NETWORK_ID)
    assert registry.seeded("robinhood")
    registry.drop("robinhood")
    assert registry.verified("robinhood") is None
    assert not registry.seeded("robinhood")
    registry.record("robinhood", NETWORK_ID)
    assert not registry.seeded("robinhood")
    assert registry.validated() == {"robinhood": NETWORK_ID}
    assert CHAINS["robinhood"].platform == "robinhood"


async def test_a_process_restart_reuses_the_binding_and_creates_no_watch_twice(db):
    """Two separate cycles, as two processes would run them: the database is the state."""
    _, sessions = db
    await seed(sessions, page(1))
    again = PagedNetworks(discovery_pages={1: page(1), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=again, settings=cached())
    assert again.network_pages == []
    assert again.discovery_page_numbers == [1]
    assert summary.watches_created == 0
    assert await count(sessions, DiscoveryWatchRow) == 20
    assert len(await bindings(sessions)) == 1


# ------------------------------------------------------------ rate limits and retries


async def test_a_rate_limited_page_one_never_asks_for_page_two_even_after_a_good_retry(db):
    _, sessions = db
    await seed(sessions, page(1))

    class OnceLimited(PagedNetworks):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.limited = False

        def handle(self, request):
            if request.url.path.endswith("new_pools") and not self.limited:
                self.limited = True
                self.discovery_page_numbers.append(1)
                return __import__("httpx").Response(429, headers={"Retry-After": "0"})
            return super().handle(request)

    provider = OnceLimited(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(
        sessions, T0 + FIVE, provider=provider, settings=cached(geckoterminal_retries=1)
    )
    assert provider.discovery_page_numbers == [1, 1]
    assert summary.discovery_page2_requests == 0
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_RATE_LIMITED",)
    assert summary.provider_http_attempts == 2
    assert summary.provider_requests == 1


async def test_a_rate_limited_page_two_is_tried_once_and_recorded(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(41)}, page_status={2: 429})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert provider.discovery_page_numbers == [1, 2]
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_PROVIDER_RATE_LIMITED",)
    assert summary.provider_http_attempts == 2
    assert summary.watches_created == 20


async def test_page_two_needs_its_retries_inside_the_attempt_budget(db):
    """A run that has spent its HTTP attempts on retries asks for no page 2."""
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(
        sessions,
        T0 + FIVE,
        provider=provider,
        settings=cached(geckoterminal_max_http_attempts=1),
    )
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_RUN_BUDGET",)


async def test_every_http_attempt_is_counted_and_kept_in_the_run_history(db):
    _, sessions = db
    first = PagedNetworks(discovery_pages={1: page(1)})
    await scout(sessions, T0, provider=first, settings=cached())
    latest, *_ = await ScoutRunRepository(sessions).recent(1)
    assert latest.network_requests == 3
    assert latest.network_cache_hits == 0
    assert latest.provider_http_attempts == 4
    assert latest.discovery_gap_reasons == ()
    await scout(
        sessions,
        T0 + FIVE,
        provider=PagedNetworks(discovery_pages={1: page(41), 2: page(61)}),
        settings=cached(),
    )
    latest, *_ = await ScoutRunRepository(sessions).recent(1)
    assert latest.network_requests == 0
    assert latest.network_cache_hits == 1
    assert latest.provider_http_attempts == 2
    assert latest.discovery_gap_reasons == ("DISCOVERY_PAGE2_NO_OVERLAP",)


# ------------------------------------------------------------ the observed rate, offline


async def test_six_pools_a_minute_at_five_minutes_is_covered_with_page_two(db):
    """30 new pools per poll: page 1 alone misses ten, page 2 closes the gap."""
    _, sessions = db
    await seed(sessions, page(1))
    for poll in range(1, 4):
        # Pools are numbered by age; the newest after this poll's 30 is `top`.
        top = 20 + 30 * poll
        provider = PagedNetworks(discovery_pages={1: page(top - 19), 2: page(top - 39)})
        summary = await scout(sessions, T0 + poll * FIVE, provider=provider, settings=cached())
        assert provider.discovery_page_numbers == [1, 2]
        assert summary.discovery_possible_gaps == 0
        assert summary.provider_requests == 2
    assert await count(sessions, DiscoveryWatchRow) == 20 + 3 * 30


async def test_page_two_blocked_by_the_daily_budget_leaves_the_gap_recorded(db):
    _, sessions = db
    await seed(sessions, page(1))
    capped = cached(early_scout_max_discovery_page2_per_day=0)
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=capped)
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_DAILY_BUDGET",)


# ------------------------------------------------------------ pool creation time


def created(item: dict[str, Any], value: Any) -> dict[str, Any]:
    return {**item, "attributes": {**item["attributes"], "pool_created_at": value}}


async def ages(sessions) -> dict[str, datetime | None]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(DiscoveryWatchRow.pair_id, DiscoveryWatchRow.pool_created_at)
            )
        ).all()
    return {
        pair_id.rsplit(":", 1)[-1]: None if value is None else aware(value)
        for pair_id, value in rows
    }


async def test_the_provider_pool_creation_time_is_kept_and_a_missing_one_stays_unknown(db):
    _, sessions = db
    pools = [
        created(v4(1), "2026-10-20T05:57:30Z"),
        created(v3(2), "2026-10-20T05:51:00+00:00"),
        v3(3),
        created(v3(4), None),
        created(v3(5), "not a time"),
        created(v3(6), "2026-10-20T05:59:00"),  # no zone: not a point in time
        created(v3(7), "2026-10-20T06:00:01Z"),  # after the fetch: impossible
        created(v3(8), 1760939820),
        # Out of range once in UTC, and long before any chain: never a creation time.
        created(v3(9), "0001-01-01T00:00:00+14:00"),
        created(v3(10), "2009-01-03T18:15:05Z"),
    ]
    summary = await scout(
        sessions, T0, provider=PagedNetworks(discovery_pages={1: pools}), settings=cached()
    )
    assert summary.watches_created == 10
    assert summary.errors == ()
    stored = await ages(sessions)
    assert stored["0x" + f"{1:064x}"] == datetime(2026, 10, 20, 5, 57, 30, tzinfo=UTC)
    assert stored["0x" + f"{2:040x}"] == datetime(2026, 10, 20, 5, 51, tzinfo=UTC)
    assert all(value is None or value.utcoffset() == timedelta(0) for value in stored.values())
    for index in (3, 4, 5, 6, 7, 8, 9, 10):
        assert stored["0x" + f"{index:040x}"] is None


async def test_a_creation_time_is_written_once_and_never_moved(db):
    _, sessions = db
    first = [created(v3(1), "2026-10-20T05:58:00Z"), v3(2)]
    await scout(sessions, T0, provider=PagedNetworks(discovery_pages={1: first}), settings=cached())
    later = [created(v3(1), "2026-10-20T05:00:00Z"), created(v3(2), "2026-10-20T05:59:00Z")]
    await scout(
        sessions, T0 + FIVE, provider=PagedNetworks(discovery_pages={1: later}), settings=cached()
    )
    stored = await ages(sessions)
    assert stored["0x" + f"{1:040x}"] == datetime(2026, 10, 20, 5, 58, tzinfo=UTC)
    # Unknown at first, stated later: the provider's own value fills it once.
    assert stored["0x" + f"{2:040x}"] == datetime(2026, 10, 20, 5, 59, tzinfo=UTC)


# ------------------------------------------------------------ OBSERVE stays observation


async def test_an_observe_scout_with_page_two_and_the_cache_opens_no_case(db):
    _, sessions = db
    await seed(sessions, page(1))
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=cached())
    assert summary.watches_created == 40
    assert await count(sessions, TradeCaseRow) == 0
    assert not any("/swap" in path or "/quote" in path for path in provider.paths)
