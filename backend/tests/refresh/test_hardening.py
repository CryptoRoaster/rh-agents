"""Two contract gaps in the refresh path, reproduced and then closed.

Both are about a boundary being crossed once too often: a run that spends a step
it no longer has, and a finding that stops being a finding because it got old.
Everything here is built by the production stack on a controlled clock and read
back out of the database — task rows, attempt rows, workflow events, evidence,
requests, bindings and executions.
"""

from decimal import Decimal

from sqlalchemy import func, select

from src.core.models import AgentRole
from src.data.tables import (
    ExecutionRow,
    TradeCaseEventRow,
    TradeCaseRiskBindingRow,
    TradeCaseRiskRequestRow,
)
from src.orchestration.workflow.models import EvidenceType
from src.runner.models import ExitCode, RunStop
from src.runner.service import BoundedPaperRun, Deadline, order_key
from tests.refresh.conftest import (
    ANCHOR_SOURCE,
    all_specialists,
    at,
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
from tests.refresh.test_execution import observer_attempts
from tests.refresh.test_execution_reproduction import anchor_envelopes
from tests.refresh.test_refresh import progress_for, waited
from tests.runner.conftest import executions, run, stack_for

# What the third pass costs, step by step, when nothing stops it:
#
#   1  the intake cycle
#   2  the holder observation this run was left to make
#   3  ordering the execution re-assessment the case is blocked on
#   4  making it — after which the case is ready again
#   5  the risk request
#   6  the fill
#
# Four is therefore exactly "the refresh consumed the last step it was allowed".
STEPS_THROUGH_THE_REFRESH = 4


async def blocked_on_an_expired_assessment(sessions, now, model):
    """A real case, blocked only because a good assessment ran out of time.

    Two passes, both through the production stack. The second is interrupted by
    its own step budget after ordering a new holder observation, so that
    observation is still owed when the third pass begins — and by then the
    execution assessment written on the second pass has expired.
    """
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    second_at = await at_the_recheck(sessions, now, label="second")
    tight = type(settings).model_validate({**settings.model_dump(), "paper_runner_max_steps": 5})
    second = await run(sessions, tight, second_at, ports=ports_at(second_at, model))
    assert second.fills == 0
    owed = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (owed.status, owed.attempt) == ("PENDING", 2)
    assessed = (await anchor_envelopes(sessions))[0]
    third_at = await at_the_recheck(sessions, second_at, label="third")
    assert third_at > at(assessed.valid_until)
    return settings, third_at, assessed


async def nothing_decided(sessions):
    async with sessions() as session:
        assert (await session.scalars(select(TradeCaseRiskRequestRow))).all() == []
        assert (await session.scalars(select(TradeCaseRiskBindingRow))).all() == []
        assert (await session.scalars(select(ExecutionRow))).all() == []


# ------------------------------------------------- the budget after a refresh


async def test_a_refresh_that_spends_the_last_step_stops_before_the_risk_request(
    risk_db, now, trace
):
    """The budget is checked again after the refresh, not only before it.

    Bringing the case back to `READY_FOR_RISK` is work, and work costs steps. A
    run that spent its last one doing that has nothing left to ask SENTINEL
    with — and asking anyway would spend the case's one canonical request
    outside the budget the run was given.
    """
    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed = await blocked_on_an_expired_assessment(sessions, now, model)
    limited = type(settings).model_validate(
        {**settings.model_dump(), "paper_runner_max_steps": STEPS_THROUGH_THE_REFRESH}
    )

    third = await run(sessions, limited, third_at, ports=ports_at(third_at, model))

    case = await traded_case(sessions)
    progress = progress_for(third, case)
    # The refresh really happened and is reported as confirmed.
    assert refreshes(progress) == {ANCHOR_SOURCE: "ORDERED"}, progress
    envelopes = await anchor_envelopes(sessions)
    assert len(envelopes) == 2
    assert envelopes[1].supersedes_id == assessed.evidence_id
    reassessed = await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")
    assert reassessed.status == "SUCCEEDED"
    # And it left the case ready, which is exactly what makes the next step
    # tempting and out of budget.
    assert case.status == "READY_FOR_RISK"

    # Nothing beyond it.
    assert third.stop is RunStop.STEP_BUDGET_REACHED
    assert third.exit_code is ExitCode.COMPLETED
    assert third.steps_taken == STEPS_THROUGH_THE_REFRESH
    assert third.risk_requests == 0
    assert third.fills == 0
    assert progress.risk_outcome is None
    assert progress.risk_refusal is None
    await nothing_decided(sessions)

    # The next explicit run continues the same order, under the same key.
    fourth = await run(sessions, settings, third_at, ports=ports_at(third_at, model))

    assert fourth.fills == 1
    assert len(await executions(sessions)) == 1
    async with sessions() as session:
        request = await session.scalar(select(TradeCaseRiskRequestRow))
    assert request.request_key == order_key(case.id)
    # And the work the interrupted run had already done was not repeated.
    assert (await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")).attempt == (
        reassessed.attempt
    )
    assert len(await anchor_envelopes(sessions)) == 2


class TrippedDeadline(Deadline):
    """A monotonic budget that runs out at a named moment, not by waiting.

    Only `expired` is forced. The process can still read — which is the whole
    situation the guard exists for: out of time, but perfectly able to make one
    more call it must not make.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(seconds)
        self.tripped = False

    @property
    def expired(self) -> bool:
        return self.tripped or super().expired


async def test_a_deadline_reached_during_the_refresh_stops_before_the_risk_request(
    risk_db, now, trace
):
    """The same boundary, reached by the clock instead of by the step count."""
    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed = await blocked_on_an_expired_assessment(sessions, now, model)
    stack = stack_for(sessions, settings, third_at, ports=ports_at(third_at, model))
    deadline = TrippedDeadline(30)

    # The run's own time runs out the moment the re-assessment is in.
    anchor = next(item for item in stack.runners if item.handler.role is AgentRole.ANCHOR)
    original = anchor.run_once

    async def then_out_of_time(**keywords):
        disposition = await original(**keywords)
        if disposition is not None:
            deadline.tripped = True
        return disposition

    anchor.run_once = then_out_of_time  # type: ignore[method-assign]

    third = await BoundedPaperRun(stack, deadline=deadline).execute()

    case = await traded_case(sessions)
    progress = progress_for(third, case)
    assert refreshes(progress) == {ANCHOR_SOURCE: "ORDERED"}, progress
    assert len(await anchor_envelopes(sessions)) == 2
    assert case.status == "READY_FOR_RISK"
    assert third.stop is RunStop.TIME_BUDGET_REACHED
    assert third.risk_requests == 0
    assert third.fills == 0
    await nothing_decided(sessions)


# ------------------------------------------- a negative finding does not age out


async def a_refused_market(sessions, now, model):
    """A real ANCHOR assessment of a market that will not serve any size.

    ANCHOR is left out of the first pass, where there is no trigger for it to
    assess against and its one claim can only fail. That keeps its slot well
    inside its own claim ceiling, so what the checks below prove is that a
    negative finding is not re-armed — not that the run had simply run out of
    attempts.
    """
    from tests.anchor.conftest import source as quote_source

    settings, first = await first_pass(sessions, now, model, anchor_worker_enabled=False)
    await waited(first, sessions)
    settings = all_specialists()
    second_at = await at_the_recheck(sessions, now, label="second")
    second = await run(
        sessions, settings, second_at, ports=ports_at(second_at, model, **chain_sources(now))
    )
    assert second.fills == 0
    assessed = (await anchor_envelopes(sessions))[0]

    third_at = await at_the_recheck(sessions, second_at, label="third")
    assert third_at > at(assessed.valid_until)
    shallow = quote_source(third_at, fails_above=Decimal("1"))
    await run(sessions, settings, third_at, ports=ports_at(third_at, model, quotes=shallow))

    case = await traded_case(sessions)
    assert case.status == "BLOCKED"
    assert [item["code"] for item in case.blockers] == ["ANCHOR_BLOCKED_EXECUTION_EVIDENCE"]
    refused = (await anchor_envelopes(sessions))[-1]
    assert refused.status == "AVAILABLE", "available, and carrying a negative verdict"
    slot = await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")
    assert slot.attempt < slot.max_attempts, "attempts left, so the ceiling proves nothing here"
    return settings, third_at, refused, assessed


async def test_a_negative_assessment_is_not_refreshable_once_it_expires(risk_db, now, trace):
    """Age does not turn a finding into a gap.

    The assessment says this market would not serve a trade. That answer stops
    being *current* when its life runs out, and it never stops being an answer:
    what the case is blocked on is still what ANCHOR found, not how old the
    finding is. Re-arming on that basis would be a second opinion bought with a
    clock.
    """
    _, sessions = risk_db
    model = scripted()
    settings, third_at, refused, positive = await a_refused_market(sessions, now, model)
    case = await traded_case(sessions)

    # While it is still current, the refusal is already the right one.
    stack = stack_for(sessions, settings, third_at, ports=ports_at(third_at, model))
    assert (
        await stack.cases.refresh_source(case.id, ANCHOR_SOURCE)
    ).outcome.value == "SOURCE_NOT_STALE"

    # Now past its life, with the case and the setup still perfectly valid.
    later_at = await at_the_recheck(sessions, third_at, label="fourth")
    assert later_at > at(refused.valid_until)
    # Re-evaluated by the workflow itself, so the published blocker now names
    # age — which is precisely the disguise this test exists for.
    aged = stack_for(sessions, settings, later_at, ports=ports_at(later_at, model))
    await aged.cases.evaluate_trade_case(case.id)
    case = await traded_case(sessions)
    assert [item["code"] for item in case.blockers] == ["ANCHOR_STALE_EXECUTION_EVIDENCE"]

    # Bracketing the refusal alone: a typed no must leave nothing behind.
    before = await observer_attempts(sessions)
    events_before = await event_count(sessions)

    order = await aged.cases.refresh_source(case.id, ANCHOR_SOURCE)

    assert order.outcome.value == "SOURCE_NOT_STALE", order
    assert order.attempt is None
    assert order.task_id is None
    await unchanged_since(sessions, before, events_before, refused)


async def test_the_run_does_not_refresh_an_expired_negative_assessment(risk_db, now, trace):
    """The same through the runner, with a market that would answer differently.

    The quote source at this instant is the ordinary one, so a re-assessment
    would succeed and the case could fill. It does not happen: the run asks, the
    workflow refuses, and the case stays blocked on what was actually found.
    """
    _, sessions = risk_db
    model = scripted()
    settings, third_at, refused, _ = await a_refused_market(sessions, now, model)
    case = await traded_case(sessions)
    later_at = await at_the_recheck(sessions, third_at, label="fourth")
    assert later_at > at(refused.valid_until)
    before = await observer_attempts(sessions)
    events_before = await event_count(sessions)

    fourth = await run(sessions, settings, later_at, ports=ports_at(later_at, model))

    progress = progress_for(fourth, case)
    assert refreshes(progress) == {ANCHOR_SOURCE: "SOURCE_NOT_STALE"}, progress
    assert progress.status == "BLOCKED"
    assert fourth.risk_requests == 0
    assert fourth.fills == 0
    await unchanged_since(sessions, before, events_before, refused)


async def event_count(sessions):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TradeCaseEventRow))


async def unchanged_since(sessions, before, events_before, refused):
    """Nothing was re-armed, nothing was written, nothing was decided."""
    after = await observer_attempts(sessions)
    assert after == before, (before, after)
    anchor = await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")
    assert anchor.status == "SUCCEEDED"
    assert anchor.reason_code == "EVIDENCE_SUBMITTED"
    assert [item.attempt_number for item in await attempts(sessions, AgentRole.ANCHOR)] == list(
        range(1, before[AgentRole.ANCHOR] + 1)
    )
    envelopes = await evidence_rows(sessions, EvidenceType.LIQUIDITY_EXECUTION)
    assert envelopes[-1].evidence_id == refused.evidence_id or refused.evidence_id in {
        item.evidence_id for item in envelopes
    }
    assert sum(1 for item in envelopes if at(item.recorded_at) > at(refused.recorded_at)) == 0
    assert await event_count(sessions) == events_before
    await nothing_decided(sessions)
