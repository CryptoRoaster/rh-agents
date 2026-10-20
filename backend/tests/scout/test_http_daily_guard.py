"""One daily ceiling on every GeckoTerminal HTTP attempt of the scout, retries included.

Discovery/refresh and VECTOR history each have a per-run budget on their own
transport. `EARLY_SCOUT_MAX_PROVIDER_HTTP_ATTEMPTS_PER_DAY` caps all of them
together over the UTC day: what earlier runs of the day recorded in
`scout_runs.provider_http_attempts` is subtracted, and the rest is one shared
allowance that every transport of the run draws from before each attempt.
Exhausted means no further attempt — never a catch-up later.

Nothing reaches a network, an RPC or a model.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from src.markets.geckoterminal.errors import BudgetError, RateLimitError
from src.markets.geckoterminal.transport import AttemptAllowance, GeckoTerminalTransport
from tests.scout.conftest import scout, scout_settings
from tests.scout.test_discovery_optimization import PagedNetworks
from tests.scout.test_discovery_pages import page

T0 = datetime(2026, 10, 20, 6, tzinfo=UTC)
FIVE = timedelta(minutes=5)


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


async def no_sleep(_: float) -> None:
    return None


# ------------------------------------------------------------ the shared allowance


async def test_two_transports_share_one_allowance_retries_included():
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text=json.dumps({"data": []}))

    allowance = AttemptAllowance(3)
    config = scout_settings(geckoterminal_retries=1)
    first = GeckoTerminalTransport(
        config, transport=httpx.MockTransport(handle), sleep=no_sleep, allowance=allowance
    )
    second = GeckoTerminalTransport(
        config, transport=httpx.MockTransport(handle), sleep=no_sleep, allowance=allowance
    )
    async with first, second:
        await first.get("a", {})  # 429, then 200: two attempts
        await second.get("b", {})  # one attempt
        with pytest.raises(BudgetError):
            await second.get("c", {})
        with pytest.raises(BudgetError):
            await first.get("d", {})
    assert len(calls) == 3
    assert allowance.used == 3
    assert first.attempts_remaining == 0
    assert second.attempts_remaining == 0


async def test_an_exhausted_allowance_is_not_spent_by_a_retry():
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "0"})

    allowance = AttemptAllowance(1)
    transport = GeckoTerminalTransport(
        scout_settings(geckoterminal_retries=2),
        transport=httpx.MockTransport(handle),
        sleep=no_sleep,
        allowance=allowance,
    )
    async with transport:
        with pytest.raises((BudgetError, RateLimitError)):
            await transport.get("a", {})
    assert len(calls) == 1


# ------------------------------------------------------------ the daily ceiling in the scout


async def test_no_ceiling_unless_one_is_set(db):
    _, sessions = db
    provider = PagedNetworks(discovery_pages={1: page(1)})
    summary = await scout(sessions, T0, provider=provider, settings=settings())
    assert summary.provider_http_daily_budget == 0
    assert summary.provider_http_attempts == 4


async def test_the_day_ceiling_counts_earlier_runs_and_stops_the_next_attempt(db):
    _, sessions = db
    capped = settings(early_scout_max_provider_http_attempts_per_day=6)
    first = PagedNetworks(discovery_pages={1: page(1)})
    one = await scout(sessions, T0, provider=first, settings=capped)
    assert one.provider_http_attempts == 4  # three directory pages and page 1
    second = PagedNetworks(discovery_pages={1: page(1)})
    two = await scout(sessions, T0 + FIVE, provider=second, settings=capped)
    # Two attempts were left: the directory scan cannot finish, page 1 is never asked.
    assert second.network_pages == [1, 2]
    assert second.discovery_page_numbers == []
    assert two.provider_http_daily_used_before == 4
    assert two.provider_http_attempts == 2
    assert "REQUEST_BUDGET_EXHAUSTED" in two.rejections
    third = PagedNetworks(discovery_pages={1: page(1)})
    three = await scout(sessions, T0 + 2 * FIVE, provider=third, settings=capped)
    assert third.requests == []
    assert three.provider_http_attempts == 0
    assert three.provider_http_daily_used_before == 6


async def test_the_ceiling_resets_at_midnight_utc_without_catching_up(db):
    _, sessions = db
    late = datetime(2026, 10, 20, 23, 50, tzinfo=UTC)
    capped = settings(early_scout_max_provider_http_attempts_per_day=4)
    await scout(
        sessions, late, provider=PagedNetworks(discovery_pages={1: page(1)}), settings=capped
    )
    blocked = PagedNetworks(discovery_pages={1: page(1)})
    await scout(sessions, late + FIVE, provider=blocked, settings=capped)
    assert blocked.requests == []
    after = PagedNetworks(discovery_pages={1: page(1)})
    summary = await scout(sessions, late + 3 * FIVE, provider=after, settings=capped)
    # A new day: the day's own ceiling, not yesterday's unused rest on top.
    assert summary.provider_http_daily_used_before == 0
    assert summary.provider_http_attempts == 4


async def test_page_two_is_refused_when_the_day_cannot_carry_it(db):
    _, sessions = db
    await scout(
        sessions,
        T0,
        provider=PagedNetworks(discovery_pages={1: page(1)}),
        settings=settings(early_scout_network_cache_ttl_seconds=21600),
    )
    capped = settings(
        early_scout_network_cache_ttl_seconds=21600,
        early_scout_max_provider_http_attempts_per_day=5,
    )
    provider = PagedNetworks(discovery_pages={1: page(41), 2: page(21)})
    summary = await scout(sessions, T0 + FIVE, provider=provider, settings=capped)
    # Four used, one left: page 1 takes it, page 2 is never asked.
    assert provider.discovery_page_numbers == [1]
    assert summary.discovery_gap_reasons == ("DISCOVERY_PAGE2_RUN_BUDGET",)


class WithHistory(PagedNetworks):
    """OHLCV reads answered 503, so each due watch costs exactly one attempt."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ohlcv: list[str] = []

    def handle(self, request):
        if "/ohlcv/" in request.url.path:
            self.requests.append(request)
            self.ohlcv.append(request.url.path)
            return httpx.Response(503, text=json.dumps({"errors": [{"status": "503"}]}))
        return super().handle(request)


async def test_history_draws_from_the_same_daily_allowance(db):
    _, sessions = db
    history = {"early_scout_history_max_requests_per_run": 6}
    await scout(
        sessions,
        T0,
        provider=PagedNetworks(discovery_pages={1: page(1)}),
        settings=settings(**history),
        history=False,
    )
    # A day later every watch is due for its first history read.
    later = T0 + timedelta(hours=25)
    free = WithHistory(discovery_pages={1: page(1)})
    unbounded = await scout(
        sessions, later, provider=free, settings=settings(**history), history=False
    )
    assert len(free.ohlcv) == 6
    assert unbounded.provider_http_attempts == 4 + 6


async def test_history_stops_when_the_day_is_spent(db):
    _, sessions = db
    history = {"early_scout_history_max_requests_per_run": 6}
    await scout(
        sessions,
        T0,
        provider=PagedNetworks(discovery_pages={1: page(1)}),
        settings=settings(**history),
        history=False,
    )
    later = T0 + timedelta(hours=25)
    capped = settings(**history, early_scout_max_provider_http_attempts_per_day=6)
    provider = WithHistory(discovery_pages={1: page(1)})
    summary = await scout(sessions, later, provider=provider, settings=capped, history=False)
    # Discovery took four of the day's six; history gets the other two, no more.
    assert len(provider.ohlcv) == 2
    assert summary.provider_http_attempts == 6
    # Our own limit is not the watch's failure: no backoff from the refused reads.
    assert summary.history_backoff_set == 2


def test_the_daily_ceiling_is_bounded():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        scout_settings(early_scout_max_provider_http_attempts_per_day=5761)
    with pytest.raises(ValidationError):
        scout_settings(early_scout_max_provider_http_attempts_per_day=-1)
