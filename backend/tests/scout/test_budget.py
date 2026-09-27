"""Discovery decoupled from paid review capacity, the daily ORBIT bound, and
the batched exact-locator refresh.

Discovery is the cheap input: every valid pool it finds, up to its own bound,
becomes a watch, whatever ORBIT can afford. Paid reviews are bounded per run and
per UTC day, the daily count read from the persisted assessment history so it
holds across scheduled processes. A watch the budget cannot reach waits.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.data.tables import DiscoveryWatchAssessmentRow
from src.reasoning.models import ReasoningErrorCategory
from src.scout.policy import WatchStatus
from src.scout.repository import WatchRepository
from tests.runner.provider import MarketProvider as BaseProvider
from tests.scout.conftest import (
    HOUR,
    POOLS,
    EchoOrbit,
    MarketProvider,
    pair_id,
    scout,
    scout_settings,
    young,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)
TEN = [young(index) for index in range(10)]


def budget(*, per_run=4, per_day=96, new=10, refresh=4):
    return scout_settings(
        early_scout_max_new_watches_per_run=new,
        early_scout_max_orbit_reviews_per_run=per_run,
        early_scout_max_orbit_reviews_per_day=per_day,
        early_scout_max_refresh_markets_per_run=refresh,
    )


async def spend(sessions, count: int, at: datetime) -> None:
    """Record `count` already-started reviews on a separate watch at `at`."""
    await scout(
        sessions,
        at - timedelta(hours=1),
        provider=MarketProvider(discovery=[young(9)]),
        settings=budget(per_run=0),
    )
    repository = WatchRepository(sessions)
    watch = await repository.by_pair(pair_id(POOLS[9]))
    # Out of the due queue, so only its spent budget matters here.
    await repository.retire(watch.id, "TEST_BUDGET_HOLDER", at)
    async with sessions.begin() as session:
        for index in range(count):
            session.add(
                DiscoveryWatchAssessmentRow(
                    id=uuid4(),
                    watch_id=watch.id,
                    snapshot_id=watch.latest_snapshot_id,
                    assessed_at=at,
                    checkpoint_index=100 + index,
                    checkpoint_seconds=0,
                    status="FAILED",
                    failure_reason="PROVIDER_TIMEOUT",
                    classification=None,
                    strength=None,
                    reason_codes=[],
                    data_gaps=[],
                    cited_observation_ids=[],
                    summary=None,
                    input_digest="0" * 64,
                    policy_version="early-scout-v1",
                    prompt_version="orbit-v1",
                    prompt_hash="0" * 64,
                    output_schema_version=1,
                    reasoning_provider=None,
                    reasoning_model=None,
                    input_tokens=None,
                    output_tokens=None,
                    latency_ms=None,
                )
            )


# ------------------------------------------------ discovery is not throttled


async def test_every_valid_pool_becomes_a_watch_whatever_orbit_can_afford(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=TEN),
        orbit=orbit,
        settings=budget(per_run=3),
    )
    assert summary.valid_markets == 10
    assert summary.watches_created == 10
    assert summary.orbit_reviews_completed == 3 and len(orbit.calls) == 3
    assert summary.orbit_backlog_after == 7
    assert summary.new_watches_without_orbit_assessment == 7
    for index in range(10):
        watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[index]))
        assert watch.status is WatchStatus.WATCHING


async def test_discovery_continues_when_the_daily_budget_is_spent(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=TEN),
        orbit=orbit,
        settings=budget(per_day=0),
    )
    assert summary.watches_created == 10
    assert orbit.calls == []
    assert summary.orbit_daily_remaining_before == 0
    watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[0]))
    # Still owed its review, and still due: nothing was dropped for the budget.
    assert watch.status is WatchStatus.WATCHING
    assert watch.next_orbit_review_at == T0


# ------------------------------------------------------------ daily bound


async def test_an_unused_day_allows_the_per_run_bound(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=TEN), orbit=orbit)
    assert len(orbit.calls) == 5  # scout_settings' per-run bound
    summary = await scout(
        sessions,
        T0 + timedelta(minutes=15),
        provider=MarketProvider(),
        orbit=(orbit := EchoOrbit()),
        settings=budget(per_run=4),
    )
    assert len(orbit.calls) == 4
    assert summary.orbit_daily_used_before == 5
    assert summary.orbit_daily_used_after == 9
    assert summary.orbit_daily_remaining_after == 87


async def test_a_nearly_spent_day_allows_only_what_is_left(db):
    _, sessions = db
    await spend(sessions, 94, T0)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:9]), orbit=orbit, settings=budget()
    )
    assert len(orbit.calls) == 2
    assert (summary.orbit_daily_used_before, summary.orbit_daily_remaining_before) == (94, 2)
    assert (summary.orbit_daily_used_after, summary.orbit_daily_remaining_after) == (96, 0)


async def test_a_spent_day_allows_no_call(db):
    _, sessions = db
    await spend(sessions, 96, T0)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:9]), orbit=orbit, settings=budget()
    )
    assert orbit.calls == []
    assert summary.orbit_daily_remaining_before == 0
    assert summary.watches_created == 9


async def test_a_day_overspent_under_an_older_configuration_allows_no_call(db):
    _, sessions = db
    await spend(sessions, 100, T0)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:3]), orbit=orbit, settings=budget()
    )
    assert orbit.calls == []
    assert summary.orbit_daily_remaining_before == 0


async def test_a_new_utc_day_has_a_new_budget(db):
    _, sessions = db
    midnight = datetime(2026, 9, 27, tzinfo=UTC)
    await spend(sessions, 96, midnight - timedelta(minutes=5))
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        midnight + timedelta(minutes=1),
        provider=MarketProvider(discovery=TEN[:3]),
        orbit=orbit,
        settings=budget(),
    )
    assert len(orbit.calls) == 3
    assert summary.orbit_daily_used_before == 0


async def test_a_failed_model_call_counts_against_the_budget(db):
    _, sessions = db
    orbit = EchoOrbit(failure=ReasoningErrorCategory.PROVIDER_TIMEOUT)
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:2]), orbit=orbit, settings=budget()
    )
    assert summary.model_failures == 2
    assert summary.orbit_daily_used_after == 2
    assert summary.orbit_daily_remaining_after == 94


# ------------------------------------------------- batched exact-locator refresh


async def stale_watches(sessions, pools):
    """Watches discovered at T0 with no review yet; their readings are old by T0+1h."""
    provider = MarketProvider(discovery=pools, targeted=pools)
    await scout(sessions, T0, provider=provider, settings=budget(per_run=0))
    provider.discovery = []
    return provider


async def test_a_fresh_due_watch_is_not_refreshed(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    orbit = EchoOrbit()
    await scout(sessions, T0, provider=provider, orbit=orbit, settings=budget())
    assert len(orbit.calls) == 1
    assert provider.multi_requests == []


async def test_an_identity_contradiction_retires_only_that_watch(db):
    _, sessions = db
    from tests.atlas.conftest import QUOTE
    from tests.runner.provider import pool

    provider = await stale_watches(sessions, [young(0), young(1)])
    provider.targeted[POOLS[1]] = pool(POOLS[1], base="0x" + "ee" * 20, quote=QUOTE, price="1")
    orbit = EchoOrbit()
    summary = await scout(sessions, T0 + HOUR, provider=provider, orbit=orbit, settings=budget())
    assert len(provider.multi_requests) == 1
    retired = await WatchRepository(sessions).by_pair(pair_id(POOLS[1]))
    assert retired.status is WatchStatus.RETIRED
    assert retired.reason_code == "MARKET_IDENTITY_MISMATCH"
    assert summary.refreshed == 1 and summary.retired_new == 1
    assert [call.data["market_observation"]["pair_id"] for call in orbit.calls] == [
        pair_id(POOLS[0])
    ]


async def test_a_missing_pool_leaves_no_observation_and_the_others_proceed(db):
    _, sessions = db
    provider = await stale_watches(sessions, [young(0), young(1)])
    del provider.targeted[POOLS[0]]
    orbit = EchoOrbit()
    summary = await scout(sessions, T0 + HOUR, provider=provider, orbit=orbit, settings=budget())
    missing = await WatchRepository(sessions).by_pair(pair_id(POOLS[0]))
    assert missing.reason_code == "MARKET_NOT_RETURNED"
    assert missing.last_seen_at == T0
    assert summary.refreshed == 1 and len(orbit.calls) == 1


class ExtraPoolProvider(BaseProvider):
    """Answers a pools/multi request with an extra, or a repeated, pool."""

    def __init__(self, *args, extra=None, duplicate=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra = extra
        self.duplicate = duplicate

    def handle(self, request):
        response = super().handle(request)
        if "/pools/multi/" not in request.url.path:
            return response
        import json

        from tests.runner.provider import document

        body = json.loads(response.text)
        pools = body["data"]
        if self.extra is not None:
            pools = [*pools, self.extra]
        if self.duplicate:
            pools = [*pools, pools[0]]
        return self._json(document(pools))


async def test_an_unrequested_pool_in_the_answer_fails_the_batch_closed(db):
    _, sessions = db
    await stale_watches(sessions, [young(0), young(1)])
    provider = ExtraPoolProvider(targeted=[young(0), young(1)], extra=young(2))
    orbit = EchoOrbit()
    summary = await scout(sessions, T0 + HOUR, provider=provider, orbit=orbit, settings=budget())
    assert summary.refreshed == 0 and summary.provider_failures == 1
    assert orbit.calls == []
    assert await WatchRepository(sessions).by_pair(pair_id(POOLS[2])) is None
    for index in (0, 1):
        watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[index]))
        assert watch.status is WatchStatus.WATCHING and watch.last_seen_at == T0


async def test_a_duplicated_pool_in_the_answer_fails_the_batch_closed(db):
    _, sessions = db
    await stale_watches(sessions, [young(0), young(1)])
    provider = ExtraPoolProvider(targeted=[young(0), young(1)], duplicate=True)
    orbit = EchoOrbit()
    summary = await scout(sessions, T0 + HOUR, provider=provider, orbit=orbit, settings=budget())
    assert summary.refreshed == 0 and summary.provider_failures == 1
    assert orbit.calls == []


async def test_the_refresh_asks_by_the_stored_locators_only(db):
    _, sessions = db
    provider = await stale_watches(sessions, [young(0), young(1), young(2)])
    await scout(sessions, T0 + HOUR, provider=provider, settings=budget(refresh=2, per_run=4))
    (request,) = provider.multi_requests
    asked = request.rsplit("/", 1)[-1].split(",")
    assert asked == [POOLS[0], POOLS[1]]
