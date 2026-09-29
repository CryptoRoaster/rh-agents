"""Discovery decoupled from paid review capacity, the daily ORBIT bound, and
the batched exact-locator refresh.

Discovery is the cheap input: every valid pool it finds, up to its own bound,
becomes a watch, whatever ORBIT can afford. Paid reviews are bounded per run and
per UTC day, the daily count read from the persisted assessment history so it
holds across scheduled processes. A watch the budget cannot reach waits.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.data.tables import DiscoveryWatchAssessmentRow, ScoutOrbitReservationRow
from src.reasoning.models import ReasoningErrorCategory
from src.scout.budget import OrbitBudget
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
    """Record `count` already-reserved paid reviews on a separate watch at `at`."""
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
                ScoutOrbitReservationRow(
                    id=uuid4(),
                    watch_id=watch.id,
                    checkpoint_index=100 + index,
                    utc_day=at.astimezone(UTC).date(),
                    reserved_at=at,
                    status="COMPLETED",
                    assessment_id=None,
                    completed_at=at,
                    failure_reason=None,
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


# ------------------------------------------------ durable pre-call reservation


class ReservationCheckingOrbit(EchoOrbit):
    """Asserts, on entering the model call, that its slot is already committed."""

    def __init__(self, sessions, **kwargs):
        super().__init__(**kwargs)
        self.sessions = sessions
        self.seen: list[int] = []

    async def generate_structured(self, request):
        from sqlalchemy import func, select

        # A separate session: only what is committed is visible here.
        async with self.sessions() as session:
            reserved = await session.scalar(
                select(func.count())
                .select_from(ScoutOrbitReservationRow)
                .where(ScoutOrbitReservationRow.status == "RESERVED")
            )
        self.seen.append(int(reserved or 0))
        return await super().generate_structured(request)


async def test_no_model_call_starts_without_a_committed_reservation(db):
    _, sessions = db
    orbit = ReservationCheckingOrbit(sessions)
    await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:3]), orbit=orbit, settings=budget()
    )
    assert len(orbit.calls) == 3
    # Exactly this call's slot was RESERVED and committed when the call began.
    assert orbit.seen == [1, 1, 1]
    async with sessions() as session:
        from sqlalchemy import select

        rows = (await session.scalars(select(ScoutOrbitReservationRow))).all()
    assert sorted(row.status for row in rows) == ["COMPLETED"] * 3
    assert all(row.assessment_id is not None for row in rows)


class CrashingOrbit(EchoOrbit):
    """The process dies inside the model call, before any assessment is written."""

    async def generate_structured(self, request):
        self.calls.append(request)
        raise KeyboardInterrupt


async def test_a_call_that_crashes_before_its_assessment_still_counts(db):
    import pytest

    _, sessions = db
    provider = MarketProvider(discovery=TEN[:6])
    for attempt in range(4):
        with pytest.raises(KeyboardInterrupt):
            await scout(
                sessions,
                T0 + timedelta(minutes=attempt),
                provider=provider,
                orbit=CrashingOrbit(),
                settings=budget(per_run=4, per_day=4),
            )
        used = await OrbitBudget(sessions).used(T0.date())
        assert used == attempt + 1
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0 + timedelta(minutes=5),
        provider=provider,
        orbit=orbit,
        settings=budget(per_run=4, per_day=4),
    )
    assert orbit.calls == []
    assert summary.orbit_daily_used_before == 4
    async with sessions() as session:
        from sqlalchemy import select

        statuses = (await session.scalars(select(ScoutOrbitReservationRow.status))).all()
        assessments = (await session.scalars(select(DiscoveryWatchAssessmentRow.id))).all()
    assert sorted(statuses) == ["RESERVED"] * 4
    assert assessments == []


async def test_a_reservation_that_cannot_be_written_means_no_model_call(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _, sessions = db

    async def refuse(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("database gone"))

    monkeypatch.setattr(OrbitBudget, "reserve", refuse)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:3]), orbit=orbit, settings=budget()
    )
    assert orbit.calls == []
    assert "DATABASE_UNAVAILABLE" in summary.errors
    # Discovery still happened and nothing was dropped.
    assert summary.watches_created == 3


async def test_an_unreadable_daily_count_means_no_model_call(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _, sessions = db

    async def unreadable(*args, **kwargs):
        raise OperationalError("select", {}, Exception("database gone"))

    monkeypatch.setattr(OrbitBudget, "used", unreadable)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=TEN[:3]), orbit=orbit, settings=budget()
    )
    assert orbit.calls == []
    assert "DATABASE_UNAVAILABLE" in summary.errors


async def test_a_slot_left_before_its_checkpoint_was_claimed_is_reused_not_doubled(db):
    """Reserve, then claim, then call: an unclaimed RESERVED slot means no call was made."""
    _, sessions = db
    await scout(
        sessions, T0, provider=MarketProvider(discovery=[young(0)]), settings=budget(per_run=0)
    )
    watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[0]))
    first = await OrbitBudget(sessions).reserve(watch.id, 0, T0, 96)
    orbit = EchoOrbit()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=[young(0)]), orbit=orbit, settings=budget()
    )
    assert len(orbit.calls) == 1
    assert summary.orbit_daily_used_before == 1
    assert summary.orbit_daily_used_after == 1
    async with sessions() as session:
        from sqlalchemy import select

        (row,) = (await session.scalars(select(ScoutOrbitReservationRow))).all()
    assert row.id == first and row.status == "COMPLETED"


async def test_reviews_and_history_checks_share_one_refresh_request(db):
    _, sessions = db
    pools = [young(0), young(1)]
    provider = MarketProvider(discovery=pools, targeted=pools)
    await scout(sessions, T0, provider=provider, settings=budget(per_run=0))
    provider.discovery = []
    from tests.scout.conftest import ScriptedHistory

    summary = await scout(
        sessions,
        T0 + timedelta(hours=24),
        provider=provider,
        history=ScriptedHistory(3),
        settings=budget(per_run=1).model_copy(
            update={"early_scout_history_max_requests_per_run": 1}
        ),
    )
    assert summary.orbit_reviews_started == 1 and summary.history_checks == 1
    assert len(provider.multi_requests) == 1
    # networks + new_pools + one batched refresh (history is scripted here).
    assert summary.provider_requests == 3
