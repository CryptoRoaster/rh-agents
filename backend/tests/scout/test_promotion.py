"""From PROMOTABLE watch to a full TradeCase, through the unchanged COMMANDER.

With `EARLY_SCOUT_ENABLED` the full PAPER run no longer opens cases on whatever
new pool was recorded last: its candidates are the fresh markets behind
PROMOTABLE watches. COMMANDER still decides whether a case may be opened — one
active case per market, generations, and the RISK_REJECTED / EXECUTED bars are
all exactly as they were. The candidate source only makes sure a market COMMANDER
would refuse cannot take the one slot a small `max_candidates` leaves.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.data.tables import TradeCaseEvidenceRow, TradeCaseRow
from src.markets.recorder import MarketRecorder
from src.orchestration.workflow.models import TradeCaseStatus
from src.scout.policy import EARLY_SCOUT_V1, WatchStatus
from src.scout.repository import WatchRepository
from tests.riskdata.conftest import recorded_snapshot
from tests.runner.conftest import cases_in, run, runner_settings

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
FIRST = NOW - timedelta(hours=30)


def market(index: int) -> tuple[str, str]:
    pool_hex = f"{index + 11:02d}" * 20
    return (
        f"robinhood:mainnet:contract_address:0x{pool_hex}",
        f"robinhood:mainnet:0x{index + 61:02d}" + "ab" * 19,
    )


async def record(sessions, index: int, *, at=NOW, age=timedelta(seconds=20)):
    pair, base = market(index)
    snapshot = recorded_snapshot(
        at, age=age, pair_id=pair, base_asset_id=base, label=f"promo-{index}-{at.isoformat()}"
    )
    await MarketRecorder(sessions).record(snapshot)
    return snapshot


async def promotable(sessions, index: int, *, first_seen=FIRST):
    """A watch that has already passed its history check, the way the scout leaves one."""
    repository = WatchRepository(sessions)
    first = await record(sessions, index, at=first_seen, age=timedelta(0))
    await repository.sync(first, now=first_seen, allow_create=True)
    watch = await repository.by_pair(first.pair.pair_id)
    await repository.settle_history(
        watch.id,
        expected_next=watch.next_history_review_at,
        verdict="SUFFICIENT",
        status=WatchStatus.PROMOTABLE,
        next_at=None,
        now=first_seen + EARLY_SCOUT_V1.history_checkpoints[0],
    )
    await record(sessions, index)
    return await repository.by_pair(first.pair.pair_id)


async def case_on(sessions, index: int, status: TradeCaseStatus):
    """A case COMMANDER already opened for this market, in the given state."""
    pair, base = market(index)
    snapshot = recorded_snapshot(NOW, pair_id=pair, base_asset_id=base, label=f"case-{index}")
    async with sessions.begin() as session:
        session.add(
            TradeCaseRow(
                id=uuid4(),
                workflow_version="trade-case-v1",
                market_key=pair,
                chain="robinhood",
                network="mainnet",
                status=status.value,
                opened_at=NOW - timedelta(hours=2),
                updated_at=NOW - timedelta(hours=2),
                expires_at=NOW - timedelta(hours=1),
                originating_discovery_reference=uuid4(),
                strategy_policy_id=None,
                revision=1,
                reason_code=status.value,
                blockers=[],
                risk_input_digest=None,
                correlation_id=uuid4(),
                open_idempotency_key=f"seeded:{pair}:{status.value}",
                open_fingerprint="f" * 64,
                market_payload=snapshot.pair.market_identity.model_dump(mode="json"),
            )
        )


def scouted(**overrides):
    values = {
        "early_scout_enabled": True,
        "paper_runner_max_candidates": 1,
        "paper_runner_max_new_cases": 1,
        "paper_runner_max_cases": 1,
    }
    return runner_settings(**{**values, **overrides})


async def opened_for(sessions) -> list[str]:
    return [
        row.market_key
        for row in await cases_in(sessions)
        if not row.open_idempotency_key.startswith("seeded:")
    ]


async def test_a_promotable_watch_opens_a_full_case(risk_db):
    _, sessions = risk_db
    watch = await promotable(sessions, 0)
    summary = await run(sessions, scouted(), NOW)
    assert summary.cases_opened == 1
    assert await opened_for(sessions) == [watch.pair_id]
    promoted = await WatchRepository(sessions).by_pair(watch.pair_id)
    assert promoted.last_promoted_trade_case_id is not None


async def test_a_fresh_market_without_a_promotable_watch_opens_nothing(risk_db):
    """Scout enabled: a young pool that was merely recorded is no longer a candidate."""
    _, sessions = risk_db
    await record(sessions, 0)
    summary = await run(sessions, scouted(), NOW)
    assert summary.cases_opened == 0
    assert await cases_in(sessions) == []


async def test_a_watching_watch_opens_nothing(risk_db):
    _, sessions = risk_db
    repository = WatchRepository(sessions)
    snapshot = await record(sessions, 0)
    await repository.sync(snapshot, now=NOW, allow_create=True)
    summary = await run(sessions, scouted(), NOW)
    assert summary.cases_opened == 0


async def test_with_the_scout_disabled_intake_is_unchanged(risk_db):
    _, sessions = risk_db
    snapshot = await record(sessions, 0)
    summary = await run(sessions, scouted(early_scout_enabled=False), NOW)
    assert summary.cases_opened == 1
    assert await opened_for(sessions) == [snapshot.pair.pair_id]


@pytest.mark.parametrize(
    "status",
    [TradeCaseStatus.EVIDENCE_PENDING, TradeCaseStatus.RISK_REJECTED, TradeCaseStatus.EXECUTED],
    ids=["active_case", "risk_rejected", "executed"],
)
async def test_a_barred_market_is_filtered_before_the_limit(risk_db, status):
    """The older watch is barred; with one candidate slot the eligible one still gets it."""
    _, sessions = risk_db
    await promotable(sessions, 0, first_seen=FIRST)
    eligible = await promotable(sessions, 1, first_seen=FIRST + timedelta(minutes=5))
    await case_on(sessions, 0, status)
    summary = await run(sessions, scouted(), NOW)
    assert summary.cases_opened == 1
    assert await opened_for(sessions) == [eligible.pair_id]


@pytest.mark.parametrize(
    "status", [TradeCaseStatus.EXPIRED, TradeCaseStatus.CANCELLED], ids=["expired", "cancelled"]
)
async def test_an_ended_case_can_be_succeeded(risk_db, status):
    _, sessions = risk_db
    watch = await promotable(sessions, 0)
    await case_on(sessions, 0, status)
    summary = await run(sessions, scouted(), NOW)
    assert summary.cases_opened == 1
    assert await opened_for(sessions) == [watch.pair_id]


async def test_the_promoted_case_still_needs_its_own_orbit_evidence(risk_db):
    """The scout's assessment history is never copied into the case.

    Opening a case records the same provenance-only discovery envelope it always
    has: a reference to the candidate and no assessment. The ORBIT assessment
    the case needs is still produced by its own ORBIT task on its own input.
    """
    _, sessions = risk_db
    await promotable(sessions, 0)
    await run(sessions, scouted(), NOW)
    (case,) = await cases_in(sessions)
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(TradeCaseEvidenceRow).where(TradeCaseEvidenceRow.trade_case_id == case.id)
            )
        ).all()
    assert rows, "the provenance envelope intake always records"
    assert all(row.payload.get("assessment") is None for row in rows)


# ------------------------------------------------ exact-locator refresh before intake


async def located_promotable(sessions, provider):
    """A PROMOTABLE watch with a stored pool locator, its last reading 30h old."""
    from src.core.clock import FixedClock
    from src.scout.service import EarlyScoutCycle, ScoutPorts
    from tests.scout.conftest import EchoOrbit, scout_settings

    await EarlyScoutCycle(
        scout_settings(),
        sessions,
        ports=ScoutPorts(reasoning=EchoOrbit(), market_http=provider.transport()),
        clock=FixedClock(FIRST),
    ).execute()
    repository = WatchRepository(sessions)
    watch = await repository.by_pair(located_pair())
    assert watch is not None and watch.market.pool_locator is not None
    await repository.settle_history(
        watch.id,
        expected_next=watch.next_history_review_at,
        verdict="SUFFICIENT",
        status=WatchStatus.PROMOTABLE,
        next_at=None,
        now=FIRST + EARLY_SCOUT_V1.history_checkpoints[0],
    )
    return await repository.by_pair(located_pair())


def located_pair():
    from tests.scout.conftest import POOLS, pair_id

    return pair_id(POOLS[0])


def refreshing(**overrides):
    return scouted(market_provider="geckoterminal", market_chains="robinhood", **overrides)


async def test_an_identity_conflict_on_refresh_retires_the_watch(risk_db):
    """The exact locator now names another market: fail closed, open nothing."""
    from src.runner.composition import RunnerPorts
    from tests.atlas.conftest import QUOTE
    from tests.runner.provider import pool
    from tests.scout.conftest import POOLS, MarketProvider, young

    _, sessions = risk_db
    provider = MarketProvider(discovery=[young(0)])
    await located_promotable(sessions, provider)
    impostor = pool(POOLS[0], base="0x" + "ee" * 20, quote=QUOTE, price="0.002")
    provider.discovery = []
    provider.targeted = {POOLS[0]: impostor}

    summary = await run(
        sessions, refreshing(), NOW, ports=RunnerPorts(market_http=provider.transport())
    )

    watch = await WatchRepository(sessions).by_pair(located_pair())
    assert provider.multi_requests, "the locator was asked about"
    assert watch.status is WatchStatus.RETIRED
    assert watch.reason_code == "MARKET_IDENTITY_MISMATCH"
    assert watch.next_orbit_review_at is None and watch.next_history_review_at is None
    assert summary.promotion.refreshed == 0
    assert summary.cases_opened == 0
    assert await cases_in(sessions) == []


async def test_a_matching_exact_locator_refresh_feeds_intake(risk_db):
    from src.runner.composition import RunnerPorts
    from tests.scout.conftest import MarketProvider, young

    _, sessions = risk_db
    provider = MarketProvider(discovery=[young(0)])
    watch = await located_promotable(sessions, provider)
    provider.discovery = []
    provider.targeted = {young(0)["attributes"]["address"]: young(0)}

    summary = await run(
        sessions, refreshing(), NOW, ports=RunnerPorts(market_http=provider.transport())
    )

    after = await WatchRepository(sessions).by_pair(located_pair())
    assert after.status is WatchStatus.PROMOTABLE
    assert after.last_seen_at == NOW
    assert after.latest_snapshot_id != watch.latest_snapshot_id
    assert summary.promotion.refreshed == 1
    assert not provider.discovery_requests[1:], "no discovery scan in the full run"
    assert summary.cases_opened == 1
    assert await opened_for(sessions) == [watch.pair_id]
