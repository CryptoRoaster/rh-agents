"""Which watches the early intake may read, and that the scout still opens nothing.

The source is COMMANDER's candidate port and adds exactly the strategy's own
restrictions: WATCHING (never PROMOTABLE), Robinhood, not a fixture, first seen
inside the age window, no live case, no barring case, never an early case
before. Nothing ranks or filters by market size, volume or quote asset.
"""

from datetime import timedelta
from uuid import uuid4

from src.core.clock import FixedClock
from src.data.tables import TradeCaseRow
from src.markets.reader import MarketReader
from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1
from src.orchestration.workflow.models import TradeCaseStatus
from src.scout.candidates import EarlyWatchCandidates, early_case_exists
from src.scout.repository import WatchRepository
from tests.riskdata.conftest import recorded_snapshot
from tests.scout.test_promotion import NOW, case_on, market, promotable, record


async def watching(sessions, index, *, first_seen=NOW - timedelta(minutes=30), **kw):
    repository = WatchRepository(sessions)
    first = await record(sessions, index, at=first_seen, age=timedelta(0))
    await repository.sync(first, now=first_seen, allow_create=True)
    await record(sessions, index, **kw)
    return await repository.by_pair(first.pair.pair_id)


def source(sessions, *, now=NOW):
    clock = FixedClock(now)
    return EarlyWatchCandidates(
        sessions=sessions,
        markets=MarketReader(sessions, clock=clock),
        watches=WatchRepository(sessions),
        clock=clock,
    )


async def eligible_pairs(sessions, **kw):
    return [item.pair_id for item in await source(sessions).eligible(**kw)]


async def early_case_on(sessions, index, status=TradeCaseStatus.EXPIRED):
    pair, base = market(index)
    snapshot = recorded_snapshot(NOW, pair_id=pair, base_asset_id=base, label=f"early-{index}")
    async with sessions.begin() as session:
        session.add(
            TradeCaseRow(
                id=uuid4(),
                workflow_version="trade-case-early-v1",
                market_key=pair,
                chain="robinhood",
                network="mainnet",
                status=status.value,
                opened_at=NOW - timedelta(hours=2),
                updated_at=NOW - timedelta(hours=2),
                expires_at=NOW - timedelta(hours=1),
                originating_discovery_reference=uuid4(),
                strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1,
                revision=1,
                reason_code=status.value,
                blockers=[],
                risk_input_digest=None,
                correlation_id=uuid4(),
                open_idempotency_key=f"seeded:early:{pair}",
                open_fingerprint="e" * 64,
                market_payload=snapshot.pair.market_identity.model_dump(mode="json"),
            )
        )


async def test_a_young_watching_watch_is_an_early_candidate(risk_db):
    _, sessions = risk_db
    watch = await watching(sessions, 0)
    assert await eligible_pairs(sessions) == [watch.pair_id]
    candidates = await source(sessions).candidates()
    assert [item.pair_id for item in candidates] == [watch.pair_id]


async def test_a_promotable_watch_belongs_to_the_normal_path(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0, first_seen=NOW - timedelta(hours=1))
    assert await eligible_pairs(sessions) == []


async def test_a_watch_first_seen_outside_the_age_window_is_not_scanned(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0, first_seen=NOW - timedelta(hours=6, seconds=1))
    assert await eligible_pairs(sessions) == []


async def test_a_watch_with_a_live_case_is_skipped(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0)
    await case_on(sessions, 0, TradeCaseStatus.EVIDENCE_PENDING)
    assert await eligible_pairs(sessions) == []


async def test_an_executed_or_rejected_market_is_skipped(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0)
    await watching(sessions, 1)
    await case_on(sessions, 0, TradeCaseStatus.EXECUTED)
    await case_on(sessions, 1, TradeCaseStatus.RISK_REJECTED)
    assert await eligible_pairs(sessions) == []


async def test_a_market_that_already_had_an_early_case_is_never_early_again(risk_db):
    _, sessions = risk_db
    watch = await watching(sessions, 0)
    await early_case_on(sessions, 0, TradeCaseStatus.EXPIRED)
    async with sessions() as session:
        assert await early_case_exists(session, watch.pair_id)
    assert await eligible_pairs(sessions) == []


async def test_a_normal_expired_case_does_not_bar_an_early_attempt(risk_db):
    _, sessions = risk_db
    watch = await watching(sessions, 0)
    await case_on(sessions, 0, TradeCaseStatus.EXPIRED)
    assert await eligible_pairs(sessions) == [watch.pair_id]


async def test_a_watch_without_a_fresh_reading_yields_no_candidate(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0, age=timedelta(minutes=10))
    assert len(await eligible_pairs(sessions)) == 1
    assert await source(sessions).candidates() == ()


async def test_only_robinhood_is_read(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0)
    other = EarlyWatchCandidates(
        sessions=sessions,
        markets=MarketReader(sessions, clock=FixedClock(NOW)),
        watches=WatchRepository(sessions),
        clock=FixedClock(NOW),
        chain="bsc",
    )
    assert await other.eligible() == ()
    assert EarlyWatchCandidates.__dataclass_fields__["chain"].default == "robinhood"


async def test_candidates_are_youngest_first_and_never_ranked_by_size(risk_db):
    _, sessions = risk_db
    older = await watching(sessions, 0, first_seen=NOW - timedelta(hours=2))
    younger = await watching(sessions, 1, first_seen=NOW - timedelta(minutes=5))
    assert await eligible_pairs(sessions) == [younger.pair_id, older.pair_id]


async def test_a_fixture_watch_is_never_an_early_candidate(risk_db):
    _, sessions = risk_db
    watch = await watching(sessions, 0)
    fixture = watch.model_copy(update={"is_fixture": True})

    class FixtureWatches:
        async def young_watching(self, limit, *, seen_since, chain):
            return (fixture,)

    candidates = EarlyWatchCandidates(
        sessions=sessions,
        markets=MarketReader(sessions, clock=FixedClock(NOW)),
        watches=FixtureWatches(),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
    )
    assert await candidates.eligible() == ()
