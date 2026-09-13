"""PULSE over the real recorder and reader, with no stub between them.

Everything else mocks the market layer to make one situation legible. This file
does not: observations are recorded through the real recorder and read back
through the real reader, so the defect this phase fixed is proven closed against
the actual query rather than against a helpful fake.
"""

from datetime import timedelta
from decimal import Decimal

from src.agents.pulse.context import PulseContextReader
from src.agents.pulse.handler import PulseWorkerHandler
from src.core.clock import FixedClock
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.orchestration.worker.models import TaskAttemptOutcome
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.service import TradeCaseService
from tests.pulse.conftest import LEVEL
from tests.pulse.test_workflow import (
    open_case,
    record_setup,
    snapshot_for,
    surround,
    trigger_evidence,
)


async def drive(sessions, now, trace, prices, *, key):
    """Record a price history, then run one real PULSE check over it."""
    recorder = MarketRecorder(sessions, clock=FixedClock(now))
    for seconds_ago, price in prices:
        await recorder.record(snapshot_for(now, price=Decimal(price), seconds_ago=seconds_ago))

    clock = FixedClock(now)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = PulseContextReader(
        cases=cases,
        markets=MarketReader(sessions, clock=clock),
        clock=clock,
        include_fixtures=True,
    )
    trade_case = await open_case(cases, now, trace, key)
    await surround(cases, trade_case, now)
    await record_setup(cases, trade_case, now)

    runner = WorkerRunner(
        runtime,
        PulseWorkerHandler(),
        CapabilityProvider(service=runtime, pulse=reader),
        registration_key=f"{key}-worker",
    )
    await runner.register()
    return runtime, trade_case, await runner.run_once()


async def test_a_recorded_crossing_triggers_even_after_the_price_reverts(pulse_db, now, trace):
    """The audit finding, closed end to end.

    1.22 crossed the level and the market fell back to 1.17 before this check.
    Reading only the newest recorded price would report that nothing happened.
    """
    _, sessions = pulse_db
    runtime, trade_case, disposition = await drive(
        sessions, now, trace, [(80, "1.18"), (50, "1.22"), (10, "1.17")], key="e2e-cross"
    )
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    evidence = await trigger_evidence(runtime.cases, trade_case.id)
    assert len(evidence) == 1
    payload = evidence[0].payload
    assert payload.observed_price == Decimal("1.22")
    detail = payload.detail
    assert detail is not None
    # Bound to the exact recorded observation that satisfied the condition.
    assert detail.observed_at == now - timedelta(seconds=50)
    assert detail.reference_price == LEVEL


async def test_a_market_that_never_crossed_waits(pulse_db, now, trace):
    _, sessions = pulse_db
    runtime, trade_case, disposition = await drive(
        sessions, now, trace, [(80, "1.18"), (50, "1.19"), (10, "1.17")], key="e2e-quiet"
    )
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert disposition.reason_code == "CONDITION_NOT_MET"
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


async def test_a_crossing_that_has_aged_out_waits(pulse_db, now, trace):
    """Conservative by design: an old crossing is not a current opportunity."""
    _, sessions = pulse_db
    runtime, trade_case, disposition = await drive(
        sessions, now, trace, [(600, "1.22"), (10, "1.17")], key="e2e-stale"
    )
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.WAITING
    assert await trigger_evidence(runtime.cases, trade_case.id) == []


async def test_the_earliest_crossing_is_the_one_recorded(pulse_db, now, trace):
    _, sessions = pulse_db
    runtime, trade_case, _ = await drive(
        sessions,
        now,
        trace,
        [(100, "1.18"), (70, "1.21"), (40, "1.19"), (10, "1.23")],
        key="e2e-first",
    )
    evidence = await trigger_evidence(runtime.cases, trade_case.id)
    assert evidence[0].payload.observed_price == Decimal("1.21")
    assert evidence[0].payload.detail.observed_at == now - timedelta(seconds=70)


async def test_no_provider_request_is_made_on_any_check(pulse_db, now, trace):
    """The monitor reads recorded data. It never reaches a provider."""
    import inspect

    from src.agents.pulse import context, handler

    for module in (context, handler):
        source = inspect.getsource(module)
        for forbidden in ("httpx", "AsyncClient", "geckoterminal", "requests"):
            assert forbidden not in source
