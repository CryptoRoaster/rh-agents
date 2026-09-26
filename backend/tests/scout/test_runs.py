"""Scout run history, ORBIT backlog visibility, the review bound and the run lock."""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from src.data.tables import ScoutRunRow
from src.scout.runs import ScoutRunRepository
from tests.scout.conftest import (
    HOUR,
    EchoOrbit,
    MarketProvider,
    scout,
    scout_settings,
    young,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)


async def runs(sessions):
    return await ScoutRunRepository(sessions).recent(10)


async def test_a_completed_run_is_persisted_with_its_summary_counts(db):
    _, sessions = db
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0), young(1)]))
    (run,) = await runs(sessions)
    assert summary.run_id == run.id
    assert run.status == "COMPLETED"
    assert run.started_at == T0 and run.completed_at == T0
    for name in (
        "discovered",
        "valid_markets",
        "provider_identity_rejects",
        "other_provider_rejects",
        "watches_created",
        "orbit_reviews_started",
        "orbit_reviews_completed",
        "not_interesting",
        "orbit_backlog_before",
        "orbit_backlog_after",
        "new_watches_without_orbit_assessment",
    ):
        assert getattr(run, name) == getattr(summary, name), name
    assert run.identity_acceptance_rate == 1.0
    assert run.watch_creation_rate == 1.0


async def test_rates_are_absent_rather_than_zero_when_nothing_was_discovered(db):
    _, sessions = db
    await scout(sessions, T0, provider=MarketProvider())
    (run,) = await runs(sessions)
    assert run.discovered == 0
    assert run.identity_acceptance_rate is None
    assert run.watch_creation_rate is None


async def test_a_stopped_run_is_terminal_and_recorded(db):
    _, sessions = db
    from tests.riskrequest.conftest import set_account

    await set_account(sessions, paused=True)
    await scout(sessions, T0, provider=MarketProvider(discovery=[young(0)]))
    (run,) = await runs(sessions)
    assert run.status == "STOPPED" and run.stop == "SYSTEM_STOPPED"


async def test_a_disabled_scout_leaves_no_run_row(db):
    _, sessions = db
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0)]),
        settings=scout_settings(early_scout_enabled=False),
    )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ScoutRunRow)) == 0


async def test_the_backlog_shows_when_orbit_falls_behind_and_when_it_catches_up(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0), young(1), young(2)])
    behind = await scout(
        sessions,
        T0,
        provider=provider,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=1),
    )
    assert (behind.orbit_backlog_before, behind.orbit_backlog_after) == (3, 2)
    assert behind.oldest_orbit_due_age_seconds == 0
    assert behind.new_watches_without_orbit_assessment == 2
    later = await scout(
        sessions,
        T0 + HOUR / 4,
        provider=provider,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=5),
    )
    assert later.oldest_orbit_due_age_seconds == 900
    assert (later.orbit_backlog_before, later.orbit_backlog_after) == (2, 0)
    assert later.new_watches_without_orbit_assessment == 0


async def test_no_run_makes_more_orbit_calls_than_its_budget(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0), young(1), young(2)]),
        orbit=orbit,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=2),
    )
    assert len(orbit.calls) == 2
    assert summary.orbit_reviews_started == 2


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="advisory locks are PostgreSQL")
async def test_a_second_scout_run_gives_way_to_one_already_running(db):
    engine, sessions = db
    from src.scout.service import RUN_LOCK

    async with engine.connect() as holder:
        assert await holder.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": RUN_LOCK}
        )
        provider, orbit = MarketProvider(discovery=[young(0)]), EchoOrbit()
        summary = await scout(sessions, T0, provider=provider, orbit=orbit)
        await holder.scalar(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": RUN_LOCK})
    assert summary.stop == "ALREADY_RUNNING"
    assert summary.run_id is None and summary.errors == ()
    assert provider.paths == [] and orbit.calls == []
    assert await runs(sessions) == ()
    # And the lock is released after a normal run: the next one proceeds.
    after = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0)]))
    assert after.stop == "COMPLETED"
