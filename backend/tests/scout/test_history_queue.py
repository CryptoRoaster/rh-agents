"""The VECTOR history fair queue: own paced transport, two lanes, bounded retry.

History checks no longer ride on ORBIT's selection and refresh: they need the
stored identity and a due checkpoint, not a fresh reading. Reads go through a
transport of their own (six requests, six seconds apart), five CURRENT to one
CATCH-UP, and a failed read backs off instead of holding the head of the queue.
"""

from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from src.data.repository import aware
from src.data.tables import DiscoveryWatchRow
from src.markets.history import MarketHistoryUnavailable
from src.scout.history_queue import (
    CURRENT_WINDOW,
    RATE_LIMITED,
    backoff_after,
    interleave,
    pick,
)
from src.scout.policy import EARLY_SCOUT_V2, OrbitState, WatchStatus
from src.scout.repository import WatchRepository
from tests.scout.conftest import (
    HOUR,
    EchoOrbit,
    MarketProvider,
    RecordingSleep,
    ScriptedHistory,
    scout,
    scout_settings,
    young,
)
from tests.scout.test_fresh_first import T0

MINUTE = timedelta(minutes=1)
DUE = T0 + 30 * HOUR  # when the checks below run


class OhlcvRejected(MarketProvider):
    """The scripted provider, answering every OHLCV read with a 404."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if "/ohlcv/" in request.url.path:
            self.requests.append(request)
            self.paths.append(request.url.path)
            return httpx.Response(404, text='{"errors":[{"status":"404"}]}')
        return super().handle(request)

    @property
    def ohlcv_requests(self) -> list[str]:
        return [item for item in self.paths if "/ohlcv/" in item]


class Failing(ScriptedHistory):
    """Scripted series, except for pairs told to fail with a given code."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        super().__init__(bars=0)
        self.failures = failures or {}

    async def history(self, identity, **kwargs):
        if identity.pair_id in self.failures:
            self.reads.append(identity.pair_id)
            raise MarketHistoryUnavailable(self.failures[identity.pair_id])
        return await super().history(identity, **kwargs)


async def template(sessions):
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0)]),
        orbit=EchoOrbit(),
        policy=EARLY_SCOUT_V2,
    )
    async with sessions() as session:
        return await session.scalar(select(DiscoveryWatchRow))


def identity(payload, chain, address):
    """The template's market identity, moved to its own pool on `chain`."""
    return {
        **payload,
        "chain": chain,
        "pair_id": f"{chain}:mainnet:contract_address:{address}",
        "pool_locator": {**payload["pool_locator"], "value": address},
        "base_asset_id": f"{chain}:mainnet:0x{'ab' * 20}",
        "quote_asset_id": f"{chain}:mainnet:0x{'cd' * 20}",
    }


async def seed(sessions, specs):
    """Watches by (label, chain, overdue): due for history `overdue` before DUE."""
    row = await template(sessions)
    ids = {}
    async with sessions.begin() as session:
        await session.execute(update(DiscoveryWatchRow).values(next_history_review_at=None))
        for index, (label, chain, overdue) in enumerate(specs):
            first_seen = DUE - overdue - 24 * HOUR
            address = f"0x{index + 1:040x}"
            pid = f"{chain}:mainnet:contract_address:{address}"
            session.add(
                DiscoveryWatchRow(
                    **{
                        **{
                            c.name: getattr(row, c.name)
                            for c in DiscoveryWatchRow.__table__.columns
                        },
                        "id": uuid4(),
                        "chain": chain,
                        "pair_id": pid,
                        "market_payload": identity(row.market_payload, chain, address),
                        "first_seen_at": first_seen,
                        "last_seen_at": first_seen,
                        "next_history_review_at": DUE - overdue,
                        "next_orbit_review_at": None,
                    }
                )
            )
            ids[label] = pid
    return ids


async def run(sessions, at, *, history=None, provider=None, slots=6, sleep=None):
    return await scout(
        sessions,
        at,
        provider=provider if provider is not None else MarketProvider(),
        orbit=EchoOrbit(),
        history=history if history is not None else Failing(),
        settings=scout_settings(early_scout_history_max_requests_per_run=slots),
        policy=EARLY_SCOUT_V2,
        sleep=sleep,
    )


async def row(sessions, pid):
    async with sessions() as session:
        return await session.scalar(
            select(DiscoveryWatchRow).where(DiscoveryWatchRow.pair_id == pid)
        )


# ------------------------------------------------------- pure queue rules


def test_six_slots_split_five_to_one_and_lend_when_a_lane_is_short():
    current, catch_up = list("abcdefgh"), list("XYZ")
    assert [w for w, _ in pick(current, catch_up, 6)] == list("abcdeX")
    assert [w for w, _ in pick(current[:2], catch_up, 6)] == list("abXYZ")
    assert [w for w, _ in pick(current, [], 6)] == list("abcdef")
    assert [w for w, _ in pick([], catch_up, 6)] == list("XYZ")
    assert pick(current, catch_up, 0) == []


def test_backoff_is_bounded():
    assert [backoff_after(n) for n in range(1, 7)] == [
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(minutes=60),
        timedelta(minutes=120),
        timedelta(minutes=120),
        timedelta(minutes=120),
    ]


def test_chains_are_interleaved_fairly():
    merged = interleave({"robinhood": ["r1", "r2", "r3"], "bsc": ["b1", "b2"]})
    assert merged == ["b1", "r1", "b2", "r2", "r3"]


# ------------------------------------------------------- lanes in the database


async def test_lanes_are_due_time_ordered_per_chain_and_blind_to_markets(db):
    _, sessions = db
    ids = await seed(
        sessions,
        [
            ("r_new", "robinhood", 5 * MINUTE),
            ("r_mid", "robinhood", 20 * MINUTE),
            ("b_new", "bsc", 10 * MINUTE),
            ("r_old", "robinhood", 10 * HOUR),
            ("b_old", "bsc", 20 * HOUR),
        ],
    )
    # Market figures on the rows must not move anything.
    async with sessions.begin() as session:
        await session.execute(
            update(DiscoveryWatchRow)
            .where(DiscoveryWatchRow.pair_id == ids["r_mid"])
            .values(market_payload=DiscoveryWatchRow.market_payload, reason_code="X")
        )
    current, catch_up = await WatchRepository(sessions).history_lanes(DUE, CURRENT_WINDOW, 6)
    assert [w.pair_id for w in current["robinhood"]] == [ids["r_new"], ids["r_mid"]]
    assert [w.pair_id for w in current["bsc"]] == [ids["b_new"]]
    assert [w.pair_id for w in catch_up["robinhood"]] == [ids["r_old"]]
    assert [w.pair_id for w in catch_up["bsc"]] == [ids["b_old"]]
    assert [w.chain for w in interleave(current)] == ["bsc", "robinhood", "robinhood"]


# ------------------------------------------------------- the run


async def test_five_current_and_one_catch_up_are_taken(db):
    _, sessions = db
    await seed(
        sessions,
        [(f"c{i}", "robinhood", i * MINUTE) for i in range(8)]
        + [(f"o{i}", "robinhood", (10 + i) * HOUR) for i in range(3)],
    )
    summary = await run(sessions, DUE)
    assert (summary.history_current_selected, summary.history_catchup_selected) == (5, 1)
    assert summary.history_checks == 6
    assert summary.history_eligible_now == 11
    assert summary.oldest_history_due_age_seconds == 12 * 3600


async def test_new_checks_do_not_wait_behind_a_large_backlog(db):
    _, sessions = db
    ids = await seed(
        sessions,
        [(f"o{i}", "robinhood", (5 + i) * HOUR) for i in range(40)]
        + [(f"n{i}", "robinhood", i * MINUTE) for i in range(3)],
    )
    history = Failing()
    await run(sessions, DUE, history=history)
    assert {ids[f"n{i}"] for i in range(3)} <= set(history.reads)


async def test_the_backlog_is_worked_every_run_oldest_first(db):
    _, sessions = db
    ids = await seed(
        sessions,
        [(f"o{i}", "robinhood", (5 + i) * HOUR) for i in range(3)]
        + [(f"n{i}", "robinhood", i * MINUTE) for i in range(20)],
    )
    history = Failing()
    for step in range(3):
        summary = await run(sessions, DUE + step * 15 * MINUTE, history=history)
        assert summary.history_catchup_selected == 1
    assert [pid for pid in history.reads if pid in {ids["o0"], ids["o1"], ids["o2"]}] == [
        ids["o2"],
        ids["o1"],
        ids["o0"],
    ]


async def test_a_rate_limited_head_backs_off_and_the_queue_moves_on(db):
    _, sessions = db
    ids = await seed(
        sessions,
        [("head", "robinhood", 20 * HOUR), ("next", "robinhood", 10 * HOUR)],
    )
    history = Failing({ids["head"]: RATE_LIMITED})
    first = await run(sessions, DUE, history=history, slots=1)
    assert (first.history_rate_limited, first.history_backoff_set, first.history_checks) == (
        1,
        1,
        0,
    )
    head = await row(sessions, ids["head"])
    assert aware(head.history_retry_not_before) == DUE + 15 * MINUTE
    assert (head.history_failure_count, head.history_last_failure) == (1, RATE_LIMITED)
    # The checkpoint itself is untouched.
    assert aware(head.next_history_review_at) == DUE - 20 * HOUR

    second = await run(sessions, DUE + 5 * MINUTE, history=history, slots=1)
    assert history.reads[-1] == ids["next"]
    assert second.history_checks == 1

    # Backoff over: the head is eligible again and, still failing, backs off longer.
    await run(sessions, DUE + 16 * MINUTE, history=history, slots=1)
    assert history.reads[-1] == ids["head"]
    head = await row(sessions, ids["head"])
    assert aware(head.history_retry_not_before) == DUE + 16 * MINUTE + 30 * MINUTE


async def test_a_successful_check_clears_the_retry_state(db):
    _, sessions = db
    ids = await seed(sessions, [("w", "robinhood", 2 * HOUR)])
    await run(sessions, DUE, history=Failing({ids["w"]: "MARKET_HISTORY_PROVIDER_UNAVAILABLE"}))
    assert (await row(sessions, ids["w"])).history_failure_count == 1
    await run(sessions, DUE + 20 * MINUTE, history=Failing())
    watch = await row(sessions, ids["w"])
    assert (watch.history_failure_count, watch.history_retry_not_before) == (0, None)
    assert watch.history_last_failure is None
    assert watch.vector_checked_at is not None


async def test_the_failure_count_is_bounded(db):
    _, sessions = db
    ids = await seed(sessions, [("w", "robinhood", 2 * HOUR)])
    repository = WatchRepository(sessions)
    watch = await repository.by_pair(ids["w"])
    pauses = []
    for step in range(12):
        pauses.append(
            await repository.history_failed(
                watch.id,
                expected_next=watch.next_history_review_at,
                failure="X",
                now=DUE + step * MINUTE,
                backoff=backoff_after,
                max_failures=10,
            )
        )
    assert pauses[:5] == [backoff_after(n) for n in range(1, 6)]
    assert max(pauses) == timedelta(minutes=120)
    assert (await row(sessions, ids["w"])).history_failure_count == 10


# ------------------------------------------------------- decoupled and paced


async def test_an_old_reading_needs_no_refresh_and_no_orbit_budget(db):
    _, sessions = db
    await seed(sessions, [(f"w{i}", "robinhood", i * MINUTE) for i in range(3)])
    provider = MarketProvider()
    summary = await run(sessions, DUE, provider=provider)
    # Readings are 30h old; nothing re-observed them, yet all three were checked.
    assert summary.history_checks == 3
    assert summary.refreshed == 0
    assert provider.multi_requests == []


async def test_history_reads_use_their_own_budget_six_seconds_apart(db):
    _, sessions = db
    await seed(sessions, [(f"w{i}", "robinhood", i * MINUTE) for i in range(9)])
    provider = OhlcvRejected()
    sleep = RecordingSleep()
    summary = await scout(
        sessions,
        DUE,
        provider=provider,
        orbit=EchoOrbit(),
        history=False,
        settings=scout_settings(early_scout_history_max_requests_per_run=6),
        policy=EARLY_SCOUT_V2,
        sleep=sleep,
    )
    assert len(provider.ohlcv_requests) == summary.history_provider_requests == 6
    assert sleep.pauses == [6.0] * 5
    # Discovery kept its own budget: one directory read and one discovery read.
    others = [path for path in provider.paths if "/ohlcv/" not in path]
    assert len(others) == 2 and summary.provider_requests == 2
    # A rejected read backs off each watch rather than holding the queue.
    assert summary.history_backoff_set == 6


# ------------------------------------------------------- promotion unchanged


async def test_a_stale_skipped_watch_still_becomes_promotable(db):
    _, sessions = db
    ids = await seed(sessions, [("w", "robinhood", 10 * MINUTE)])
    async with sessions.begin() as session:
        await session.execute(
            update(DiscoveryWatchRow).values(
                orbit_state=OrbitState.FIRST_REVIEW_SKIPPED_STALE.value, orbit_state_at=T0
            )
        )
    summary = await run(sessions, DUE, history=ScriptedHistory(bars=48))
    assert summary.promotable_new == 1
    assert (await row(sessions, ids["w"])).status == WatchStatus.PROMOTABLE.value
