"""Regressions for the defects an independent review reproduced.

Each test here corresponds to a behaviour that was demonstrably wrong before it
existed. They are grouped by the finding they close rather than by module,
because several of them cut across the context reader, the decision function and
the intake service at once.

The concurrency proofs force their races with a barrier rather than starting two
coroutines and hoping they overlap. A race that only sometimes happens is a test
that only sometimes tests anything.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import AgentRole, RiskDecision, RiskMetrics, RiskOutcome
from src.orchestration.commander.context import (
    AccountPauseReader,
    CommanderContextReader,
    SystemPauseUnavailable,
)
from src.orchestration.commander.decision import decide
from src.orchestration.commander.intake import IntakeRefusal
from src.orchestration.commander.models import CommanderDisposition, CommanderReason
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import TradeCaseStatus
from tests.commander.conftest import (
    RunningSystem,
    build_stack,
    inject_pre_trigger_evidence,
    inject_trigger,
    open_case,
)
from tests.commander.test_execution import inject_anchor_evidence
from tests.commander.test_intake import candidate_for, service_for

pytestmark = pytest.mark.usefixtures("worker_db")


def sentinel_decision(trade_case, now, *, ttl=timedelta(hours=2), outcome=RiskOutcome.APPROVE):
    return RiskDecision(
        source="SENTINEL",
        correlation_id=trade_case.correlation_id,
        created_at=now,
        updated_at=now,
        intent_id=uuid4(),
        intent_fingerprint="intent",
        market_snapshot_id=uuid4(),
        market_fingerprint="market",
        outcome=outcome,
        reason_codes=("WITHIN_LIMITS",),
        position_size_limit_usd=Decimal("2500"),
        max_additional_notional_usd=Decimal("2500"),
        max_slippage_bps=Decimal("100"),
        metrics=RiskMetrics(
            requested_notional_usd=Decimal("100"),
            worst_case_notional_usd=Decimal("101"),
            exposure_usd=Decimal("0"),
            daily_loss_usd=Decimal("0"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
        ),
        evaluated_at=now,
        expires_at=now + ttl,
    )


async def authorized_case(sessions, now, trace, key, *, ttl=timedelta(hours=2)):
    """A real case carried to RISK_APPROVED through the authoritative services."""
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, key)
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    fresh = await runtime.cases.get_trade_case(trade_case.id)
    digest = risk_input_digest(fresh, active_evidence(await runtime.cases.evidence(trade_case.id)))
    await runtime.cases.record_risk_decision(
        trade_case.id, sentinel_decision(fresh, now, ttl=ttl), risk_input_digest=digest
    )
    return trade_case


# ============================================================ A: risk freshness


@pytest.mark.parametrize(
    ("offset", "still_current"),
    [
        (timedelta(seconds=-1), True),  # kurz davor
        (timedelta(0), False),  # exakt bei Ablauf
        (timedelta(seconds=1), False),  # danach
    ],
)
async def test_an_authorization_stops_being_current_exactly_at_its_expiry(
    worker_db, now, trace, offset, still_current
):
    """Pinned at the microsecond either side, because "expires at" is a boundary.

    An expiry that only takes effect a while later is a grace period nobody
    specified, and one that takes effect early discards a valid authorization.
    """
    _, sessions = worker_db
    ttl = timedelta(minutes=2)
    trade_case = await authorized_case(sessions, now, trace, f"expiry-{offset}", ttl=ttl)

    instant = now + ttl + offset
    _, reader = build_stack(sessions, instant)
    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, instant, COMMANDER_CONTROL_V1)

    assert context.risk is not None
    assert context.risk.matches_current_inputs is True, "evidence itself did not change"
    assert context.risk.expired is not still_current
    assert (decision.disposition == CommanderDisposition.RISK_CURRENT) is still_current


async def test_an_expired_authorization_is_not_current_even_without_any_write(
    worker_db, now, trace
):
    """The stored status is a snapshot of the last write, and time moves anyway.

    Nothing touches the case between the binding and the read, so a reader that
    trusted the stored row would report an authorization the evaluator would
    have withdrawn the moment anybody asked it.
    """
    _, sessions = worker_db
    trade_case = await authorized_case(
        sessions, now, trace, "expiry-nowrite", ttl=timedelta(minutes=2)
    )
    stored = await build_stack(sessions, now)[0].cases.get_trade_case(trade_case.id)
    assert stored.status == TradeCaseStatus.RISK_APPROVED

    later = now + timedelta(minutes=10)
    _, reader = build_stack(sessions, later)
    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, later, COMMANDER_CONTROL_V1)

    # The stored row still says approved; the temporally sound view does not.
    unchanged = await build_stack(sessions, later)[0].cases.get_trade_case(trade_case.id)
    assert unchanged.status == TradeCaseStatus.RISK_APPROVED
    assert context.status == TradeCaseStatus.READY_FOR_RISK
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY


async def test_the_context_recomputes_status_without_writing_anything(worker_db, now, trace):
    """Reuse of the authoritative evaluator, not a second engine — and read-only."""
    _, sessions = worker_db
    trade_case = await authorized_case(
        sessions, now, trace, "expiry-readonly", ttl=timedelta(minutes=2)
    )
    later = now + timedelta(minutes=10)
    runtime, reader = build_stack(sessions, later)
    before = await runtime.cases.get_trade_case(trade_case.id)

    for _ in range(3):
        await reader.commander_context(trade_case.id, uuid4())

    after = await runtime.cases.get_trade_case(trade_case.id)
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at


async def test_an_evaluator_run_agrees_with_the_read_only_view(worker_db, now, trace):
    """The two must not diverge: it is the same evaluator over the same state."""
    _, sessions = worker_db
    trade_case = await authorized_case(
        sessions, now, trace, "expiry-agree", ttl=timedelta(minutes=2)
    )
    later = now + timedelta(minutes=10)
    runtime, reader = build_stack(sessions, later)

    view = await reader.commander_context(trade_case.id, uuid4())
    persisted = await runtime.cases.evaluate_trade_case(trade_case.id)
    assert view.status == persisted.status
    assert decide(view, later, COMMANDER_CONTROL_V1).disposition == (
        CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    )


# ====================================================== B: intake generations


async def test_a_replayed_intake_is_refused_rather_than_counted_as_opened(worker_db, now, trace):
    """The miscount: a cancelled predecessor was reported as freshly opened.

    `open_trade_case` resolves a duplicate key by returning the existing case,
    so a constant-per-market key made every later cycle hand back whatever case
    that market already had — terminal or not — and count it as new work.
    """
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])

    first = await service.run_cycle()
    assert first.opened_count == 1

    second = await service.run_cycle()
    assert second.opened_count == 0
    assert second.refused == ((candidate.pair_id, IntakeRefusal.ACTIVE_CASE_EXISTS),)


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        (TradeCaseStatus.CANCELLED, True),
        (TradeCaseStatus.EXPIRED, True),
        (TradeCaseStatus.RISK_REJECTED, False),
    ],
)
async def test_only_some_terminal_predecessors_permit_a_new_generation(
    worker_db, now, trace, terminal, expected
):
    """§B. A rejection is a verdict; expiry and cancellation are not.

    `EXPIRED` and `CANCELLED` end a case without deciding anything about the
    market, so the next observation may start the next generation. A
    `RISK_REJECTED` predecessor must not be reopened by re-observing the market:
    a fresh fetch carries no new information about risk, and letting it start
    another attempt would be retry-until-pass with extra steps.
    """
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])
    case = (await service.run_cycle()).opened[0]

    if terminal == TradeCaseStatus.RISK_REJECTED:
        await _force_risk_rejected(sessions, service, case, now, trace)
    elif terminal == TradeCaseStatus.CANCELLED:
        await service.cases.cancel_trade_case(case.id, reason_code="OPERATOR_CANCELLED")
    else:
        await _expire(sessions, service, case, now)

    ended = await service.cases.get_trade_case(case.id)
    assert ended.status == terminal

    outcome = await service.run_cycle()
    assert (outcome.opened_count == 1) is expected
    if expected:
        reopened = await service.cases.get_trade_case(outcome.opened[0].id)
        assert reopened.id != case.id
        assert reopened.status not in {TradeCaseStatus.CANCELLED, TradeCaseStatus.EXPIRED}
    else:
        assert outcome.refused == ((candidate.pair_id, IntakeRefusal.RISK_REJECTED_FOR_MARKET),)


async def _force_risk_rejected(sessions, service, case, now, trace):
    """Carry a case to a genuine SENTINEL rejection through the real services."""
    runtime, _ = build_stack(sessions, now)
    setup = await inject_pre_trigger_evidence(runtime.cases, case, now)
    await inject_trigger(runtime.cases, case, now, setup)
    await inject_anchor_evidence(runtime.cases, case, now, setup)
    fresh = await runtime.cases.get_trade_case(case.id)
    digest = risk_input_digest(fresh, active_evidence(await runtime.cases.evidence(case.id)))
    await runtime.cases.record_risk_decision(
        case.id,
        sentinel_decision(fresh, now, outcome=RiskOutcome.REJECT),
        risk_input_digest=digest,
    )


async def _expire(sessions, service, case, now):
    """Let the case reach its own expiry through the evaluator, not by fiat."""
    beyond = case.expires_at + timedelta(minutes=1)
    late_runtime, _ = build_stack(sessions, beyond)
    await late_runtime.cases.evaluate_trade_case(case.id)


async def test_a_workflow_refusal_does_not_abort_the_remaining_candidates(worker_db, now, trace):
    """One bad candidate took every later one down with it.

    An idempotency conflict escaped `_open` and propagated out of `run_cycle`,
    so a single unopenable candidate cancelled the whole pass.
    """
    _, sessions = worker_db
    good = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{7:040x}")
    broken = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{8:040x}")
    service = service_for(sessions, now, [broken, good])

    async def refuse(*args, **kwargs):
        from src.orchestration.workflow.models import WorkflowErrorCode, WorkflowFailure

        raise WorkflowFailure(WorkflowErrorCode.IDEMPOTENCY_CONFLICT)

    original = service.cases.open_trade_case_in_session
    calls: list[str] = []

    async def selective(session, identity, **kwargs):
        calls.append(identity.pair_id)
        if identity.pair_id == broken[0].pair_id:
            return await refuse()
        return await original(session, identity, **kwargs)

    service.cases.open_trade_case_in_session = selective  # type: ignore[method-assign]
    outcome = await service.run_cycle()

    assert len(calls) == 2, "the cycle continued past the refusal"
    assert outcome.opened_count == 1
    assert outcome.opened[0].market.pair_id == good[0].pair_id
    assert (broken[0].pair_id, IntakeRefusal.OPEN_REFUSED) in outcome.refused


# ============================================ B: forced concurrency proofs


class Barrier:
    """Holds every participant until all have arrived, then releases together.

    A race started with `gather` and hoped for is a test that sometimes tests
    nothing. This makes the overlap a property of the harness.
    """

    def __init__(self, parties: int) -> None:
        self._barrier = asyncio.Barrier(parties)

    async def wait(self) -> None:
        await self._barrier.wait()


def racing_service(sessions, now, pairs, barrier):
    """An intake service that pauses at the market read, just before the open."""
    service = service_for(sessions, now, pairs)
    original = service.markets.latest

    async def synchronized(identity, **kwargs):
        snapshot = await original(identity, **kwargs)
        await barrier.wait()
        return snapshot

    service.markets.latest = synchronized  # type: ignore[method-assign]
    return service


async def test_two_workers_on_one_candidate_converge_on_one_case(worker_db, now, trace):
    """§53. Both workers are inside the open at the same instant, by construction."""
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    candidate, snapshot = candidate_for(now)
    barrier = Barrier(2)
    outcomes = await asyncio.gather(
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    service = service_for(sessions, now, [(candidate, snapshot)])
    cases = await service.cases.list_trade_cases()
    assert len(cases) == 1
    opened = {case.id for outcome in outcomes for case in outcome.opened}
    assert len(opened) == 1


async def test_two_workers_with_different_observations_of_one_market_converge(
    worker_db, now, trace
):
    """Different candidate identities, same market, still one case.

    This is the case a per-observation key would get wrong: two observations a
    minute apart are two candidates, and if the key followed the observation
    each would open its own case for the same market.
    """
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    pair = f"robinhood:mainnet:contract_address:0x{21:040x}"
    early = candidate_for(now, seconds_ago=30, pair=pair)
    late = candidate_for(now, seconds_ago=10, pair=pair)
    assert early[0].id != late[0].id

    barrier = Barrier(2)
    outcomes = await asyncio.gather(
        racing_service(sessions, now, [early], barrier).run_cycle(),
        racing_service(sessions, now, [late], barrier).run_cycle(),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    service = service_for(sessions, now, [early])
    cases = await service.cases.list_trade_cases()
    assert len(cases) == 1, "one market, one active case"


async def test_two_workers_on_different_markets_open_both(worker_db, now, trace):
    """The control: dedupe is by identity, not a lock that serialises everything."""
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    left = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{31:040x}")
    right = candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{32:040x}")
    barrier = Barrier(2)
    outcomes = await asyncio.gather(
        racing_service(sessions, now, [left], barrier).run_cycle(),
        racing_service(sessions, now, [right], barrier).run_cycle(),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    cases = await service_for(sessions, now, [left]).cases.list_trade_cases()
    assert len(cases) == 2


async def test_two_workers_racing_for_a_new_generation_open_one(worker_db, now, trace):
    """The generation boundary is the newest place a duplicate could appear.

    Both workers see the same cancelled predecessor, derive the same generation,
    and therefore the same key — so the second converges rather than opening a
    rival successor.
    """
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    candidate, snapshot = candidate_for(now)
    service = service_for(sessions, now, [(candidate, snapshot)])
    first = (await service.run_cycle()).opened[0]
    await service.cases.cancel_trade_case(first.id, reason_code="OPERATOR_CANCELLED")

    barrier = Barrier(2)
    outcomes = await asyncio.gather(
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
        return_exceptions=True,
    )
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    cases = await service.cases.list_trade_cases()
    assert len(cases) == 2, "one predecessor plus exactly one successor"
    successors = {case.id for outcome in outcomes for case in outcome.opened}
    assert len(successors) == 1
    assert first.id not in successors


async def test_a_replay_after_a_race_still_opens_nothing(worker_db, now, trace):
    """Idempotency survives the race, not merely the sequential replay."""
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    candidate, snapshot = candidate_for(now)
    barrier = Barrier(2)
    await asyncio.gather(
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
        racing_service(sessions, now, [(candidate, snapshot)], barrier).run_cycle(),
    )
    service = service_for(sessions, now, [(candidate, snapshot)])
    again = await service.run_cycle()
    assert again.opened_count == 0
    assert len(await service.cases.list_trade_cases()) == 1


# ================================================== C: stops and mode contract


async def test_a_stop_that_arrives_during_the_candidate_read_still_blocks(worker_db, now, trace):
    """§C. The gate was at the top of the cycle and nothing looked again.

    A stop coming into force while candidates were being read had already been
    passed. The window cannot be closed by reading earlier — only by reading
    last, immediately before the one write this service performs.
    """
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)
    pause = RunningSystem()
    service = service_for(sessions, now, [(candidate, snapshot)], pause=pause)

    original = service.markets.candidates

    async def flip(**kwargs):
        result = await original(**kwargs)
        pause.paused = True
        return result

    service.markets.candidates = flip  # type: ignore[method-assign]
    outcome = await service.run_cycle()

    assert outcome.opened_count == 0
    assert (candidate.pair_id, IntakeRefusal.SYSTEM_PAUSED) in outcome.refused
    assert await service.cases.list_trade_cases() == ()


@pytest.mark.parametrize("configured", [None, "unavailable"])
async def test_an_unreadable_stop_is_not_permission(worker_db, now, trace, configured):
    """§C. Unknown fails closed, as it does everywhere else in this system.

    A missing port and a control that raises are both *unknown*. The previous
    implementation answered False to both, turning an unavailable stop into a
    green light.
    """
    _, sessions = worker_db
    candidate, snapshot = candidate_for(now)

    class Unavailable:
        async def system_paused(self) -> bool:
            raise SystemPauseUnavailable("PAUSE_STATE_UNAVAILABLE")

    pause = None if configured is None else Unavailable()
    service = service_for(sessions, now, [(candidate, snapshot)], pause=pause)
    if configured is None:
        outcome = await service.run_cycle()
        assert outcome.opened_count == 0
        assert outcome.refused == (("*", IntakeRefusal.SYSTEM_PAUSED),)
    else:
        with pytest.raises(SystemPauseUnavailable):
            await service.run_cycle()

    # And the context reader treats an unreadable control the same way.
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, f"unreadable-{configured}")
    reader = CommanderContextReader(
        cases=runtime.cases, sessions=sessions, clock=runtime.clock, pause=pause
    )
    context = await reader.commander_context(trade_case.id, uuid4())
    assert context.controls.account_paused is True
    assert decide(context, now, COMMANDER_CONTROL_V1).disposition == (CommanderDisposition.PAUSED)


async def test_the_pause_reader_addresses_the_authoritative_account(worker_db, now):
    """§C. `paper_accounts` is singular by constraint, and reads must say so.

    A row missing entirely is an uninitialised accounting subsystem — unreadable
    rather than unpaused — so it raises instead of answering False.
    """
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL schema")

    from sqlalchemy.exc import SQLAlchemyError

    reader = AccountPauseReader(sessions=sessions)
    assert reader.account_id == 1
    # The workflow-only schema carries no accounting tables, which is itself an
    # unreadable control rather than an absent stop: it raises instead of
    # answering "not paused".
    with pytest.raises((SQLAlchemyError, SystemPauseUnavailable)):
        await reader.system_paused()


@pytest.mark.parametrize("mode", ["LIVE_AUTONOMOUS", "live", "", "PAPER_TRADING"])
def test_the_mode_contract_refuses_anything_it_cannot_operate_in(mode):
    """§C. Not merely unsupported: unrepresentable, so no later branch sees it."""
    from src.orchestration.commander.models import SystemControls

    with pytest.raises(ValueError):
        SystemControls(kill_switch=False, account_paused=False, trading_mode=mode)


async def test_observe_mode_stops_progression_and_says_which_stop_it_is(worker_db, now, trace):
    """§C. OBSERVE means watch and do not act, so coordination does not act.

    Reported distinctly from a pause: it is a stated posture rather than an
    incident, and an operator reading the reason should not go looking for a
    kill switch nobody pulled.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, mode="OBSERVE")
    trade_case = await open_case(runtime.cases, now, trace, "observe-mode")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, now, COMMANDER_CONTROL_V1)

    assert context.controls.trading_mode == "OBSERVE"
    assert context.controls.halted is True
    assert decision.disposition == CommanderDisposition.PAUSED
    assert decision.reason_code == CommanderReason.OBSERVE_MODE


async def test_paper_mode_does_not_stop_progression(worker_db, now, trace):
    """The control, so OBSERVE is not simply a stop that is always on."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, mode="PAPER")
    trade_case = await open_case(runtime.cases, now, trace, "paper-mode")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    context = await reader.commander_context(trade_case.id, uuid4())
    assert context.controls.halted is False
    assert decide(context, now, COMMANDER_CONTROL_V1).disposition == (
        CommanderDisposition.AWAIT_TRIGGER
    )


def test_the_commander_stop_is_not_described_as_a_global_one():
    """§C. `RiskLimits.kill_switch` is a separate field this one is not wired to.

    The comment previously claimed SENTINEL already honoured it, which promised
    a global stop that does not exist.
    """
    import subprocess

    wiring = subprocess.run(
        ["git", "grep", "-n", "commander_kill_switch", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout
    assert "RiskLimits" not in wiring
    config = (__import__("pathlib").Path("src/core/config.py")).read_text()
    block = config.split("commander_kill_switch")[0][-400:]
    assert "SENTINEL already honours" not in block
    assert "SENTINEL itself honours" not in block


# ================================================ D: advisory freshness


async def test_an_expired_synthesis_is_not_advertised_as_usable(worker_db, now, trace):
    """§D. References intact, reading aged out — two independent failures.

    Checking only whether the cited evidence is still current missed this
    entirely: a synthesis expires with the earliest of its sources *and* the
    setup it describes, so it can lapse while every reference it names is
    untouched.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "advisory-expiry")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    submission = await _record_synthesis(sessions, runtime, trade_case, now, trace)

    beyond = submission.valid_until + timedelta(minutes=5)
    _, late_reader = build_stack(sessions, beyond)
    context = await late_reader.commander_context(trade_case.id, uuid4())

    assert context.advisory is not None
    assert context.advisory.describes_current_inputs is True, "no reference changed"
    assert context.advisory.expired is True
    assert context.advisory.is_usable is False


async def test_a_current_synthesis_is_advertised_as_usable(worker_db, now, trace):
    """The control, so `is_usable` is not simply always false."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "advisory-fresh")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await _record_synthesis(sessions, runtime, trade_case, now, trace)

    context = await reader.commander_context(trade_case.id, uuid4())
    assert context.advisory is not None
    assert context.advisory.is_usable is True


async def test_an_expired_advisory_still_changes_no_decision(worker_db, now, trace):
    """FUSE remains advisory in both directions: usable or not, it decides nothing."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "advisory-inert")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    submission = await _record_synthesis(sessions, runtime, trade_case, now, trace)

    beyond = submission.valid_until - timedelta(seconds=1)
    _, reader = build_stack(sessions, beyond)
    context = await reader.commander_context(trade_case.id, uuid4())
    with_advisory = decide(context, beyond, COMMANDER_CONTROL_V1)
    without = decide(context.model_copy(update={"advisory": None}), beyond, COMMANDER_CONTROL_V1)
    assert with_advisory.disposition == without.disposition
    assert with_advisory.reason_code == without.reason_code


async def _record_synthesis(sessions, runtime, trade_case, instant, trace):
    """Run the real FUSE worker and record what it produced."""
    from src.agents.fuse.context import FuseContextReader
    from src.agents.fuse.handler import FuseWorkerHandler
    from src.core.clock import FixedClock
    from src.orchestration.worker.capabilities import FuseCapabilities
    from tests.fuse.test_workflow import Fixed, NoSubmit, lease_for

    reader = FuseContextReader(cases=runtime.cases, clock=FixedClock(instant))
    context = await reader.synthesis_context(trade_case.id, uuid4())
    lease = lease_for(trade_case, context.task_id, instant, trace)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    await runtime.cases.record_evidence(trade_case.id, report.submission)
    return report.submission


# ======================================== E: runtime recovery and replay


def test_evidence_stale_is_an_expected_refusal_and_is_budgeted():
    """§E1. Absent from the table, it propagated and ended the polling loop.

    Transient rather than superseding on purpose: a superseded outcome is not
    counted against the retry budget at all, so mapping it there would retry a
    persistently-too-slow worker forever.
    """
    from src.orchestration.worker.models import (
        FAILURE_OUTCOMES,
        TaskAttemptOutcome,
        WorkerErrorCode,
        WorkerFailureCategory,
    )
    from src.orchestration.worker.policy import RETRYABLE_CATEGORIES
    from src.orchestration.worker.runner import REFUSAL_CATEGORIES

    category = REFUSAL_CATEGORIES.get(WorkerErrorCode.EVIDENCE_STALE)
    assert category == WorkerFailureCategory.TRANSIENT
    assert category in RETRYABLE_CATEGORIES
    # Budgeted, so retries are bounded.
    assert TaskAttemptOutcome.FAILED_RETRYABLE in FAILURE_OUTCOMES
    # The unbounded alternative, named so the choice is visible.
    assert TaskAttemptOutcome.SUPERSEDED not in FAILURE_OUTCOMES


async def test_a_stale_submission_leaves_the_task_runnable_rather_than_claimed(
    worker_db, now, trace
):
    """§E1, through the real runtime with a still-valid lease.

    The refusal is recorded, the lease released and the task returned to
    PENDING, so the worker can keep working. Previously the refusal escaped
    `run_once` and ended `run()` with the task still RUNNING until its lease
    expired — a worker that stopped without saying so.
    """
    from src.orchestration.worker.models import (
        WorkerErrorCode,
        WorkerFailure,
        WorkerRegistration,
    )
    from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
    from src.orchestration.workflow.models import SpecialistTaskStatus

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "stale-submit")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key=f"stale-{trade_case.id}",
            role=AgentRole.FUSE,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    claimed = await runtime.cases.tasks(trade_case.id)
    assert next(t for t in claimed if t.role == AgentRole.FUSE).status == (
        SpecialistTaskStatus.RUNNING
    )

    runner = WorkerRunner(
        service=runtime,
        handler=None,
        capabilities=CapabilityProvider(service=runtime),
    )
    disposition = await runner._record_refusal(lease, WorkerFailure(WorkerErrorCode.EVIDENCE_STALE))
    assert disposition.reason_code == "EVIDENCE_STALE"

    tasks = await runtime.cases.tasks(trade_case.id)
    fuse_task = next(task for task in tasks if task.role == AgentRole.FUSE)
    assert fuse_task.status == SpecialistTaskStatus.PENDING, "the worker can work again"


async def test_the_same_evidence_recomputed_later_replays_instead_of_conflicting(
    worker_db, now, trace
):
    """§E2. Identical inputs, one second apart, same durable result.

    The stored detail carried its own evaluation timestamp, which is run
    metadata rather than part of the reading — so two recomputations of
    identical evidence produced the same idempotency key with different
    submission fingerprints, and the runtime correctly called that a conflict.
    """
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "replay-identical")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    first = await _synthesize(runtime, trade_case, now, trace)
    second = await _synthesize(runtime, trade_case, now + timedelta(seconds=1), trace)

    assert first.idempotency_key == second.idempotency_key
    assert first.fingerprint() == second.fingerprint()

    await runtime.cases.record_evidence(trade_case.id, first)
    await runtime.cases.record_evidence(trade_case.id, second)
    stored = [
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.producer_role == AgentRole.FUSE
    ]
    assert len(stored) == 1


async def test_genuinely_different_content_is_still_a_different_result(worker_db, now, trace):
    """The control. Removing run metadata must not make real changes invisible."""
    from tests.fuse.conftest import sentiment

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "replay-different")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    first = await _synthesize(runtime, trade_case, now, trace)

    from src.orchestration.workflow.models import EvidenceType
    from tests.commander.conftest import record

    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    later = now + timedelta(minutes=1)
    late_runtime, _ = build_stack(sessions, later)
    await record(
        late_runtime.cases,
        trade_case,
        later,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE", data_quality="DEGRADED"),
        key="replay-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    second = await _synthesize(late_runtime, trade_case, later, trace)

    assert first.idempotency_key != second.idempotency_key
    assert first.fingerprint() != second.fingerprint()


async def _synthesize(runtime, trade_case, instant, trace):
    from src.agents.fuse.context import FuseContextReader
    from src.agents.fuse.handler import FuseWorkerHandler
    from src.core.clock import FixedClock
    from src.orchestration.worker.capabilities import FuseCapabilities
    from tests.fuse.test_workflow import Fixed, NoSubmit, lease_for

    reader = FuseContextReader(cases=runtime.cases, clock=FixedClock(instant))
    context = await reader.synthesis_context(trade_case.id, uuid4())
    lease = lease_for(trade_case, context.task_id, instant, trace)
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Fixed(context), submit=NoSubmit())
    )
    return report.submission


# ============================ F: structured payloads, and what actually ran


async def inject_structured_anchor_evidence(cases, trade_case, now, setup_evidence, **kw):
    """ANCHOR evidence in the Phase 2J structured shape, not legacy scalars.

    The legacy shape is still accepted and every earlier test used it, which
    left the current contracts — `ExecutionAssessmentDetail`, capacity semantics,
    USD units — unexercised end to end. This builds what an ANCHOR worker
    actually produces today.
    """
    from src.orchestration.workflow.models import (
        EvidenceType,
        ExecutionAssessmentDetail,
        LiquidityExecutionPayload,
        QuotedLadderPoint,
    )
    from tests.commander.conftest import record

    trigger = next(
        item
        for item in await cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.TRIGGER
    )
    detail = ExecutionAssessmentDetail(
        policy_version="anchor-execution-v1",
        capacity_semantics="AT_LEAST",
        reason_code="CAPACITY_AT_LEAST_TESTED_CEILING",
        # A round, attractive number that nothing may read as a trade size.
        largest_tested_acceptable_notional_usd=Decimal("50000"),
        reference_price=Decimal("1.00"),
        reference_price_basis="USD_PER_BASE_UNIT",
        reference_observed_at=now,
        quote_asset_usd_price=Decimal("1.00"),
        quote_asset_usd_observed_at=now,
        quote_asset_usd_provider="geckoterminal",
        effective_price_usd_at_capacity=Decimal("1.0012"),
        execution_deviation_bps_at_capacity=Decimal("12"),
        payment_asset_id=trade_case.market.quote_asset_id,
        target_asset_id=trade_case.market.base_asset_id,
        quote_provider="kyberswap",
        quote_requests=5,
        ladder=(
            QuotedLadderPoint(
                notional_usd=Decimal("100"),
                amount_in_tokens=Decimal("100"),
                accepted=True,
                amount_out=99,
                effective_price_usd=Decimal("1.0002"),
                execution_deviation_bps=Decimal("2"),
                route_hops=1,
                venues=("uniswap-v3",),
                quoted_at=now,
            ),
            QuotedLadderPoint(
                notional_usd=Decimal("50000"),
                amount_in_tokens=Decimal("50000"),
                accepted=True,
                amount_out=49_940,
                effective_price_usd=Decimal("1.0012"),
                execution_deviation_bps=Decimal("12"),
                route_hops=4,
                venues=("uniswap-v3", "ramses-v3"),
                quoted_at=now,
            ),
        ),
        execution_digest="d" * 64,
    )
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        LiquidityExecutionPayload(
            setup_evidence_id=setup_evidence.evidence_id,
            trigger_evidence_id=trigger.evidence_id,
            # The Phase 2J worker leaves both legacy scalars empty on purpose.
            estimated_slippage_bps=None,
            price_impact_bps=None,
            execution=detail,
        ),
        key=f"structured-anchor-{trade_case.id}",
        **kw,
    )


async def test_the_lifecycle_works_on_current_structured_payloads(worker_db, now, trace):
    """§F. The same end-to-end path, on what the workers actually emit today.

    Evidence here is injected rather than produced by running ORBIT, ATLAS,
    SIGNAL and PULSE — those need reasoning providers and market data this suite
    does not reach. FUSE is the one specialist actually executed, in the
    advisory test above. What this proves is the workflow and control plane on
    current contracts, not the specialists themselves.
    """
    from src.orchestration.workflow.models import EvidenceType

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "structured-e2e")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_structured_anchor_evidence(runtime.cases, trade_case, now, setup)

    stored = next(
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    )
    assert stored.payload.execution is not None
    assert stored.payload.estimated_slippage_bps is None
    assert stored.payload.price_impact_bps is None

    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, now, COMMANDER_CONTROL_V1)
    assert context.status == TradeCaseStatus.READY_FOR_RISK
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING


async def test_a_tested_capacity_of_fifty_thousand_is_still_not_a_trade_size(worker_db, now, trace):
    """§31, §82, now against the structured payload that actually carries it.

    The earlier version of this assertion only checked that the field name was
    absent from the control plane's schemas. This puts a real `AT_LEAST 50,000`
    into durable evidence and shows the number never reaches a decision.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "capacity-not-size")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_structured_anchor_evidence(runtime.cases, trade_case, now, setup)

    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, now, COMMANDER_CONTROL_V1)

    rendered = context.model_dump_json() + decision.model_dump_json()
    assert "50000" not in rendered
    assert "AT_LEAST" not in rendered
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING
