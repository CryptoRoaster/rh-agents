"""Autonomous case intake: how the system gets a case to work on at all.

Before this, a TradeCase could only be opened by test code — nothing in `src/`
called `open_trade_case` and every API route was a read. An autonomous system
had no way to begin.

What intake must not become is a strategy. Choosing between candidates on market
grounds would make it a second analyst whose reasoning nobody recorded, and ORBIT
already exists to judge whether a candidate is worth pursuing. So the tests below
are mostly about what intake refuses and why, and about the one thing that is
genuinely hard: two workers seeing the same candidate at the same moment.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.markets.fake import fixture_snapshot
from src.markets.models import MarketCandidate
from src.orchestration.commander.intake import IntakeRefusal
from src.orchestration.workflow.models import TradeCaseStatus
from tests.commander.conftest import intake_service

pytestmark = pytest.mark.usefixtures("worker_db")


def candidate_for(now, *, chain="robinhood", seconds_ago=10, is_fixture=False, pair=None):
    """A recorded candidate plus the snapshot it points at.

    Every identity is rewritten together, because the contracts refuse a
    snapshot whose asset ids disagree with its chain — which is the identity
    invariant this system keeps everywhere and not something to work around.
    """
    snapshot = fixture_snapshot(now - timedelta(seconds=seconds_ago), uuid4())
    base_id = f"{chain}:mainnet:0x{'22' * 20}"
    quote_id = f"{chain}:mainnet:0x{'33' * 20}"
    pair_id = pair or f"{chain}:mainnet:contract_address:0x{'11' * 20}"
    common = {"chain": chain, "is_fixture": is_fixture}
    base = snapshot.pair.base.model_copy(update={**common, "asset_id": base_id})
    quote = snapshot.pair.quote.model_copy(update={**common, "asset_id": quote_id})
    return_pair = snapshot.pair.model_copy(
        update={**common, "asset_id": base_id, "pair_id": pair_id, "base": base, "quote": quote}
    )
    snapshot = snapshot.model_copy(
        update={
            **common,
            "asset_id": base_id,
            "pair": return_pair,
            "price": snapshot.price.model_copy(update={**common, "asset_id": base_id}),
            "liquidity": snapshot.liquidity.model_copy(update={**common, "asset_id": base_id}),
            "volume": snapshot.volume.model_copy(update={**common, "asset_id": base_id}),
        }
    )
    return MarketCandidate.from_snapshot(snapshot), snapshot


def service_for(sessions, now, pairs, **overrides):
    candidates, snapshots = [], {}
    for item in pairs:
        candidate, snapshot = item
        candidates.append(candidate)
        snapshots[candidate.pair_id] = snapshot
    return intake_service(sessions, now, candidates=candidates, snapshots=snapshots, **overrides)


# ------------------------------------------------------ 96: the happy path


async def test_scenario_96_a_recorded_candidate_opens_exactly_one_case(worker_db, now, trace):
    """Candidate to case, with every specialist task created and ORBIT eligible."""
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])

    outcome = await service.run_cycle()

    assert outcome.opened_count == 1
    case = outcome.opened[0]
    assert case.market.pair_id == candidate.pair_id
    assert case.originating_discovery_reference == candidate.id
    assert case.status in (TradeCaseStatus.DISCOVERED, TradeCaseStatus.EVIDENCE_PENDING)

    tasks = await service.cases.tasks(case.id)
    roles = {task.role.value for task in tasks}
    assert {"ORBIT", "ATLAS", "SIGNAL", "VECTOR", "PULSE", "ANCHOR", "FUSE"} <= roles


def test_intake_reaches_no_provider():
    """Recorded observations only. The market layer already did the fetching."""
    import ast
    from pathlib import Path

    source = Path("src/orchestration/commander/intake.py").read_text()
    modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert modules
    for forbidden in ("httpx", "geckoterminal", "kyberswap", "neynar", "runtime.rpc"):
        assert not any(forbidden in module for module in modules)


# ---------------------------------------- T, U, V: what intake refuses


async def test_scenario_t_replaying_a_cycle_opens_no_second_case(worker_db, now, trace):
    """§88. Idempotent by derived identity, not by a check-then-insert."""
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])

    first = await service.run_cycle()
    second = await service.run_cycle()

    assert first.opened_count == 1
    assert second.opened_count == 0
    assert second.refused == ((candidate.pair_id, IntakeRefusal.ACTIVE_CASE_EXISTS),)


async def test_scenario_u_a_stale_candidate_opens_nothing(worker_db, now, trace):
    """§52, §89. Age is measured from the market's own observation time."""
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now, seconds_ago=3600)
    service = service_for(sessions, now, [(candidate, snapshot)])

    outcome = await service.run_cycle()
    assert outcome.opened_count == 0
    assert outcome.refused == ((candidate.pair_id, IntakeRefusal.CANDIDATE_TOO_OLD),)


async def test_scenario_v_an_unsupported_chain_opens_nothing(worker_db, now, trace):
    """§90. An allow-list: an unverified chain is not a place to start work."""
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now, chain="ethereum")
    service = service_for(sessions, now, [(candidate, snapshot)])

    outcome = await service.run_cycle()
    assert outcome.opened_count == 0
    assert outcome.refused == ((candidate.pair_id, IntakeRefusal.CHAIN_NOT_ENABLED),)


async def test_a_fixture_market_never_starts_a_real_workflow(worker_db, now, trace):
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now, is_fixture=True)
    service = service_for(sessions, now, [(candidate, snapshot)])

    outcome = await service.run_cycle()
    assert outcome.opened_count == 0


async def test_a_candidate_whose_market_cannot_be_read_opens_nothing(worker_db, now, trace):
    """§15. The case needs the full canonical identity, which only the market holds.

    Reconstructing it from the candidate's pair id would mean guessing the base
    and quote assets, the venue and the provider — which is exactly the
    symbol-level identity this system refuses everywhere else.
    """
    _, sessions = worker_db
    candidate, _ = candidate_for(now)
    service = intake_service(sessions, now, candidates=[candidate], snapshots={})

    outcome = await service.run_cycle()
    assert outcome.opened_count == 0
    assert outcome.refused == ((candidate.pair_id, IntakeRefusal.MARKET_UNAVAILABLE),)


# ---------------------------------------------- 51: backpressure, not ranking


async def test_scenario_51_a_provider_burst_cannot_open_unbounded_cases(worker_db, now, trace):
    """A burst is a fact about a provider, not an abundance of opportunity."""
    _, sessions = worker_db
    pairs = [
        candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{index:040x}")
        for index in range(1, 13)
    ]
    service = service_for(sessions, now, pairs)

    outcome = await service.run_cycle()

    assert outcome.opened_count == 5
    assert len(outcome.refused) == 7
    assert {reason for _, reason in outcome.refused} == {IntakeRefusal.CYCLE_LIMIT_REACHED}


async def test_the_bounded_cycle_takes_candidates_in_a_deterministic_order(worker_db, now, trace):
    """§51. Oldest observation first — an order, deliberately not a ranking.

    Nothing here prefers more liquidity, more attention or a better-looking
    chart. Those are ORBIT's question, and a coordinator that answered them
    would be a strategy nobody could audit.
    """
    _, sessions = worker_db
    pairs = [
        candidate_for(
            now,
            seconds_ago=seconds,
            pair=f"robinhood:mainnet:contract_address:0x{seconds:040x}",
        )
        for seconds in (10, 20, 30, 40, 50, 60, 70)
    ]
    service = service_for(sessions, now, pairs)

    outcome = await service.run_cycle()
    opened = [case.market.pair_id for case in outcome.opened]
    oldest_first = [
        candidate.pair_id for candidate, _ in sorted(pairs, key=lambda item: item[0].observed_at)
    ][:5]
    assert opened == oldest_first


# --------------------------------------------------- M: system stops


async def test_scenario_m_a_kill_switch_opens_no_cases_at_all(worker_db, now, trace):
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)], kill_switch=True)

    outcome = await service.run_cycle()
    assert outcome.opened_count == 0
    assert outcome.refused == (("*", IntakeRefusal.SYSTEM_PAUSED),)
    assert await service.cases.list_trade_cases() == ()


async def test_the_intake_key_is_canonical_identity_never_a_symbol(worker_db, now, trace):
    """§15. Two workers must compute the same key, and a ticker is not an identity."""
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])

    key = service.intake_key(candidate)
    assert candidate.pair_id in key
    assert "DEMO" not in key
    assert service.intake_key(candidate) == key


# ------------------------------------------------ 53: two workers, one candidate


async def test_scenario_53_two_workers_racing_on_one_candidate_open_one_case(worker_db, now, trace):
    """§53. The race that a check-then-insert would lose.

    Both workers see the same eligible candidate and both decide to act. Safety
    does not come from the duplicate check — that is advisory and both will pass
    it — but from the case identity being *derived* from the candidate, so the
    second insert collides with a unique constraint and resolves to the first
    case instead of raising.
    """
    import asyncio

    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    candidate, snapshot = candidate_for(now)
    first = service_for(sessions, now, [(candidate, snapshot)])
    second = service_for(sessions, now, [(candidate, snapshot)])

    outcomes = await asyncio.gather(first.run_cycle(), second.run_cycle(), return_exceptions=True)
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    cases = await first.cases.list_trade_cases()
    assert len(cases) == 1
    # Both workers computed the same key, so whichever raced second converged on
    # the first worker's case rather than failing or creating a rival.
    opened = [case.id for outcome in outcomes for case in outcome.opened]
    assert len(set(opened)) == 1


async def test_two_workers_on_different_candidates_open_both(worker_db, now, trace):
    """The control: dedupe is by identity, not a lock that serialises everything."""
    import asyncio

    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    left = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{1:040x}")
    right = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{2:040x}")
    outcomes = await asyncio.gather(
        service_for(sessions, now, [left]).run_cycle(),
        service_for(sessions, now, [right]).run_cycle(),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    cases = await service_for(sessions, now, [left]).cases.list_trade_cases()
    assert len(cases) == 2


async def test_a_replayed_cycle_after_a_race_still_opens_nothing(worker_db, now, trace):
    """Idempotency survives the race, not merely the sequential replay."""
    import asyncio

    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    candidate, snapshot = candidate_for(now)
    await asyncio.gather(
        service_for(sessions, now, [(candidate, snapshot)]).run_cycle(),
        service_for(sessions, now, [(candidate, snapshot)]).run_cycle(),
    )
    service = service_for(sessions, now, [(candidate, snapshot)])
    again = await service.run_cycle()
    assert again.opened_count == 0
    assert len(await service.cases.list_trade_cases()) == 1


async def test_the_open_fingerprint_inputs_are_worker_independent(worker_db, now, trace):
    """Two services, two clocks, one candidate — one identity.

    This is what makes the race converge rather than conflict. `open_trade_case`
    resolves a duplicate insert by comparing the open fingerprint, so every
    input to it must be a function of the candidate: the key, the correlation
    and the expiry alike.
    """
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    early = service_for(sessions, now, [(candidate, snapshot)])
    late = service_for(sessions, now + timedelta(seconds=37), [(candidate, snapshot)])

    assert early.intake_key(candidate) == late.intake_key(candidate)
    assert early.intake_correlation(candidate) == late.intake_correlation(candidate)
    # The expiry is measured from the observation, not from either clock.
    expected = candidate.observed_at + early.policy.case_lifetime
    opened = (await early.run_cycle()).opened[0]
    assert opened.expires_at == expected
