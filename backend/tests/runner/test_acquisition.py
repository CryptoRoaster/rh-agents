"""From a provider response to a PAPER fill, with nothing written by hand.

The control case of this phase. No market row is prepared by the test: the only
thing supplied is the HTTP response bytes a public market provider would return,
and everything from the transport onwards — the network directory, the adapter,
`normalize`, the `MarketRecorder`, `MarketReader`, intake, the real specialist
handlers with their real context readers, `RiskRequestService`,
`src.risk.engine.evaluate`, `CaseFillService`, the executor and the ledger — is
the production object, composed by the production `build_stack`.

Two boundaries are substituted, both visibly and both in test code: the model,
and the bytes of an HTTP response. Neither is a claim that the real provider
works.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.models import AgentRole
from src.data.tables import ExecutionRow, MarketObservationRow, PositionRow, TradeCaseRow
from src.runner.composition import RunnerPorts
from src.runner.models import (
    AcquisitionNeed,
    AcquisitionOutcome,
    AcquisitionStop,
    ExitCode,
    RunStop,
)
from tests.runner.conftest import run, runner_settings
from tests.runner.provider import PAYMENT_POOL, POOL, MarketProvider, payment, pool, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_end_to_end import (
    SPOT,
    dispersed_holders,
    specialist_ports,
    succeeded,
    traded_case,
)

CHAIN = "robinhood"
NETWORK = "mainnet"
PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{POOL}"
PAYMENT_PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{PAYMENT_POOL}"


def acquiring_settings(**overrides):
    """A run that is permitted to ask the provider for observations.

    Stated in full rather than defaulted: the acquisition is off unless an
    operator says otherwise, and every bound it is held to is named here so a
    reader can see what this pass may spend.
    """
    defaults: dict[str, object] = {
        "market_provider": "geckoterminal",
        # One chain, so the provider budget is spent where the fixtures are.
        "market_chains": CHAIN,
        "paper_runner_market_acquisition_enabled": True,
        "paper_runner_acquisition_max_markets": 4,
        "paper_runner_acquisition_max_discovery_requests": 1,
        "paper_runner_acquisition_max_provider_requests": 6,
        "paper_runner_acquisition_max_http_attempts": 8,
        "paper_runner_acquisition_max_seconds": 30,
    }
    return runner_settings(**{**defaults, **overrides})


def full_acquiring_settings(**overrides):
    """Every specialist this runtime can claim, plus the acquisition stage."""
    defaults: dict[str, object] = {
        # Both acquired markets are read and judged — ANCHOR needs the payment
        # asset's own price, so its market has to be recorded too — and exactly
        # one of them may become a case. Intake takes candidates in its own
        # deterministic order, oldest observation and then canonical pair first,
        # so the market being traded is the one that opens and the payment
        # asset's pool is refused as beyond the cycle limit.
        "paper_runner_max_candidates": 2,
        "paper_runner_max_new_cases": 1,
        "orbit_worker_enabled": True,
        "atlas_worker_enabled": True,
        "signal_worker_enabled": True,
        "signal_social_provider": "neynar",
        "neynar_api_key": "unused-because-the-source-is-supplied",
        "vector_worker_enabled": True,
        "fuse_worker_enabled": True,
        "pulse_worker_enabled": True,
        "anchor_worker_enabled": True,
        "evm_runtime_enabled": True,
        "reasoning_provider": "anthropic",
    }
    return acquiring_settings(**{**defaults, **overrides})


def acquiring_ports(now, model, provider, **overrides):
    """The specialist fixtures, plus the one HTTP boundary the provider reaches."""
    from tests.anchor.conftest import source as quote_source
    from tests.atlas.conftest import StubHolders

    defaults: dict[str, object] = {
        "holders": StubHolders(dispersed_holders(now)),
        "quotes": quote_source(now, reference_price=SPOT),
        "market_http": provider.transport(),
    }
    return specialist_ports(now, model, **{**defaults, **overrides})


async def observations(sessions, pair_id=None):
    async with sessions() as session:
        statement = select(MarketObservationRow)
        if pair_id is not None:
            statement = statement.where(MarketObservationRow.pair_id == pair_id)
        return (await session.scalars(statement.order_by(MarketObservationRow.observed_at))).all()


def entries(summary, need=None, outcome=None):
    found = summary.acquisition.markets
    if need is not None:
        found = [item for item in found if item.need == need.value]
    if outcome is not None:
        found = [item for item in found if item.outcome == outcome.value]
    return list(found)


# --------------------------------------------------------------- the control case


async def test_a_provider_response_becomes_a_recorded_market_a_case_and_a_fill(risk_db, now, trace):
    """Provider → adapter → recorder → reader → intake → specialists → SENTINEL → fill.

    Two explicit runs, because that is what the contract produces: the first
    carries the case as far as a trigger that has not happened yet, and the
    second picks it up once the provider reports a market that has moved.
    Nothing waits in between — the pass ends, and the task table holds the work.

    Not one market row is written by this test. The rows the whole chain reads
    are the ones the acquisition stage recorded from the response bytes.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    # PULSE re-checks on its own interval and SENTINEL refuses sources older
    # than thirty seconds, so a trigger found on a rescheduled check arrives
    # with on-chain evidence the risk engine has already stopped accepting.
    # That interaction between two existing policies is a limit of this system,
    # recorded in the phase notes rather than worked around here.
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)

    first = await run(sessions, settings, now, ports=acquiring_ports(now, model, provider))

    assert first.exit_code is ExitCode.COMPLETED, first
    # The stage really did ask, and really did write.
    assert first.acquisition.enabled
    assert first.acquisition.stop == AcquisitionStop.COMPLETED.value, first.acquisition
    assert first.acquisition.recorded == 2, first.acquisition
    assert first.acquisition.provider_requests >= 2
    assert {item.need for item in first.acquisition.markets} == {
        AcquisitionNeed.NEW_CANDIDATE.value
    }
    assert len(await observations(sessions, PAIR_ID)) == 1
    assert len(await observations(sessions, PAYMENT_PAIR_ID)) == 1

    case = await traded_case(sessions)
    assert case is not None, first
    for role in (AgentRole.ORBIT, AgentRole.ATLAS, AgentRole.SIGNAL, AgentRole.VECTOR):
        assert await succeeded(sessions, role, case), (role, first)
    assert first.fills == 0, "nothing may fill before a trigger"

    # The market moves through the level the setup named, and the provider is
    # what says so. Twenty seconds later: long enough for a new observation to
    # be its own event, short enough that the facts the first run established
    # are still inside SENTINEL's own bound.
    later = now + timedelta(seconds=20)
    moved = MarketProvider(
        discovery=[],
        targeted=[traded(SPOT * Decimal("1.20")), payment()],
    )
    second = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, moved),
    )

    assert second.exit_code is ExitCode.COMPLETED, second
    # The case's own market and its payment asset, both asked for by locator.
    assert second.acquisition.recorded == 2, second.acquisition
    assert len(moved.multi_requests) == 1, moved.paths
    assert {item.need for item in second.acquisition.markets} == {
        AcquisitionNeed.CASE_MARKET.value,
        AcquisitionNeed.QUOTE_ASSET.value,
    }
    assert await succeeded(sessions, AgentRole.PULSE, case), second
    assert await succeeded(sessions, AgentRole.ANCHOR, case), second

    progress = next(item for item in second.cases if item.trade_case_id == case)
    assert progress.risk_outcome == "APPROVE", (progress, second)
    assert progress.execution_id is not None
    assert second.fills == 1, second

    async with sessions() as session:
        fills = (await session.scalars(select(ExecutionRow))).all()
        held = await session.scalar(select(PositionRow))
    assert len(fills) == 1
    assert held is not None and held.quantity > 0


async def test_an_existing_case_outside_discovery_is_updated_by_locator(risk_db, now, trace):
    """A market that no longer appears as a new pool is still observed again.

    The whole reason the targeted read exists. Discovery answers "what is new on
    this chain", and a market this system already holds a case in stops being
    new almost immediately — which is exactly when its reading needs renewing.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))
    case = await traded_case(sessions)
    assert case is not None

    later = now + timedelta(seconds=20)
    # Discovery now shows a different pool entirely. The case's market is not on
    # it, and is fetched by its recorded locator instead.
    other = "0x" + "ab" * 20
    provider = MarketProvider(
        discovery=[pool(other, base="0x" + "cd" * 20, quote="0x" + "ef" * 20, price="5")],
        targeted=[traded(SPOT * Decimal("1.20")), payment()],
    )

    summary = await run(sessions, settings, later, ports=acquiring_ports(later, model, provider))

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert len(provider.multi_requests) == 1, provider.paths
    assert POOL in provider.multi_requests[0]
    recorded = [item.pair_id for item in entries(summary, outcome=AcquisitionOutcome.RECORDED)]
    assert PAIR_ID in recorded, summary.acquisition
    rows = await observations(sessions, PAIR_ID)
    assert len(rows) == 2, "the market was observed again, as its own event"
    assert rows[-1].observed_at > rows[0].observed_at


async def test_one_market_wanted_twice_is_observed_once_and_replayed(risk_db, now, trace):
    """Two needs, one observation, and the second reported as the replay it is."""
    from src.data.tables import PositionRow as Row

    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    # A holding in the very market the case is about.
    await hold(sessions, now, trace, pair_id=PAIR_ID)

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.05")), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    asked = provider.multi_requests[0]
    assert asked.count(POOL) == 1, "one market is one request, however many things want it"
    replayed = entries(summary, outcome=AcquisitionOutcome.UNCHANGED)
    assert [item.pair_id for item in replayed] == [PAIR_ID], summary.acquisition
    assert summary.acquisition.unchanged == 1
    async with sessions() as session:
        written = await session.scalar(
            select(func.count())
            .select_from(MarketObservationRow)
            .where(MarketObservationRow.pair_id == PAIR_ID)
        )
    assert written == 2, "the replay wrote nothing new"
    assert (await session_count(sessions, Row)) == 1


async def session_count(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def hold(sessions, now, trace, *, pair_id, asset_id=None, quantity="10"):
    """One open PAPER holding, written through the ledger's own repository.

    Stands for a position an earlier cycle acquired. What matters here is that
    it names its own market, because that is what the acquisition looks up and
    what the valuation prices it from.
    """
    from src.core.models import Position
    from src.data.repository import save_position

    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id=asset_id or f"{CHAIN}:{NETWORK}:" + "0x" + "a1" * 20,
                market_pair_id=pair_id,
                market_chain=CHAIN,
                market_network=NETWORK,
                market_provider="geckoterminal",
                quantity=Decimal(quantity),
                cost_basis_usd=Decimal("100"),
                created_at=now,
                updated_at=now,
            ),
        )


# ------------------------------------------------------------------ what it refuses


async def test_an_unsupported_chain_is_refused_and_never_served_from_another(risk_db, now, trace):
    """A market on a chain this run has not configured is not fetched at all."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT), payment()])
    # The same open case, and a run configured for a different chain entirely.
    summary = await run(
        sessions,
        full_acquiring_settings(
            market_chains="bsc",
            paper_runner_acquisition_max_discovery_requests=0,
            vector_worker_enabled=False,
            atlas_worker_enabled=False,
            evm_runtime_enabled=False,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert provider.multi_requests == [], "no request may be made for another chain"
    refused = entries(summary, outcome=AcquisitionOutcome.REFUSED)
    assert {item.reason for item in refused} == {"CHAIN_NOT_CONFIGURED"}, summary.acquisition
    assert {item.pair_id for item in refused} >= {PAIR_ID}


async def test_a_market_the_provider_does_not_return_is_left_exactly_as_it_was(risk_db, now, trace):
    """Asked about, unanswered, and never filled in with something older."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    later = now + timedelta(seconds=20)
    # The provider answers about the payment asset and says nothing about the
    # traded pool.
    provider = MarketProvider(discovery=[], targeted=[payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    refused = entries(summary, outcome=AcquisitionOutcome.REFUSED)
    assert [(item.pair_id, item.reason) for item in refused] == [
        (PAIR_ID, "MARKET_NOT_RETURNED")
    ], summary.acquisition
    assert len(await observations(sessions, PAIR_ID)) == 1, "nothing new was written"
    assert summary.fills == 0


async def test_an_unavailable_new_reading_does_not_fall_back_to_the_usable_old_one(
    risk_db, now, trace
):
    """Recording a reading the provider could not price hides the older one.

    The existing market contract, reached through the new path: a market whose
    newest event is unavailable is not readable at all, and the previous
    available observation is *not* what answers instead. Acquiring is therefore
    not the same as improving, which is the honest behaviour.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    first = await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))
    assert first.acquisition.recorded == 2

    later = now + timedelta(seconds=20)
    unpriced = MarketProvider(
        discovery=[],
        targeted=[pool(POOL, base="0x" + "a1" * 20, quote="0x" + "b2" * 20, price=None), payment()],
    )
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, unpriced),
    )

    assert summary.acquisition.recorded == 2, summary.acquisition
    from src.markets.reader import MarketReader

    reader = MarketReader(sessions)
    assert await reader.latest(PAIR_ID) is None, "an unavailable event is not a readable market"
    assert summary.fills == 0


async def test_reading_an_old_observation_again_does_not_make_it_fresh(risk_db, now, trace):
    """Age is the source's own, and no later run can move it.

    The acquisition records at the instant the fetch completed. A later run that
    acquires nothing finds exactly that instant, and the market ages out under
    the same rule it always did.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, provider))
    rows = await observations(sessions, PAIR_ID)
    assert len(rows) == 1
    stored = rows[0].observed_at

    from src.core.clock import FixedClock
    from src.markets.reader import MarketReader

    much_later = now + timedelta(minutes=10)
    reader = MarketReader(sessions, clock=FixedClock(much_later))
    assert await reader.latest(PAIR_ID) is None

    # And the row itself never moved.
    assert (await observations(sessions, PAIR_ID))[0].observed_at == stored


# ------------------------------------------------------------------------- budgets


async def test_the_market_budget_is_applied_before_the_request(risk_db, now, trace):
    """Fewer pools are asked for — not fetched and then thrown away."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(
        paper_runner_acquisition_max_markets=1,
        pulse_worker_enabled=False,
        anchor_worker_enabled=False,
    )

    summary = await run(sessions, settings, now, ports=acquiring_ports(now, model, provider))

    request = next(item for item in provider.requests if item.url.path.endswith("new_pools"))
    assert request.url.params["page"] == "1"
    assert summary.acquisition.recorded == 1, summary.acquisition
    assert len(await observations(sessions)) == 1, "nothing fetched was discarded"


async def test_a_full_market_budget_leaves_the_rest_visibly_unattempted(risk_db, now, trace):
    """A market the budget did not reach says so, rather than going silent."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_markets=1,
            paper_runner_acquisition_max_discovery_requests=0,
        ),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert summary.acquisition.stop == AcquisitionStop.MARKET_BUDGET_REACHED.value
    assert summary.acquisition.not_attempted == 1, summary.acquisition
    left = entries(summary, outcome=AcquisitionOutcome.NOT_ATTEMPTED)
    assert [(item.pair_id, item.reason) for item in left] == [
        (PAYMENT_PAIR_ID, "MARKET_BUDGET_REACHED")
    ], summary.acquisition
    assert len(provider.multi_requests[0].rsplit("/", 1)[-1].split(",")) == 1


async def test_the_provider_request_budget_is_the_lower_of_the_two(risk_db, now, trace):
    """A run may tighten what the provider configuration allows, never loosen it.

    The transport is built with the smaller of the two numbers, so the budget is
    the provider's own mechanism rather than a second counter beside it — which
    is what makes it cover retries and helper queries too.
    """
    from src.core.clock import FixedClock
    from src.markets.reader import MarketReader
    from src.runner.acquisition import BoundedMarketAcquisition
    from src.runner.composition import acquisition_limits_from_settings

    _, sessions = risk_db
    settings = acquiring_settings(
        geckoterminal_max_requests=2,
        paper_runner_acquisition_max_provider_requests=9,
        geckoterminal_max_http_attempts=9,
        paper_runner_acquisition_max_http_attempts=3,
    )
    stage = BoundedMarketAcquisition(
        settings,
        sessions,
        MarketReader(sessions, clock=FixedClock(now)),
        acquisition_limits_from_settings(settings),
        clock=FixedClock(now),
    )

    bounded = stage._settings
    assert bounded.geckoterminal_max_requests == 2, "the provider's own lower bound wins"
    assert bounded.geckoterminal_max_http_attempts == 3, "and so does the run's"


async def test_the_request_budget_stops_the_stage_including_helper_queries(risk_db, now, trace):
    """Network resolution is a request too, and it is counted like one."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    # One logical request in total: the network directory consumes it, and the
    # discovery read is refused by the transport's own budget rather than made.
    settings = full_acquiring_settings(
        paper_runner_acquisition_max_provider_requests=1,
        pulse_worker_enabled=False,
        anchor_worker_enabled=False,
    )

    summary = await run(sessions, settings, now, ports=acquiring_ports(now, model, provider))

    assert provider.discovery_requests == [], provider.paths
    assert summary.acquisition.recorded == 0, summary.acquisition
    assert summary.acquisition.stop == AcquisitionStop.PROVIDER_FAILED.value
    failed = entries(summary, outcome=AcquisitionOutcome.FAILED)
    assert [item.reason for item in failed] == ["REQUEST_BUDGET_EXHAUSTED"], summary.acquisition
    assert summary.exit_code is ExitCode.COMPLETED, "a bounded stage is not a broken run"


async def test_an_exhausted_time_budget_stops_before_the_request(risk_db, now, trace):
    """A stage with no time left asks nobody anything."""
    from src.core.clock import FixedClock
    from src.markets.reader import MarketReader
    from src.runner.acquisition import BoundedMarketAcquisition
    from src.runner.composition import acquisition_limits_from_settings
    from src.runner.service import Deadline

    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT)])
    settings = acquiring_settings()
    stage = BoundedMarketAcquisition(
        settings,
        sessions,
        MarketReader(sessions, clock=FixedClock(now)),
        acquisition_limits_from_settings(settings),
        pause=_running(),
        clock=FixedClock(now),
        http=provider.transport(),
    )

    reading = await stage.execute(Deadline(-1))

    assert reading.stop == AcquisitionStop.TIME_BUDGET_REACHED.value
    assert provider.paths == []
    assert reading.recorded == 0


def _running():
    class Running:
        async def system_paused(self) -> bool:
            return False

        async def locked_paused(self, session) -> bool:
            return False

    return Running()


# ------------------------------------------------------------------ stops and faults


async def test_a_paused_system_is_not_asked_to_spend_a_provider_request(risk_db, now, trace):
    """The stop is read before anything is asked, and nothing is asked."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    from tests.runner.conftest import set_account

    await set_account(sessions, paused=True)

    summary = await run(
        sessions,
        full_acquiring_settings(),
        now,
        ports=acquiring_ports(now, model, provider),
    )

    assert provider.paths == [], "a stopped system calls nobody"
    assert summary.acquisition.stop == AcquisitionStop.SYSTEM_STOPPED.value
    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert summary.cases_opened == 0
    assert summary.fills == 0


async def test_an_unreadable_stop_is_reported_as_a_fault_and_stops_the_pass(risk_db, now, trace):
    """Not knowing whether the system is stopped is not permission to spend."""
    from src.core.clock import FixedClock
    from src.runner.composition import build_stack
    from src.runner.service import BoundedPaperRun

    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT)])
    ports = acquiring_ports(now, model, provider)
    stack = build_stack(full_acquiring_settings(), sessions, ports=ports, clock=FixedClock(now))
    # No stop source at all: the deployment cannot answer a safety question.
    object.__setattr__(stack.acquisition, "_pause", None)

    summary = await BoundedPaperRun(stack).execute()

    assert provider.paths == []
    assert summary.acquisition.stop == AcquisitionStop.SYSTEM_STOP_UNREADABLE.value
    assert summary.stop is RunStop.SYSTEM_STOPPED
    assert summary.errors == ("SYSTEM_STOP_UNREADABLE",)
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE


async def test_a_provider_failure_keeps_what_was_already_recorded(risk_db, now, trace):
    """Confirmed recordings survive a later failure, and the summary says both."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    later = now + timedelta(seconds=20)
    broken = MarketProvider(discovery=[], targeted=[traded(SPOT)], status=503)
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, broken),
    )

    assert summary.acquisition.stop == AcquisitionStop.PROVIDER_FAILED.value
    assert summary.acquisition.failed >= 1, summary.acquisition
    assert summary.acquisition.recorded == 0
    # The earlier pass's observations are untouched, and the run still ran:
    # a provider that failed says nothing about a market.
    assert len(await observations(sessions, PAIR_ID)) == 1
    assert summary.exit_code is ExitCode.COMPLETED, summary


async def test_an_unknown_recording_outcome_stops_before_any_trading_stage(risk_db, now, trace):
    """A write that may or may not have committed ends the pass, conservatively."""
    from src.core.clock import FixedClock
    from src.runner.composition import build_stack
    from src.runner.service import BoundedPaperRun

    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])
    stack = build_stack(
        full_acquiring_settings(),
        sessions,
        ports=acquiring_ports(now, model, provider),
        clock=FixedClock(now),
    )

    async def never_answers(*arguments, **keywords):
        import asyncio

        await asyncio.sleep(3600)

    from src.runner import acquisition as module

    original = module.record_pair_reporting
    module.record_pair_reporting = never_answers
    try:
        summary = await BoundedPaperRun(stack).execute()
    finally:
        module.record_pair_reporting = original

    assert summary.acquisition.stop == AcquisitionStop.OUTCOME_UNKNOWN.value, summary.acquisition
    assert summary.acquisition.unknown == 1
    assert summary.stop is RunStop.ACQUISITION_OUTCOME_UNKNOWN
    assert summary.errors == ("ACQUISITION_OUTCOME_UNKNOWN",)
    assert summary.cases_opened == 0, "no case may be opened over data of unknown provenance"
    assert summary.fills == 0
    async with sessions() as session:
        assert (await session.scalar(select(func.count()).select_from(TradeCaseRow))) == 0


# --------------------------------------------------------------- positions and replay


async def test_a_foreign_open_position_is_valued_from_its_own_acquired_market(risk_db, now, trace):
    """A holding in another market is priced from the market it was acquired in.

    Its market is the first thing on the acquisition list, ahead of the case
    this run is about: a portfolio that cannot be marked makes SENTINEL refuse
    every case, so the holding's data is a precondition for all of them.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    # A holding in the payment asset's market, from some earlier cycle.
    await hold(
        sessions,
        now,
        trace,
        pair_id=PAYMENT_PAIR_ID,
        asset_id=f"{CHAIN}:{NETWORK}:" + "0x" + "b2" * 20,
    )

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.20")), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    held = entries(summary, need=AcquisitionNeed.POSITION_VALUATION)
    assert [(item.pair_id, item.outcome) for item in held] == [
        (PAYMENT_PAIR_ID, AcquisitionOutcome.RECORDED.value)
    ], summary.acquisition
    # The case wanted the same market as its payment asset. One request, and the
    # second need answered by that same reading.
    assert PAYMENT_PAIR_ID in {
        item.pair_id
        for item in entries(
            summary, need=AcquisitionNeed.QUOTE_ASSET, outcome=AcquisitionOutcome.UNCHANGED
        )
    }, summary.acquisition
    assert summary.fills == 1, summary
    assert len(await observations(sessions, PAYMENT_PAIR_ID)) == 2


async def test_a_holding_whose_market_cannot_be_acquired_prevents_every_fill(risk_db, now, trace):
    """A source that is needed and missing refuses the case rather than guessing.

    The market this position names was never recorded, so there is no identity
    to ask the provider about — reading its address out of its own identifier
    would be inventing coordinates — and the portfolio therefore cannot be
    marked. SENTINEL refuses, terminally, and nothing fills.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    unknown_market = f"{CHAIN}:{NETWORK}:contract_address:" + "0x" + "99" * 20
    await hold(
        sessions,
        now,
        trace,
        pair_id=unknown_market,
        asset_id=f"{CHAIN}:{NETWORK}:" + "0x" + "77" * 20,
    )

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.20")), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    assert provider.multi_requests and unknown_market not in provider.multi_requests[0]
    refused = entries(summary, need=AcquisitionNeed.POSITION_VALUATION)
    assert [(item.pair_id, item.reason) for item in refused] == [
        (unknown_market, "MARKET_NEVER_RECORDED")
    ], summary.acquisition
    assert summary.fills == 0
    progress = [item for item in summary.cases if item.risk_refusal is not None]
    assert progress and progress[0].risk_refusal == "PORTFOLIO_MARKS_UNAVAILABLE", summary.cases


async def test_a_historical_replay_needs_no_new_acquisition(risk_db, now, trace):
    """A stored verdict replays without the market layer being asked anything."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    await run(
        sessions,
        full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False),
        now,
        ports=acquiring_ports(now, model, opening),
    )
    later = now + timedelta(seconds=20)
    moved = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.20")), payment()])
    filled = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, moved),
    )
    assert filled.fills == 1, filled

    # The same case again, with acquisition switched off entirely.
    from tests.runner.conftest import run as plain_run

    again = await plain_run(
        sessions,
        runner_settings(),
        later + timedelta(seconds=1),
        ports=RunnerPorts(),
    )

    assert again.acquisition is not None and not again.acquisition.enabled
    assert again.acquisition.stop == AcquisitionStop.NOT_ENABLED.value
    assert len(await observations(sessions)) == 4, "nothing new was recorded"
    assert len(await executions(sessions)) == 1


async def executions(sessions):
    async with sessions() as session:
        return (await session.scalars(select(ExecutionRow))).all()


def test_the_api_starts_neither_acquisition_nor_a_run():
    """Booting the web process reads none of this and starts nothing."""
    from pathlib import Path

    source = Path("src/api").rglob("*.py")
    text = "\n".join(item.read_text() for item in source)
    assert "paper_runner_market_acquisition_enabled" not in text
    assert "BoundedMarketAcquisition" not in text
    assert "BoundedPaperRun" not in text


def test_acquisition_cannot_be_configured_without_a_run_or_a_provider():
    """Two configurations that describe something that can never happen."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        runner_settings(paper_runner_market_acquisition_enabled=True, market_provider="fixture")
    with pytest.raises(ValidationError):
        runner_settings(
            paper_runner_enabled=False,
            trading_mode="OBSERVE",
            paper_runner_market_acquisition_enabled=True,
            market_provider="geckoterminal",
        )


# ------------------------------------------------- partial work, and what survives


async def test_confirmed_recordings_survive_a_later_failure(risk_db, now, trace):
    """Some markets recorded, then an error. The summary states both, exactly.

    The targeted reads succeed and the discovery read that follows fails. What
    was durably written stays written — nothing is rolled back to make the
    account tidy — and the failure is reported beside it rather than instead
    of it.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    later = now + timedelta(seconds=20)
    partial = MarketProvider(
        targeted=[traded(SPOT * Decimal("1.20")), payment()], discovery_status=503
    )
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=1),
        later,
        ports=acquiring_ports(later, model, partial),
    )

    assert summary.acquisition.recorded == 2, summary.acquisition
    assert summary.acquisition.failed == 1, summary.acquisition
    assert summary.acquisition.stop == AcquisitionStop.PROVIDER_FAILED.value
    failed = entries(summary, outcome=AcquisitionOutcome.FAILED)
    assert [(item.pair_id, item.need) for item in failed] == [
        ("*", AcquisitionNeed.NEW_CANDIDATE.value)
    ]
    # Both observations are durable, and the run went on to do its work with
    # them: a failed discovery says nothing about a market already observed.
    assert len(await observations(sessions, PAIR_ID)) == 2
    assert len(await observations(sessions, PAYMENT_PAIR_ID)) == 2
    assert summary.exit_code is ExitCode.COMPLETED, summary


async def test_a_holding_whose_recorded_market_disagrees_is_refused(risk_db, now, trace):
    """A position naming one provider and a market recorded by another.

    Two providers observing one pool are two sources. Asking this provider
    about a market somebody else recorded would attribute a reading to the
    wrong one, so the identity is refused instead.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False)
    await run(sessions, settings, now, ports=acquiring_ports(now, model, opening))

    from sqlalchemy import update

    from src.data.tables import PositionRow as Row

    await hold(
        sessions,
        now,
        trace,
        pair_id=PAYMENT_PAIR_ID,
        asset_id=f"{CHAIN}:{NETWORK}:" + "0x" + "b2" * 20,
    )
    async with sessions.begin() as session:
        await session.execute(update(Row).values(market_provider="somebody-else"))

    later = now + timedelta(seconds=20)
    provider = MarketProvider(discovery=[], targeted=[traded(SPOT), payment()])
    summary = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=acquiring_ports(later, model, provider),
    )

    refused = entries(summary, need=AcquisitionNeed.POSITION_VALUATION)
    assert [(item.pair_id, item.reason) for item in refused] == [
        (PAYMENT_PAIR_ID, "MARKET_IDENTITY_MISMATCH")
    ], summary.acquisition


# --------------------------------------------- the wait, and the 2N-B refresh beside it


async def test_a_pulse_wait_a_new_observation_and_an_evidence_refresh_still_fill(
    risk_db, now, trace
):
    """The whole current contract, with the market data coming from a provider.

    The first pass leaves PULSE waiting. Ninety-five seconds later the monitor
    is due again, the acquisition observes both markets anew, the trigger is
    found — and the on-chain reading the first pass established has by then aged
    past SENTINEL's own bound, so the existing source-refresh contract orders a
    new observation of it before the case's one risk request is spent.

    Two mechanisms, deliberately kept apart: the market layer is refreshed by
    acquiring an observation, and evidence is refreshed by asking the task that
    produces it to run again. Neither is the other, and this proves they compose.
    """
    from tests.refresh.conftest import ATLAS_SOURCE, RECHECK, ports_at, refreshes, traded_case
    from tests.refresh.test_refresh import progress_for, waited

    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    settings = full_acquiring_settings()
    first = await run(
        sessions,
        settings,
        now,
        ports=ports_at(now, model, market_http=opening.transport()),
    )

    assert first.fills == 0, first
    await waited(first, sessions)

    later = now + RECHECK
    moved = MarketProvider(discovery=[], targeted=[traded(SPOT * Decimal("1.20")), payment()])
    second = await run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports=ports_at(later, model, market_http=moved.transport()),
    )

    # The markets were observed again, by locator, in this pass.
    assert second.acquisition.recorded == 2, second.acquisition
    assert len(await observations(sessions, PAIR_ID)) == 2

    progress = progress_for(second, await traded_case(sessions))
    assert refreshes(progress).get(ATLAS_SOURCE) == "ORDERED", progress
    assert progress.risk_outcome == "APPROVE", (progress, second)
    assert second.fills == 1, second


async def test_a_stage_this_configuration_cannot_perform_ends_the_pass(risk_db, now, trace):
    """Switched on, not performable, and never silently skipped.

    Two chains enabled and a provider chain limit of one. Nothing can be asked,
    so nothing is — and the run refuses to carry on trading over whatever
    happened to be recorded while reporting an acquisition that never ran.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(discovery=[traded(SPOT), payment()])

    summary = await run(
        sessions,
        full_acquiring_settings(
            market_chains="robinhood,bsc",
            geckoterminal_max_chains=1,
            atlas_worker_enabled=False,
            vector_worker_enabled=False,
            evm_runtime_enabled=False,
        ),
        now,
        ports=acquiring_ports(now, model, provider),
    )

    assert provider.paths == []
    assert summary.acquisition.stop == AcquisitionStop.CONFIGURATION_REFUSED.value
    assert summary.errors == ("ACQUISITION_NOT_CONFIGURED",)
    assert summary.cases_opened == 0
    assert summary.exit_code is ExitCode.TECHNICAL_FAILURE


async def test_a_network_the_provider_does_not_publish_is_refused_not_substituted(
    risk_db, now, trace
):
    """The directory cannot verify the chain, so no pool read is attempted.

    A bounded scan that did not find a network is not proof that it does not
    exist, and it is certainly not permission to read a different one. The stage
    reports the provider's own code and the run goes on without having traded
    over somebody else's chain.
    """
    _, sessions = risk_db
    model = ScriptedSpecialists()
    provider = MarketProvider(
        discovery=[traded(SPOT), payment()],
        # The published network list mentions every chain but this run's.
        networks={
            "data": [
                {
                    "id": "bsc",
                    "type": "network",
                    "attributes": {
                        "name": "BNB Chain",
                        "coingecko_asset_platform_id": "binance-smart-chain",
                    },
                }
            ],
            "links": {"next": None},
        },
    )

    summary = await run(
        sessions,
        full_acquiring_settings(),
        now,
        ports=acquiring_ports(now, model, provider),
    )

    assert provider.discovery_requests == [], "no pool read without a verified network"
    assert provider.multi_requests == []
    failed = entries(summary, outcome=AcquisitionOutcome.FAILED)
    assert [item.reason for item in failed] == ["UNSUPPORTED_NETWORK"], summary.acquisition
    assert summary.acquisition.recorded == 0
    assert summary.cases_opened == 0
    assert summary.fills == 0
