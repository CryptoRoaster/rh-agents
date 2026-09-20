"""Three ways the acquisition stage could be lied to, or lie to itself.

Each test below first existed as a reproduction against
`8e7914cc95837e15a6bd7903f6ee74ee05df4183`, where it failed. Everything is the
production composition; the substituted boundaries are the HTTP response bytes,
the reasoning model and the specialist sources, in test code.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from src.data.tables import (
    ExecutionRow,
    MarketObservationRow,
    TradeCaseRiskRequestRow,
    TradeCaseRow,
    WorkerTaskAttemptRow,
)
from src.runner.models import AcquisitionOutcome, AcquisitionStop, ExitCode, RunStop
from tests.runner.conftest import run
from tests.runner.provider import MarketProvider, payment, pool, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_acquisition import (
    PAIR_ID,
    acquiring_ports,
    entries,
    full_acquiring_settings,
    observations,
)
from tests.runner.test_end_to_end import SPOT

TOKEN = "0x" + "a1" * 20
# The pool the case is about, as the market layer's own fixtures name it.
PAIR_POOL = "0x" + "e5" * 20
QUOTE = "0x" + "b2" * 20
# A token nobody asked about, and a pool that has nothing to do with the case.
OTHER_TOKEN = "0x" + "33" * 20
OTHER_POOL = "0x" + "ab" * 20


async def opened_case(sessions, now, model, **overrides):
    """One live case, its market and its payment market, all acquired."""
    settings = full_acquiring_settings(
        pulse_worker_enabled=False, anchor_worker_enabled=False, **overrides
    )
    summary = await run(
        sessions,
        settings,
        now,
        ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
    )
    assert summary.acquisition.recorded == 2, summary.acquisition
    return summary


async def counted(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


# ---------------------------------------------------- 1. the answer's own identity


async def test_a_swapped_base_asset_under_the_asked_pool_is_refused(risk_db, now, trace):
    """The pool is the one that was asked about. The market is not.

    Reproduction: `observe()` checks the pair identifier, and a contract-address
    pair identifier carries the chain, the network and the pool — not the assets
    and not the venue. `record_pair_reporting` then compares the snapshot with
    the pair from that *same answer*, which agrees with itself by construction.
    So an answer that kept the requested pool address and named a different base
    token was recorded against the planned market, and the case's next reading
    would have been a different market's price.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)
    before = [item.id for item in await observations(sessions, PAIR_ID)]
    stored = await identity_of(sessions, PAIR_ID)

    later = now + timedelta(seconds=20)
    swapped = MarketProvider(
        discovery=[],
        # Same pool, same pair identifier, different base asset — and internally
        # consistent: the token resource is present and binds to its own id.
        targeted=[pool(PAIR_POOL, base=OTHER_TOKEN, quote=QUOTE, price="1.10"), payment()],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, swapped),
    )

    refused = [item for item in entries(summary, outcome=AcquisitionOutcome.REFUSED)]
    assert [(item.pair_id, item.reason) for item in refused] == [
        (PAIR_ID, "MARKET_IDENTITY_MISMATCH")
    ], summary.acquisition
    # Nothing was written for that answer, and what was already recorded is
    # exactly as it was.
    assert [item.id for item in await observations(sessions, PAIR_ID)] == before
    assert await identity_of(sessions, PAIR_ID) == stored
    assert summary.fills == 0


async def test_a_swapped_quote_asset_under_the_asked_pool_is_refused(risk_db, now, trace):
    """The payment side is identity too: it decides what the price is *in*."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)
    before = [item.id for item in await observations(sessions, PAIR_ID)]

    later = now + timedelta(seconds=20)
    swapped = MarketProvider(
        discovery=[],
        targeted=[pool(PAIR_POOL, base=TOKEN, quote=OTHER_TOKEN, price="1.10"), payment()],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, swapped),
    )

    assert [
        (item.pair_id, item.reason) for item in entries(summary, outcome=AcquisitionOutcome.REFUSED)
    ] == [(PAIR_ID, "MARKET_IDENTITY_MISMATCH")], summary.acquisition
    assert [item.id for item in await observations(sessions, PAIR_ID)] == before


async def test_a_swapped_venue_under_the_asked_pool_is_refused(risk_db, now, trace):
    """A contract-address pair identifier says nothing about the venue.

    Two venues reporting one address are two markets, and the recorded identity
    names which one this system has been watching.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)
    before = [item.id for item in await observations(sessions, PAIR_ID)]

    later = now + timedelta(seconds=20)
    swapped = MarketProvider(
        discovery=[],
        targeted=[
            pool(PAIR_POOL, base=TOKEN, quote=QUOTE, price="1.10", venue="another-dex"),
            payment(),
        ],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, swapped),
    )

    assert [
        (item.pair_id, item.reason) for item in entries(summary, outcome=AcquisitionOutcome.REFUSED)
    ] == [(PAIR_ID, "MARKET_IDENTITY_MISMATCH")], summary.acquisition
    assert [item.id for item in await observations(sessions, PAIR_ID)] == before


async def identity_of(sessions, pair_id):
    """The canonical identity of the newest recorded observation of a market."""
    from src.markets.reader import MarketReader

    found = await MarketReader(sessions).identities([pair_id])
    return found[0] if found else None


# ------------------------------------------------------- 2. budget spent on asking


async def test_a_market_asked_about_and_not_returned_still_costs_its_budget(risk_db, now, trace):
    """Work that was triggered is not made free by the answer.

    Reproduction: the market budget was counted from *recorded* markets, so a
    targeted request the provider did not answer left the budget untouched — and
    the discovery read that follows then spent it on some other market. One
    market's worth of budget bought two requests.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)

    later = now + timedelta(seconds=20)
    # The case's market is asked about and not returned. Discovery would have a
    # different market to offer, and must never get the chance.
    provider = MarketProvider(
        discovery=[pool(OTHER_POOL, base=OTHER_TOKEN, quote=QUOTE, price="7")],
        targeted=[],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_markets=1,
            paper_runner_acquisition_max_discovery_requests=1,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert len(provider.multi_requests) == 1, provider.paths
    assert provider.discovery_requests == [], provider.paths
    assert summary.acquisition.requested == 1, summary.acquisition
    assert summary.acquisition.recorded == 0, summary.acquisition
    assert summary.acquisition.budget_spent == 1, summary.acquisition
    # And nothing new reached the database from this pass.
    assert await counted(sessions, MarketObservationRow) == 2


async def test_a_refused_recording_still_costs_its_budget(risk_db, now, trace):
    """A market that was fetched and then refused was still fetched."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)

    later = now + timedelta(seconds=20)
    provider = MarketProvider(
        discovery=[pool(OTHER_POOL, base=OTHER_TOKEN, quote=QUOTE, price="7")],
        targeted=[pool(PAIR_POOL, base=OTHER_TOKEN, quote=QUOTE, price="1.10")],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_markets=1,
            paper_runner_acquisition_max_discovery_requests=1,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert summary.acquisition.refused == 1, summary.acquisition
    assert provider.discovery_requests == [], provider.paths
    assert summary.acquisition.budget_spent == 1
    assert await counted(sessions, MarketObservationRow) == 2


async def test_a_spent_budget_stops_the_second_chain_before_its_request(risk_db, now, trace):
    """Budget is a property of the run, not of one chain's turn at it."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(
        sessions,
        now,
        model,
        atlas_worker_enabled=False,
        vector_worker_enabled=False,
        evm_runtime_enabled=False,
    )

    later = now + timedelta(seconds=20)
    provider = MarketProvider(
        discovery=[pool(OTHER_POOL, base=OTHER_TOKEN, quote=QUOTE, price="7")],
        targeted=[traded(SPOT * Decimal("1.10"))],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(
            market_chains="robinhood,bsc",
            paper_runner_acquisition_max_markets=1,
            # Both chains would be discovered, and neither may be.
            paper_runner_acquisition_max_discovery_requests=2,
            atlas_worker_enabled=False,
            vector_worker_enabled=False,
            evm_runtime_enabled=False,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert len(provider.multi_requests) == 1, provider.paths
    assert provider.discovery_requests == [], provider.paths
    assert summary.acquisition.recorded == 1, summary.acquisition
    assert summary.acquisition.budget_spent == 1


async def test_discovery_reserves_what_it_is_allowed_to_bring_back(risk_db, now, trace):
    """Capacity is committed before the read, not after it returns."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT)])

    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_markets=2,
            paper_runner_acquisition_max_discovery_requests=2,
            market_chains="robinhood,bsc",
            atlas_worker_enabled=False,
            vector_worker_enabled=False,
            evm_runtime_enabled=False,
            pulse_worker_enabled=False,
            anchor_worker_enabled=False,
        ),
        now,
        ports=acquiring_ports(now, model, provider),
    )

    # The first chain's read was allowed to return two pools, so it committed
    # two — the second chain gets no read even though only one came back.
    assert len(provider.discovery_requests) == 1, provider.paths
    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.recorded == 1


async def test_one_market_wanted_twice_still_costs_one(risk_db, now, trace):
    """Deduplication survives the change: one market is one budgeted request."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)
    from tests.runner.test_acquisition import hold

    await hold(sessions, now, trace, pair_id=PAIR_ID)

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.05")), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_markets=2,
            paper_runner_acquisition_max_discovery_requests=0,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    asked = provider.multi_requests[0].rsplit("/", 1)[-1].split(",")
    assert len(asked) == 2 and len(set(asked)) == 2, asked
    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.recorded == 2
    assert summary.acquisition.unchanged == 1


async def test_a_failed_read_does_not_erase_the_markets_it_asked_about(risk_db, now, trace):
    """The asking happened. A summary that counted answers would deny it."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened_case(sessions, now, model)

    later = now + timedelta(seconds=20)
    broken = MarketProvider(discovery=[], targeted=[traded(SPOT), payment()], status=503)
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, broken),
    )

    assert summary.acquisition.stop == AcquisitionStop.PROVIDER_FAILED.value
    assert summary.acquisition.requested == 2, summary.acquisition
    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.recorded == 0
    assert summary.acquisition.failed == 2
    # HTTP attempts include the retry the transport is configured for, and the
    # market counters are untouched by it: four counters, four questions.
    assert summary.acquisition.http_attempts > summary.acquisition.provider_requests
    assert await counted(sessions, MarketObservationRow) == 2


# ---------------------------------------------------- 3. the stop query is bounded


class HangingPause:
    """A stop source that is reachable and never answers.

    Records that it was entered and that its cancellation was carried out, so a
    test can prove the timeout did not simply abandon it.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def system_paused(self) -> bool:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def locked_paused(self, session) -> bool:
        return await self.system_paused()


class CountingPause:
    """A stop source that answers, and says how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    async def system_paused(self) -> bool:
        self.asked += 1
        return False

    async def locked_paused(self, session) -> bool:
        return False


async def test_an_expired_deadline_never_asks_the_stop_source(risk_db, now, trace):
    """Out of time before the first question, so the question is not asked."""
    from src.core.clock import FixedClock
    from src.markets.reader import MarketReader
    from src.runner.acquisition import BoundedMarketAcquisition
    from src.runner.composition import acquisition_limits_from_settings
    from src.runner.service import Deadline
    from tests.runner.test_acquisition import acquiring_settings

    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT)])
    pause = CountingPause()
    settings = acquiring_settings()
    stage = BoundedMarketAcquisition(
        settings,
        sessions,
        MarketReader(sessions, clock=FixedClock(now)),
        acquisition_limits_from_settings(settings),
        pause=pause,
        clock=FixedClock(now),
        http=provider.transport(),
    )

    reading = await stage.execute(Deadline(-1))

    assert pause.asked == 0, "an expired run does not query a stop source"
    assert reading.stop == AcquisitionStop.TIME_BUDGET_REACHED.value
    assert provider.paths == []


async def test_a_stop_query_that_hangs_is_bounded_and_ends_the_pass(risk_db, now, trace):
    """Entered, cut off at the deadline, awaited, and nothing traded.

    Reproduction: the first stop query was awaited without a bound at all, so a
    stop source that never answered held the whole run open for as long as it
    liked — past the run's own deadline, with nothing else able to happen.
    """
    from src.core.clock import FixedClock
    from src.runner.composition import build_stack
    from src.runner.service import BoundedPaperRun

    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    pause = HangingPause()
    stack = build_stack(
        full_acquiring_settings(paper_runner_acquisition_max_seconds=1),
        sessions,
        ports=acquiring_ports(now, model, provider, pause=pause),
        clock=FixedClock(now),
    )

    summary = await BoundedPaperRun(stack).execute()

    # The port really was entered, and its cancellation really was carried out.
    assert pause.entered.is_set()
    assert pause.cancelled.is_set(), "the hanging query was abandoned rather than cancelled"
    assert asyncio.all_tasks() == {asyncio.current_task()} or all(
        item.done() or item is asyncio.current_task() for item in asyncio.all_tasks()
    )

    # Both facts, in the typed vocabulary: out of time, and the stop unconfirmed.
    assert summary.acquisition.stop == AcquisitionStop.SYSTEM_STOP_UNREADABLE.value
    assert summary.acquisition.detail == "TIME_BUDGET_REACHED", summary.acquisition
    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert summary.errors == ("SYSTEM_STOP_UNREADABLE",)
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE

    # Nothing was asked of the provider and nothing mutating followed.
    assert provider.paths == []
    assert summary.acquisition.recorded == 0
    assert await counted(sessions, MarketObservationRow) == 0
    assert await counted(sessions, TradeCaseRow) == 0
    assert await counted(sessions, WorkerTaskAttemptRow) == 0
    assert await counted(sessions, TradeCaseRiskRequestRow) == 0
    assert await counted(sessions, ExecutionRow) == 0
    assert summary.cases_opened == 0 and summary.fills == 0
