"""What one bounded run may do when a decision basis has aged past its bound.

Every case here starts the same way: the production composition assembles a
TradeCase from recorded candidates, PULSE genuinely waits, the controlled clock
moves to the next due check, new observations are recorded, and a second
`--once` pass runs. What differs between them is what the world says when it is
observed again — and what the run is allowed to spend finding out.

External fixtures are the outside edge only: the recorded market rows, the
scripted model, the chain, holder, origin and social reads and the quote source.
Everything between them is the production stack. No network call is made or
simulated.
"""

from sqlalchemy import func, select

from src.core.models import AgentRole
from src.data.tables import (
    ExecutionRow,
    TradeCaseRiskRequestRow,
    TradeCaseTransitionRow,
)
from src.orchestration.workflow.models import EvidenceType
from src.runner.models import ExitCode, RunStop
from src.runner.service import order_key
from tests.refresh.conftest import (
    ATLAS_SOURCE,
    RECHECK,
    SPOT_UP,
    at_the_recheck,
    attempts,
    chain_sources,
    evidence_rows,
    first_pass,
    ports_at,
    refreshes,
    scripted,
    task_row,
    traded_case,
)
from tests.runner.conftest import executions, run, stack_for

# The step at which the second pass has a verdict and has not yet filled. Taken
# from the run that completes, not guessed: a test that picked a number would
# stop meaning anything the moment the pass changed shape.
APPROVAL_STEPS = 7


async def waited(summary, sessions):
    """The first pass really did leave a monitor waiting, not a finished case."""
    assert summary.fills == 0
    assert [item.outcome for item in await attempts(sessions, AgentRole.PULSE)] == ["WAITING"]
    monitor = await task_row(sessions, AgentRole.PULSE, "WAIT_FOR_TRIGGER")
    assert monitor.next_eligible_at is not None
    return monitor


async def stack_evidence(sessions, case):
    """The case's live evidence set, read through the workflow's own contract."""
    from src.orchestration.workflow.engine import active_evidence
    from src.orchestration.workflow.service import TradeCaseService

    service = TradeCaseService(sessions)
    return tuple(active_evidence(await service.evidence(case.id)).values())


def progress_for(summary, case):
    return next(item for item in summary.cases if item.trade_case_id == case.id)


# ------------------------------------------------------- the refresh working


async def test_new_observations_and_a_reassessment_make_exactly_one_fill(risk_db, now, trace):
    """The whole point, end to end: observed again, judged again, filled once.

    The trigger is found ninety-five seconds after the case was assembled. The
    holder reading it would be judged on is too old, so the run orders one new
    observation, the real ATLAS handler produces it, and only then is the case's
    one canonical request spent — on inputs that were all current.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)

    second = await run(sessions, settings, later, ports=ports_at(later, model))

    case = await traded_case(sessions)
    progress = progress_for(second, case)
    assert refreshes(progress) == {ATLAS_SOURCE: "ORDERED"}
    assert progress.risk_refusal is None, progress
    assert progress.risk_outcome == "APPROVE"
    assert progress.execution_id is not None
    assert second.fills == 1
    assert second.exit_code is ExitCode.COMPLETED

    # One fill, one execution row, one canonical request.
    assert len(await executions(sessions)) == 1
    async with sessions() as session:
        assert (await session.scalar(select(func.count()).select_from(ExecutionRow))) == 1
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 1

    # The reading it was judged on is the new one, produced by the real handler
    # on its second attempt and superseding the reading from before the wait.
    onchain = await evidence_rows(sessions, EvidenceType.ONCHAIN)
    assert len(onchain) == 2
    current = next(item for item in onchain if item.supersedes_id is not None)
    assert current.observed_at.replace(tzinfo=None) == later.replace(tzinfo=None)
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert observer.attempt == 2
    assert [item.outcome for item in await attempts(sessions, AgentRole.ATLAS)] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]


async def test_the_stored_basis_names_the_sources_the_decision_actually_used(risk_db, now, trace):
    """Not the reading that was there when the case was assembled — the one used.

    The basis is what makes a decision checkable a month later, so it must name
    the envelope the verdict was computed from rather than the role that has
    produced several.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    await run(sessions, settings, later, ports=ports_at(later, model))

    onchain = await evidence_rows(sessions, EvidenceType.ONCHAIN)
    current = next(item for item in onchain if item.supersedes_id is not None)
    superseded = next(item for item in onchain if item.supersedes_id is None)
    case = await traded_case(sessions)
    async with sessions() as session:
        request = await session.scalar(select(TradeCaseRiskRequestRow))

    named = request.basis["evidence"]["ONCHAIN_EVIDENCE"]
    assert named["evidence_id"] == str(current.evidence_id)
    assert named["evidence_id"] != str(superseded.evidence_id)
    # The reading it replaced is out of the live set entirely, not merely
    # outranked: anything that was derived from it describes a case that has
    # moved on.
    live = {item.evidence_id for item in await stack_evidence(sessions, case)}
    assert current.evidence_id in live
    assert superseded.evidence_id not in live
    assert named["observed_at"].startswith(later.isoformat()[:19])
    # And the safety digest the decision was taken under is not the one the case
    # carried when the trigger landed: replacing a safety source invalidates the
    # basis computed from the source it replaced.
    async with sessions() as session:
        became_ready = await session.scalar(
            select(TradeCaseTransitionRow)
            .where(
                TradeCaseTransitionRow.trade_case_id == case.id,
                TradeCaseTransitionRow.to_status == "READY_FOR_RISK",
            )
            .order_by(TradeCaseTransitionRow.revision)
        )
    assert became_ready.risk_input_digest is not None
    assert request.risk_input_digest != became_ready.risk_input_digest
    assert request.basis["safety_risk_input_digest"] == request.risk_input_digest
    assert request.basis["case_revision"] == request.case_revision


async def test_a_replay_of_the_same_request_asks_no_source_again(risk_db, now, trace):
    """History is read, not recomputed. A stored verdict needs no live source."""
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    summary = await run(sessions, settings, later, ports=ports_at(later, model))
    assert summary.fills == 1
    case = await traded_case(sessions)

    # A stack whose every outside source raises if it is touched.
    forbidden = stack_for(
        sessions,
        settings,
        later,
        ports=ports_at(later, model, onchain=Forbidden(), holders=Forbidden(), quotes=Forbidden()),
    )
    replayed = await forbidden.risk.request_risk_evaluation(case.id, request_key=order_key(case.id))

    assert replayed.kind == "risk_request_evaluated"
    assert replayed.replayed is True
    assert replayed.outcome.value == "APPROVE"
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 1


class Forbidden:
    """Any source that must not be consulted. Every method is a failure."""

    def __getattr__(self, name):
        async def refuse(*arguments, **keywords):
            raise AssertionError(f"a stored decision must not read {name} again")

        return refuse


# ------------------------------------------------------- the refresh not working


async def test_without_an_observer_the_refusal_stands_and_the_order_survives(risk_db, now, trace):
    """No ATLAS runtime in this pass: nothing observes, so nothing is filled.

    The order is still placed and still durable, which is the difference between
    refusing and giving up: the next explicit run finds a claimable task rather
    than a case that has quietly forgotten what it needed.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    without_atlas = type(settings).model_validate(
        {**settings.model_dump(), "atlas_worker_enabled": False}
    )

    second = await run(sessions, without_atlas, later, ports=ports_at(later, model))

    case = await traded_case(sessions)
    progress = progress_for(second, case)
    assert refreshes(progress) == {ATLAS_SOURCE: "ORDERED"}
    assert progress.risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT"
    assert second.fills == 0
    assert await executions(sessions) == []
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert observer.status == "PENDING"
    assert observer.attempt == 2
    assert observer.lease_id is None


async def test_a_reassessment_that_refuses_does_not_fill(risk_db, now, trace):
    """The chain was read again and the holder source had nothing to say.

    An unusable answer is an answer: the case is judged on what is actually
    known, which is not enough, and no order is placed.
    """
    from src.markets.models import Availability
    from tests.atlas.conftest import holder_source_result

    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    blind = holder_source_result(later, status=Availability.UNAVAILABLE)

    second = await run(
        sessions,
        settings,
        later,
        ports=ports_at(later, model, **chain_sources(later, holders=blind)),
    )

    case = await traded_case(sessions)
    progress = progress_for(second, case)
    assert progress.execution_id is None
    assert second.fills == 0
    assert await executions(sessions) == []
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 0


async def test_a_run_refreshes_one_case_at_most_once(risk_db, now, trace):
    """One order per case per pass, whatever the second answer turns out to be.

    Two orders would be this process retrying until something passed, which is
    the one shape a bounded run must never take.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    blind_to_the_chain = chain_sources(now)

    second = await run(
        sessions, settings, later, ports=ports_at(later, model, **blind_to_the_chain)
    )

    case = await traded_case(sessions)
    assert progress_for(second, case).risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT"
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    # Re-armed exactly once. Attempt three would mean the run went round again.
    assert observer.attempt == 2
    assert second.fills == 0


# ------------------------------------------------------- bounds


async def test_a_budget_that_runs_out_mid_refresh_stops_and_books_nothing(risk_db, now, trace):
    """Out of steps between ordering the new observation and getting one.

    The run stops where it stands. What it ordered is durable and claimable, so
    the next explicit pass continues from there; what it had not done is simply
    not done. Nothing partial is written, because every step it took was its own
    committed transaction.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    tight = type(settings).model_validate({**settings.model_dump(), "paper_runner_max_steps": 5})

    second = await run(sessions, tight, later, ports=ports_at(later, model))

    case = await traded_case(sessions)
    progress = progress_for(second, case)
    assert refreshes(progress) == {ATLAS_SOURCE: "ORDERED"}, (
        "the budget ended after the order, not before it"
    )
    assert progress.risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT"
    assert second.stop is RunStop.STEP_BUDGET_REACHED
    assert second.steps_taken == 5
    assert second.fills == 0
    assert await executions(sessions) == []
    assert second.exit_code is ExitCode.COMPLETED
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 0

    # Left claimable, owned by nobody, at the attempt the order moved it to.
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (observer.status, observer.attempt, observer.lease_id) == ("PENDING", 2, None)

    # And the next explicit pass finishes exactly that work, once.
    third = await run(sessions, settings, later, ports=ports_at(later, model))
    assert third.fills == 1
    assert len(await executions(sessions)) == 1
    assert (await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")).attempt == 2


async def test_an_approval_whose_window_closed_is_refused_at_the_fill(risk_db, now, trace):
    """Out of steps after the verdict, and out of time before the next pass.

    The approval was real and stays recorded. What it is not is an authorization
    for later: the fill refuses in the typed vocabulary and books nothing, and
    the case is left exactly as the interrupted run left it.
    """
    from datetime import timedelta

    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    later = await at_the_recheck(sessions, now)
    approved_only = type(settings).model_validate(
        {**settings.model_dump(), "paper_runner_max_steps": APPROVAL_STEPS}
    )

    second = await run(sessions, approved_only, later, ports=ports_at(later, model))

    case = await traded_case(sessions)
    progress = progress_for(second, case)
    assert progress.risk_outcome == "APPROVE"
    assert progress.execution_id is None
    assert second.fills == 0
    assert await executions(sessions) == []

    # Long past the decision's own short life, and with the market still moving.
    afterwards = later + timedelta(hours=2)
    await at_the_recheck(sessions, afterwards - RECHECK, price=SPOT_UP, label="two-hours-on")

    third = await run(sessions, settings, afterwards, ports=ports_at(afterwards, model))

    refused = progress_for(third, case)
    assert refused.fill_refusal == "AUTHORIZATION_EXPIRED", refused
    assert third.fills == 0
    assert await executions(sessions) == []
