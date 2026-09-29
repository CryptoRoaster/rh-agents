"""EARLY_SCOUT_V2: ORBIT spends its fixed budget on fresh first reviews only.

One review per watch, only within an hour of discovery, chosen by a
signal-blind hash of the market identity, from slots released evenly across the
UTC day. Everything the policy will not serve is closed deterministically and
without a model call; the watch, its history schedule, JEV and outcomes go on.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from src.data.repository import aware
from src.data.tables import (
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchRow,
    ScoutOrbitReservationRow,
)
from src.scout.budget import OrbitBudget, SlotPacing
from src.scout.policy import EARLY_SCOUT_V1, EARLY_SCOUT_V2, OrbitState, WatchStatus
from src.scout.repository import WatchRepository, selection_key
from tests.scout.conftest import (
    HOUR,
    POOLS,
    EchoOrbit,
    MarketProvider,
    pair_id,
    scout,
    young,
)

# Noon UTC: bucket 48 of 96, so 49 slots are released and two fit one bucket.
T0 = datetime(2026, 9, 26, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


async def v2(sessions, now, provider, orbit):
    return await scout(sessions, now, provider=provider, orbit=orbit, policy=EARLY_SCOUT_V2)


async def rows(sessions):
    async with sessions() as session:
        return {row.pair_id: row for row in (await session.scalars(select(DiscoveryWatchRow)))}


async def assessment_count(sessions) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(select(func.count()).select_from(DiscoveryWatchAssessmentRow))
        )


async def clone_watches(
    sessions, template, count, *, first_seen, chain="robinhood", reviewed=False
):
    """Many watches shaped like `template`, without discovering each one."""
    async with sessions.begin() as session:
        for index in range(count):
            session.add(
                DiscoveryWatchRow(
                    **{
                        **{
                            column.name: getattr(template, column.name)
                            for column in DiscoveryWatchRow.__table__.columns
                        },
                        "id": uuid4(),
                        "chain": chain,
                        "pair_id": f"{chain}:mainnet:contract_address:0x{index:040x}",
                        "first_seen_at": first_seen,
                        "last_seen_at": first_seen,
                        "next_orbit_review_at": first_seen + (HOUR if reviewed else timedelta(0)),
                        "orbit_checkpoint_index": 0 if reviewed else None,
                        "orbit_state": None,
                        "orbit_state_at": None,
                    }
                )
            )


# ------------------------------------------------------- one fresh review


async def test_a_fresh_watch_gets_exactly_one_review_and_no_follow_up(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(0)])
    orbit = EchoOrbit()
    summary = await v2(sessions, T0, provider, orbit)
    assert (summary.orbit_reviews_started, len(orbit.calls)) == (1, 1)
    row = (await rows(sessions))[pair_id(POOLS[0])]
    assert (row.orbit_state, row.next_orbit_review_at) == (OrbitState.REVIEWED.value, None)
    # The V1 follow-up instants pass: nothing is scheduled, nothing is called.
    for hours in (1, 3, 6, 12, 24):
        await v2(sessions, T0 + hours * HOUR, provider, orbit)
    assert len(orbit.calls) == 1
    assert await assessment_count(sessions) == 1


async def test_pacing_bounds_one_run_to_its_bucket(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await v2(sessions, T0, MarketProvider(discovery=[young(i) for i in range(8)]), orbit)
    assert summary.orbit_fresh_first_reviews_due == 8
    assert summary.orbit_slots_released == 49
    # The per-run cap is five; the bucket admits two.
    assert len(orbit.calls) == summary.orbit_reviews_started == 2


async def test_the_chosen_watches_are_the_smallest_identity_hashes(db):
    """Signal-blind: tiny and large pools alike, by identity alone."""
    _, sessions = db
    pools = [young(i, liquidity=str(10 ** (i % 5)), price=f"0.{i + 1}") for i in range(8)]
    await v2(sessions, T0, MarketProvider(discovery=pools), EchoOrbit())
    found = await rows(sessions)
    watches = [await WatchRepository(sessions).by_pair(pid) for pid in found]
    ordered = sorted(watches, key=lambda watch: selection_key("early-scout-v2", watch))
    reviewed = {pid for pid, row in found.items() if row.orbit_state == OrbitState.REVIEWED.value}
    assert reviewed == {watch.pair_id for watch in ordered[:2]}
    # What is still due comes back in exactly that order, and includes the tiny pools.
    due = await WatchRepository(sessions, policy=EARLY_SCOUT_V2).due_for_orbit(T0, 50)
    assert [watch.pair_id for watch in due] == [watch.pair_id for watch in ordered[2:]]


async def test_market_figures_cannot_move_the_selection_key(db):
    _, sessions = db
    await v2(sessions, T0, MarketProvider(discovery=[young(0)]), EchoOrbit())
    watch = await WatchRepository(sessions).by_pair(pair_id(POOLS[0]))
    moved = watch.model_copy(
        update={"last_seen_at": T0 + HOUR, "latest_snapshot_id": uuid4(), "reason_code": "X"}
    )
    assert selection_key("early-scout-v2", moved) == selection_key("early-scout-v2", watch)
    # And the key is reproducible: the same identity, the same hash, every time.
    assert selection_key("early-scout-v2", watch) == selection_key("early-scout-v2", watch)


async def test_both_chains_are_selectable(db):
    _, sessions = db
    await v2(sessions, T0 - 2 * HOUR, MarketProvider(discovery=[young(0)]), EchoOrbit())
    template = (await rows(sessions))[pair_id(POOLS[0])]
    await clone_watches(sessions, template, 20, first_seen=T0, chain="robinhood")
    await clone_watches(sessions, template, 20, first_seen=T0, chain="bsc")
    due = await WatchRepository(sessions, policy=EARLY_SCOUT_V2).due_for_orbit(T0, 40)
    assert {watch.chain for watch in due} == {"robinhood", "bsc"}
    assert {watch.chain for watch in due[:10]} == {"robinhood", "bsc"}


# ------------------------------------------------------- stale, deferred, bulk


async def test_no_review_within_the_hour_is_skipped_without_a_call(db):
    _, sessions = db
    provider = MarketProvider(discovery=[young(i) for i in range(8)])
    orbit = EchoOrbit()
    await v2(sessions, T0, provider, orbit)
    calls = len(orbit.calls)
    before = {pid: row.next_history_review_at for pid, row in (await rows(sessions)).items()}

    summary = await v2(sessions, T0 + 61 * MINUTE, MarketProvider(discovery=[]), orbit)

    assert summary.orbit_first_reviews_skipped_stale == 6
    assert summary.orbit_fresh_first_reviews_due == 0
    assert len(orbit.calls) == calls
    after = await rows(sessions)
    skipped = [
        row for row in after.values() if row.orbit_state == "ORBIT_FIRST_REVIEW_SKIPPED_STALE"
    ]
    assert len(skipped) == 6
    for row in skipped:
        # Closed for ORBIT only: still watched, history still scheduled.
        assert row.status == WatchStatus.WATCHING.value
        assert row.next_orbit_review_at is None
        assert aware(row.next_history_review_at) == aware(before[row.pair_id])
        assert aware(row.orbit_state_at) == T0 + 61 * MINUTE

    again = await v2(sessions, T0 + 75 * MINUTE, MarketProvider(discovery=[]), orbit)
    assert (again.orbit_first_reviews_skipped_stale, again.orbit_follow_ups_deferred) == (0, 0)


async def test_v1_debt_is_closed_without_calls(db):
    """Unreviewed V1 watches are skipped; reviewed ones have their follow-ups deferred."""
    _, sessions = db
    old = T0 - 2 * 24 * HOUR
    provider = MarketProvider(discovery=[young(i) for i in range(8)])
    await scout(sessions, old, provider=provider, orbit=EchoOrbit(), policy=EARLY_SCOUT_V1)
    legacy = await assessment_count(sessions)
    assert legacy == 5  # V1's per-run cap, with follow-ups still scheduled

    orbit = EchoOrbit()
    summary = await v2(sessions, T0, MarketProvider(discovery=[]), orbit)

    assert (summary.orbit_first_reviews_skipped_stale, summary.orbit_follow_ups_deferred) == (3, 5)
    assert orbit.calls == []
    assert await assessment_count(sessions) == legacy  # history untouched
    backlog = await WatchRepository(sessions, policy=EARLY_SCOUT_V2).backlog(T0)
    assert (backlog.due, backlog.unreviewed) == (0, 0)
    assert (backlog.skipped_stale, backlog.follow_ups_deferred) == (3, 5)


async def test_a_large_backlog_is_closed_in_bulk(db):
    _, sessions = db
    await v2(sessions, T0 - 3 * HOUR, MarketProvider(discovery=[young(0)]), EchoOrbit())
    template = (await rows(sessions))[pair_id(POOLS[0])]
    await clone_watches(sessions, template, 1710, first_seen=T0 - 30 * HOUR)
    await clone_watches(
        sessions, template, 141, first_seen=T0 - 30 * HOUR, chain="bsc", reviewed=True
    )
    repository = WatchRepository(sessions, policy=EARLY_SCOUT_V2)

    settled = await repository.settle_orbit_debts(T0)

    assert (settled.skipped_stale, settled.follow_ups_deferred) == (1710, 141)
    assert (await repository.count_due(T0))[0] == 0
    assert await repository.settle_orbit_debts(T0 + MINUTE) == type(settled)()


# ------------------------------------------------------- pacing


async def watch_ids(sessions, count):
    """Fresh watches with no reservation yet; the template is reviewed a day earlier."""
    await v2(sessions, T0 - 24 * HOUR, MarketProvider(discovery=[young(0)]), EchoOrbit())
    template = (await rows(sessions))[pair_id(POOLS[0])]
    await clone_watches(sessions, template, count, first_seen=T0)
    async with sessions() as session:
        return [
            row.id
            for row in await session.scalars(
                select(DiscoveryWatchRow)
                .where(DiscoveryWatchRow.id != template.id)
                .order_by(DiscoveryWatchRow.pair_id)
            )
        ]


async def simulate_day(budget, ids, *, start, runs, every=timedelta(minutes=15), per_run=4):
    taken = 0
    pending = iter(ids)
    for step in range(runs):
        now = start + step * every
        for _ in range(per_run):
            if await budget.reserve(next(pending), 0, now, 96, SlotPacing()) is None:
                break
            taken += 1
    return taken


async def test_the_day_cannot_be_spent_by_nine_cest(db):
    _, sessions = db
    ids = await watch_ids(sessions, 400)
    budget = OrbitBudget(sessions)
    midnight = T0.replace(hour=0)
    # Every run from 00:00 to 07:00 UTC (09:00 CEST), four attempts each.
    taken = await simulate_day(budget, ids, start=midnight, runs=29)
    assert taken == 29
    assert await budget.used(midnight.date()) == 29


async def test_a_full_day_never_exceeds_ninety_six(db):
    _, sessions = db
    ids = await watch_ids(sessions, 500)
    budget = OrbitBudget(sessions)
    midnight = T0.replace(hour=0)
    taken = await simulate_day(budget, ids, start=midnight, runs=96)
    assert taken == 96
    late = midnight + timedelta(hours=23, minutes=59)
    assert await budget.reserve(ids[-1], 0, late, 96, SlotPacing()) is None
    async with sessions() as session:
        per_day = await session.scalar(
            select(func.count())
            .select_from(ScoutOrbitReservationRow)
            .where(ScoutOrbitReservationRow.utc_day == midnight.date())
        )
    assert per_day == 96


async def test_missed_runs_do_not_burst(db):
    _, sessions = db
    ids = await watch_ids(sessions, 50)
    budget = OrbitBudget(sessions)
    # Nothing ran all morning: at noon 49 slots are released, two may be taken.
    assert await budget.available(T0, 96, SlotPacing()) == 2
    taken = await simulate_day(budget, ids, start=T0, runs=1, per_run=10)
    assert taken == 2


async def test_a_crashed_reservation_is_reused_not_double_counted(db):
    _, sessions = db
    ids = await watch_ids(sessions, 5)
    budget = OrbitBudget(sessions)
    first = await budget.reserve(ids[0], 0, T0, 96, SlotPacing())
    second = await budget.reserve(ids[1], 0, T0, 96, SlotPacing())
    assert first is not None and second is not None
    # The bucket is full, yet a still-RESERVED slot of a crashed run is returned.
    assert await budget.reserve(ids[0], 0, T0 + MINUTE, 96, SlotPacing()) == first
    assert await budget.reserve(ids[2], 0, T0 + MINUTE, 96, SlotPacing()) is None
    assert await budget.used(T0.date()) == 2
