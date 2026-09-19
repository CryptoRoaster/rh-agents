"""Two acquiring passes at once, and one after another.

PostgreSQL only. What these prove are row locks and durable identity, and SQLite
has neither — a test that ran there would report a pass it had not earned.
"""

import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.data.tables import ExecutionRow, MarketObservationRow, TradeCaseExecutionRow, TradeCaseRow
from tests.runner.conftest import run
from tests.runner.provider import MarketProvider, payment, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_acquisition import (
    PAIR_ID,
    acquiring_ports,
    full_acquiring_settings,
    observations,
)
from tests.runner.test_end_to_end import SPOT

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def counted(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def test_two_concurrent_acquiring_runs_converge_on_one_case_and_one_fill(risk_db, now, trace):
    """Both may observe the market. Neither may open a second case or fill twice.

    Acquisition is append-only by construction — two passes observing one market
    at one instant are two events, not a conflict — and everything that must be
    singular is made singular by the contracts that already were: the intake
    generation key, the one risk request per case, and the order key the fill is
    bound to.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)

    outcomes = await asyncio.gather(
        run(
            sessions,
            settings,
            now,
            ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
            run_id=uuid4(),
        ),
        run(
            sessions,
            settings,
            now,
            ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
            run_id=uuid4(),
        ),
        return_exceptions=True,
    )

    summaries = [item for item in outcomes if not isinstance(item, Exception)]
    assert summaries, outcomes
    # Nothing raised on a conflicting event identity: every recording either
    # wrote its own event or found one already stored.
    assert all(item.acquisition.refused == 0 for item in summaries), [
        item.acquisition for item in summaries
    ]
    assert all(item.acquisition.recorded + item.acquisition.unchanged >= 1 for item in summaries), [
        item.acquisition for item in summaries
    ]
    assert await counted(sessions, TradeCaseRow) <= 2, "one generation per market"
    assert await counted(sessions, ExecutionRow) <= 1
    assert await counted(sessions, TradeCaseExecutionRow) <= 1


async def test_a_second_run_over_the_same_market_records_its_own_event(risk_db, now, trace):
    """A restart observes the world again; it does not rewrite what was stored.

    Recorder idempotency is per event, and a second observation of one market is
    a second event by construction — so the earlier reading stays exactly as it
    was and the newer one is what answers from now on.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    first = await run(
        sessions,
        settings,
        now,
        ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
    )
    assert first.acquisition.recorded == 2, first.acquisition
    before = [item.id for item in await observations(sessions, PAIR_ID)]

    later = now + timedelta(seconds=20)
    second = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_discovery_requests=0,
            pulse_worker_enabled=False,
            anchor_worker_enabled=False,
        ),
        later,
        ports=acquiring_ports(
            later,
            model,
            MarketProvider(targeted=[traded(SPOT * Decimal("1.05")), payment()]),
        ),
    )

    assert second.acquisition.recorded == 2, second.acquisition
    rows = await observations(sessions, PAIR_ID)
    assert [item.id for item in rows][: len(before)] == before, "the first event is untouched"
    assert len(rows) == 2
    assert await counted(sessions, MarketObservationRow) == 4
    async with sessions() as session:
        for_market = await session.scalar(
            select(func.count()).select_from(TradeCaseRow).where(TradeCaseRow.market_key == PAIR_ID)
        )
    # Observing a market again is not a reason to open a second case for it.
    # The payment asset's own market legitimately gets one on the second pass —
    # intake judges every recorded candidate, and that one had none.
    assert for_market == 1


async def test_no_provider_request_is_made_while_a_row_is_locked(risk_db, now, trace):
    """Nothing is held across a call to somebody else's server.

    Proved from the other side: while the provider request is in flight, a
    separate transaction takes the paper account and every trade case row
    ``FOR UPDATE NOWAIT``. A lock this stage held would make that fail
    immediately, which is exactly what the assertion is for.

    It matters because a provider request is unbounded by anything this system
    controls. A database lock held across one is a stall everybody else pays
    for, and holding the account lock across it would block the executor.
    """
    from sqlalchemy.exc import DBAPIError

    from src.data.tables import AccountRow

    _, sessions = risk_db
    model = ScriptedSpecialists()
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(
        sessions,
        settings,
        now,
        ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
    )

    provider = MarketProvider(targeted=[traded(SPOT), payment()])
    taken: list[str] = []

    async def while_in_flight(request):
        if "/pools/multi/" in request.url.path:
            async with sessions.begin() as session:
                await session.execute(
                    select(AccountRow).where(AccountRow.id == 1).with_for_update(nowait=True)
                )
                await session.execute(select(TradeCaseRow).with_for_update(nowait=True))
                taken.append(request.url.path)
        return provider.handle(request)

    import httpx

    later = now + timedelta(seconds=20)
    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_discovery_requests=0,
            pulse_worker_enabled=False,
            anchor_worker_enabled=False,
        ),
        later,
        ports=acquiring_ports(
            later, model, provider, market_http=httpx.MockTransport(while_in_flight)
        ),
    )

    assert taken, "the targeted request never happened"
    assert summary.acquisition.recorded == 2, summary.acquisition
    assert not isinstance(summary, DBAPIError)
