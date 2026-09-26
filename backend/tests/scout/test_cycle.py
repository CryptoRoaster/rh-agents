"""The scout cycle, end to end over a real database.

Every test drives `EarlyScoutCycle.execute()` — discovery, watch sync,
exact-locator refresh, ORBIT review and history check — and then reads what was
durably recorded. The model and the provider are fakes; everything between them
is production code.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from src.agents.orbit.models import OrbitClassification
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS
from src.data.tables import (
    DiscoveryWatchAssessmentRow,
    TradeCaseEvidenceRow,
    TradeCaseRow,
)
from src.markets.history import MarketHistoryUnavailable
from src.reasoning.models import ReasoningErrorCategory
from src.scout.policy import WatchStatus
from src.scout.repository import WatchRepository
from tests.scout.conftest import (
    HOUR,
    POOLS,
    EchoOrbit,
    MarketProvider,
    ScriptedHistory,
    pair_id,
    scout,
    scout_settings,
    trade_cases,
    young,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)


async def watch_for(sessions, index: int):
    return await WatchRepository(sessions).by_pair(pair_id(POOLS[index]))


async def assessments(sessions, watch):
    return await WatchRepository(sessions).assessments(watch.id)


# ----------------------------------------------------------------- discovery


async def test_a_new_pool_creates_a_watch(db):
    _, sessions = db
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0)]))
    watch = await watch_for(sessions, 0)
    assert watch is not None
    assert watch.status is WatchStatus.WATCHING
    assert watch.first_seen_at == T0
    assert watch.market.pool_locator is not None
    assert summary.discovered == 1
    assert summary.valid_markets == 1
    assert summary.watches_created == 1


async def test_repeated_discovery_reuses_the_watch(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)])
    await scout(sessions, T0, provider=provider)
    first = await watch_for(sessions, 0)
    summary = await scout(sessions, T0 + HOUR, provider=provider)
    again = await watch_for(sessions, 0)
    assert again.id == first.id
    assert summary.watches_created == 0
    assert summary.watches_updated == 1


async def test_first_seen_does_not_move_and_the_latest_snapshot_does(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)])
    await scout(sessions, T0, provider=provider)
    first = await watch_for(sessions, 0)
    await scout(sessions, T0 + 2 * HOUR, provider=provider)
    later = await watch_for(sessions, 0)
    assert later.first_seen_at == T0
    assert later.last_seen_at == T0 + 2 * HOUR
    assert later.latest_snapshot_id != first.latest_snapshot_id


async def test_a_provider_identity_rejection_creates_no_watch(db):
    _, sessions = db
    broken = young(1)
    broken["id"] = "robinhood_" + POOLS[2]  # resource id disagrees with the address
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0), broken]))
    assert await watch_for(sessions, 1) is None
    assert await watch_for(sessions, 2) is None
    assert summary.discovered == 2
    assert summary.valid_markets == 1
    assert summary.watches_created == 1


async def test_small_pools_are_watched_like_any_other(db):
    """No market-cap, liquidity or volume floor stands between discovery and a watch."""
    _, sessions = db
    tiny = young(0, liquidity="0.5", volume="0", price="0.0000000001")
    await scout(sessions, T0, provider=MarketProvider(discovery=[tiny]))
    assert (await watch_for(sessions, 0)).status is WatchStatus.WATCHING


async def test_the_new_watch_budget_bounds_creation(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0), young(1), young(2)])
    summary = await scout(
        sessions,
        T0,
        provider=provider,
        settings=scout_settings(early_scout_max_new_watches_per_run=2),
    )
    assert summary.watches_created == 2
    assert await watch_for(sessions, 2) is None


# ------------------------------------------------------------------ ORBIT


async def test_t0_orbit_runs_in_the_cycle_that_discovered_the_pool(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0)]), orbit=orbit)
    watch = await watch_for(sessions, 0)
    (assessment,) = await assessments(sessions, watch)
    assert len(orbit.calls) == 1
    assert orbit.calls[0].instructions == ORBIT_INSTRUCTIONS
    assert assessment.checkpoint_seconds == 0
    assert assessment.snapshot_id == watch.latest_snapshot_id
    assert watch.next_orbit_review_at == T0 + HOUR
    assert summary.orbit_reviews_started == 1
    assert summary.orbit_reviews_completed == 1


@pytest.mark.parametrize(
    "classification",
    [
        OrbitClassification.NOT_INTERESTING,
        OrbitClassification.INSUFFICIENT_DATA,
        OrbitClassification.INTERESTING,
    ],
)
async def test_no_classification_ends_a_watch(db, classification):
    _, sessions = db
    unpriced = young(0, price=None)  # a gap to name, so INSUFFICIENT_DATA is expressible
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[unpriced]),
        orbit=EchoOrbit(classification),
    )
    watch = await watch_for(sessions, 0)
    (assessment,) = await assessments(sessions, watch)
    assert assessment.classification == classification.value
    assert watch.status is WatchStatus.WATCHING
    assert watch.next_orbit_review_at == T0 + HOUR


async def test_every_checkpoint_is_reviewed_once_on_a_fresh_reading(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    orbit = EchoOrbit()
    await scout(sessions, T0, provider=provider, orbit=orbit)
    provider.discovery = []  # the pool has left the new-pool list
    for hours in (1, 3, 6, 12, 24):
        await scout(sessions, T0 + hours * HOUR, provider=provider, orbit=orbit)
    watch = await watch_for(sessions, 0)
    rows = await assessments(sessions, watch)
    assert [item.checkpoint_seconds for item in rows] == [0, 3600, 10800, 21600, 43200, 86400]
    assert len(orbit.calls) == 6
    assert watch.next_orbit_review_at is None
    # Every later review was taken on a reading re-observed by exact locator.
    assert len(provider.multi_requests) == 5
    assert len({item.snapshot_id for item in rows}) == 6


async def test_missed_checkpoints_do_not_burst_calls(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    orbit = EchoOrbit()
    await scout(sessions, T0, provider=provider, orbit=orbit)
    provider.discovery = []
    summary = await scout(sessions, T0 + 4 * HOUR, provider=provider, orbit=orbit)
    watch = await watch_for(sessions, 0)
    rows = await assessments(sessions, watch)
    assert len(orbit.calls) == 2
    assert summary.orbit_reviews_started == 1
    assert [item.checkpoint_seconds for item in rows] == [0, 10800]
    assert watch.next_orbit_review_at == T0 + 6 * HOUR


async def test_one_orbit_call_per_watch_per_cycle(db):
    _, sessions = db
    orbit = EchoOrbit()
    provider = MarketProvider(discovery=[young(0)])
    await scout(
        sessions,
        T0,
        provider=provider,
        orbit=orbit,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=10),
    )
    await scout(
        sessions,
        T0,  # the same instant: nothing is due twice
        provider=provider,
        orbit=orbit,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=10),
    )
    assert len(orbit.calls) == 1


async def test_the_review_budget_limits_calls(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0), young(1), young(2)]),
        orbit=orbit,
        settings=scout_settings(early_scout_max_orbit_reviews_per_run=1),
    )
    assert len(orbit.calls) == 1
    assert summary.watches_due_orbit == 3
    assert summary.orbit_reviews_started == 1
    reviewed = [await watch_for(sessions, index) for index in range(3)]
    assert [item.next_orbit_review_at for item in reviewed] == [T0 + HOUR, T0, T0]


async def test_due_reviews_ignore_liquidity_volume_and_classification(db):
    """Order is due time, then first seen, then pair — never size or opinion."""
    _, sessions = db
    provider = MarketProvider(
        discovery=[
            young(0, liquidity="10", volume="1"),
            young(1, liquidity="90000000", volume="50000000"),
            young(2, liquidity="50000000", volume="90000000"),
        ],
        targeted=[young(0), young(1), young(2)],
    )
    one = scout_settings(early_scout_max_orbit_reviews_per_run=1)
    orbit = EchoOrbit(OrbitClassification.INTERESTING)
    await scout(sessions, T0, provider=provider, orbit=orbit, settings=one)
    await scout(sessions, T0, provider=provider, orbit=orbit, settings=one)
    await scout(sessions, T0, provider=provider, orbit=orbit, settings=one)
    shown = [call.data["market_observation"]["pair_id"] for call in orbit.calls]
    assert shown == [pair_id(POOLS[0]), pair_id(POOLS[1]), pair_id(POOLS[2])]


async def test_a_provider_failure_is_recorded_typed_and_keeps_the_watch(db):
    _, sessions = db
    orbit = EchoOrbit(failure=ReasoningErrorCategory.PROVIDER_TIMEOUT)
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[young(0)]), orbit=orbit)
    watch = await watch_for(sessions, 0)
    (row,) = await assessments(sessions, watch)
    assert row.status == "FAILED"
    assert row.failure_reason == "PROVIDER_TIMEOUT"
    assert row.classification is None
    assert summary.model_failures == 1
    assert watch.status is WatchStatus.WATCHING
    # The checkpoint is spent: a failure never becomes a loop of paid retries.
    assert watch.next_orbit_review_at == T0 + HOUR


async def test_invalid_output_is_never_stored_as_a_valid_assessment(db):
    _, sessions = db
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=[young(0)]), orbit=EchoOrbit(lie=True)
    )
    watch = await watch_for(sessions, 0)
    (row,) = await assessments(sessions, watch)
    assert row.status == "FAILED"
    assert row.failure_reason == "MARKET_MISMATCH"
    assert row.classification is None and row.summary is None
    assert summary.model_failures == 1
    assert summary.orbit_reviews_completed == 0


async def test_assessments_are_append_only_history(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider, orbit=EchoOrbit())
    first = (await assessments(sessions, await watch_for(sessions, 0)))[0]
    provider.discovery = []
    await scout(sessions, T0 + HOUR, provider=provider, orbit=EchoOrbit())
    rows = await assessments(sessions, await watch_for(sessions, 0))
    assert rows[0] == first
    assert len(rows) == 2


# ---------------------------------------------------------------- history


async def mature(sessions, provider, *, bars, hours=24):
    """A watch discovered at T0, then looked at again at T0 + `hours`."""
    await scout(sessions, T0, provider=provider)
    provider.discovery = []
    history = ScriptedHistory(bars)
    summary = await scout(sessions, T0 + hours * HOUR, provider=provider, history=history)
    return summary, history


async def test_no_history_is_read_before_twenty_four_hours(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider)
    history = ScriptedHistory(48)
    await scout(sessions, T0 + 12 * HOUR, provider=provider, history=history)
    assert history.reads == []


async def test_too_short_history_at_twenty_four_hours_keeps_watching(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    summary, history = await mature(sessions, provider, bars=5)
    watch = await watch_for(sessions, 0)
    assert history.reads == [pair_id(POOLS[0])]
    assert watch.status is WatchStatus.WATCHING
    assert watch.latest_vector_sufficiency == "MARKET_HISTORY_TOO_SHORT"
    assert watch.next_history_review_at == T0 + 48 * HOUR
    assert summary.history_checks == 1
    assert summary.vector_sufficient == 0


async def test_sufficient_history_at_twenty_four_hours_becomes_promotable(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    summary, _ = await mature(sessions, provider, bars=30)
    watch = await watch_for(sessions, 0)
    assert watch.status is WatchStatus.PROMOTABLE
    assert watch.latest_vector_sufficiency == "SUFFICIENT"
    assert watch.vector_checked_at == T0 + 24 * HOUR
    assert watch.next_history_review_at is None
    assert summary.promotable_new == 1
    assert await trade_cases(sessions) == 0


async def test_an_early_not_interesting_watch_can_still_be_promoted(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(
        sessions, T0, provider=provider, orbit=EchoOrbit(OrbitClassification.NOT_INTERESTING)
    )
    provider.discovery = []
    await scout(sessions, T0 + 24 * HOUR, provider=provider, history=ScriptedHistory(30))
    assert (await watch_for(sessions, 0)).status is WatchStatus.PROMOTABLE


async def test_seventy_two_hours_without_history_makes_a_watch_dormant(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider)
    provider.discovery = []
    for hours in (24, 48, 72):
        summary = await scout(
            sessions, T0 + hours * HOUR, provider=provider, history=ScriptedHistory(3)
        )
    watch = await watch_for(sessions, 0)
    assert watch.status is WatchStatus.DORMANT
    assert watch.next_history_review_at is None
    assert watch.next_orbit_review_at is None
    assert summary.dormant_new == 1
    # Dormant is not deleted: its history stays readable.
    assert len(await assessments(sessions, watch)) >= 1


async def test_a_dormant_watch_causes_no_further_traffic(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider)
    provider.discovery = []
    for hours in (24, 48, 72):
        await scout(sessions, T0 + hours * HOUR, provider=provider, history=ScriptedHistory(3))
    orbit, history = EchoOrbit(), ScriptedHistory(48)
    before = len(provider.paths)
    await scout(sessions, T0 + 200 * HOUR, provider=provider, orbit=orbit, history=history)
    assert orbit.calls == [] and history.reads == []
    assert not [item for item in provider.paths[before:] if "/pools/multi/" in item]


async def test_a_history_provider_failure_leaves_the_check_due(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    failing = MarketHistoryUnavailable("MARKET_HISTORY_PROVIDER_UNAVAILABLE")
    await scout(sessions, T0, provider=provider)
    provider.discovery = []
    summary = await scout(
        sessions, T0 + 24 * HOUR, provider=provider, history=ScriptedHistory(failing)
    )
    watch = await watch_for(sessions, 0)
    assert watch.status is WatchStatus.WATCHING
    assert watch.next_history_review_at == T0 + 24 * HOUR
    assert watch.reason_code == "MARKET_HISTORY_PROVIDER_UNAVAILABLE"
    assert summary.provider_failures == 1


# -------------------------------------------------------------- boundaries


async def test_the_scout_never_opens_a_trade_case(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0), young(1)], targeted=[young(0), young(1)])
    await scout(sessions, T0, provider=provider, orbit=EchoOrbit(OrbitClassification.INTERESTING))
    provider.discovery = []
    await scout(sessions, T0 + 24 * HOUR, provider=provider, history=ScriptedHistory(40))
    await scout(sessions, T0 + 25 * HOUR, provider=provider, history=ScriptedHistory(40))
    assert await trade_cases(sessions) == 0


async def test_a_scout_assessment_is_never_case_evidence(db):
    _, sessions = db
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0)]),
        orbit=EchoOrbit(OrbitClassification.INTERESTING),
    )
    async with sessions() as session:
        evidence = await session.scalar(select(func.count()).select_from(TradeCaseEvidenceRow))
        stored = await session.scalar(select(func.count()).select_from(DiscoveryWatchAssessmentRow))
        cases = await session.scalar(select(func.count()).select_from(TradeCaseRow))
    assert (evidence, cases, stored) == (0, 0, 1)


async def test_a_disabled_scout_refuses_before_asking_anybody(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)])
    orbit = EchoOrbit()
    reading = await scout(
        sessions,
        T0,
        provider=provider,
        orbit=orbit,
        settings=scout_settings(early_scout_enabled=False),
    )
    assert reading.kind == "run_configuration_refused"
    assert reading.reason == "EARLY_SCOUT_NOT_ENABLED"
    assert provider.paths == [] and orbit.calls == []


async def test_a_system_stop_prevents_every_call(db):
    _, sessions = db
    from tests.riskrequest.conftest import set_account

    await set_account(sessions, paused=True)
    provider, orbit = MarketProvider(discovery=[young(0)]), EchoOrbit()
    summary = await scout(sessions, T0, provider=provider, orbit=orbit)
    assert summary.stop == "SYSTEM_STOPPED"
    assert provider.paths == [] and orbit.calls == []


# --------------------------------------------------------------- bootstrap


async def test_existing_streams_are_bootstrapped_once_and_bounded(db):
    _, sessions = db
    from src.markets.recorder import MarketRecorder
    from tests.riskdata.conftest import recorded_snapshot

    recorder = MarketRecorder(sessions)
    for index in range(3):
        pool_hex = f"{index + 1:02d}" * 20
        await recorder.record(
            recorded_snapshot(
                T0,
                age=HOUR * (3 - index),
                pair_id=f"robinhood:mainnet:contract_address:0x{pool_hex}",
                base_asset_id=f"robinhood:mainnet:0x{'9' + str(index)}{'aa' * 19}",
                label=f"legacy-{index}",
            )
        )
    bounded = scout_settings(early_scout_max_bootstrap_streams=2)
    orbit = EchoOrbit()
    first = await scout(sessions, T0, provider=MarketProvider(), orbit=orbit, settings=bounded)
    second = await scout(sessions, T0, provider=MarketProvider(), orbit=orbit, settings=bounded)
    third = await scout(sessions, T0, provider=MarketProvider(), orbit=orbit, settings=bounded)
    assert (first.bootstrapped, second.bootstrapped, third.bootstrapped) == (2, 1, 0)
    oldest = await WatchRepository(sessions).by_pair(
        "robinhood:mainnet:contract_address:0x" + "01" * 20
    )
    # First seen is the stream's own oldest observation, never the bootstrap time.
    assert oldest.first_seen_at == T0 - 3 * HOUR


async def test_an_unrefreshable_watch_does_not_starve_the_review_budget(db):
    """A stale watch nobody can re-observe must not hold the only review slot forever."""
    _, sessions = db
    from src.markets.recorder import MarketRecorder
    from tests.riskdata.conftest import recorded_snapshot

    # Adopted from before pool locators existed, hours stale: due first, never reviewable.
    await MarketRecorder(sessions).record(
        recorded_snapshot(
            T0,
            age=5 * HOUR,
            pair_id="robinhood:mainnet:contract_address:0x" + "0a" * 20,
            base_asset_id="robinhood:mainnet:0x" + "9b" * 20,
            label="legacy-unaddressable",
        )
    )
    orbit = EchoOrbit()
    one = scout_settings(early_scout_max_orbit_reviews_per_run=1)
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=[young(0)]), orbit=orbit, settings=one
    )
    assert summary.bootstrapped == 1
    assert len(orbit.calls) == 1
    assert orbit.calls[0].data["market_observation"]["pair_id"] == pair_id(POOLS[0])
    legacy = await WatchRepository(sessions).by_pair(
        "robinhood:mainnet:contract_address:0x" + "0a" * 20
    )
    assert legacy.reason_code == "POOL_LOCATOR_UNKNOWN"
    assert legacy.next_orbit_review_at == legacy.first_seen_at  # still due, nothing spent


async def test_discovery_coverage_is_countable_from_the_summary(db):
    """Raw, valid, identity rejects and other rejects add up; watches follow valid only."""
    _, sessions = db
    identity_broken = young(1)
    identity_broken["id"] = "robinhood_" + POOLS[2]
    shape_broken = young(2)
    del shape_broken["attributes"]["address"]
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0), identity_broken, shape_broken]),
    )
    assert summary.discovered == 3
    assert summary.valid_markets == 1
    assert summary.provider_identity_rejects == 1
    assert summary.other_provider_rejects == 1
    assert (
        summary.valid_markets + summary.provider_identity_rejects + summary.other_provider_rejects
        == summary.discovered
    )
    assert summary.watches_created == 1


async def test_a_native_quoted_pool_becomes_a_watch(db):
    """The reproduced false rejection, end to end: discovered, normalized, watched."""
    _, sessions = db
    from tests.runner.provider import NETWORK_ID

    native = "0x" + "0" * 40
    meme = young(0)
    meme["relationships"]["quote_token"]["data"]["id"] = f"{NETWORK_ID}_{native}"
    summary = await scout(sessions, T0, provider=MarketProvider(discovery=[meme]))
    watch = await watch_for(sessions, 0)
    assert summary.provider_identity_rejects == 0
    assert summary.valid_markets == 1
    assert watch is not None
    assert watch.market.quote_asset_id == f"robinhood:mainnet:{native}"
