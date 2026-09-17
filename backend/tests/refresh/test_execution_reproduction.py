"""The second stale source: ANCHOR's execution assessment.

The holder reading ages out of SENTINEL's own pre-request bound. An execution
assessment ages differently — its envelope carries a ninety-second life measured
from the reference observation it was built on — so it expires out of the
*readiness* contract instead: a different check, a different refusal, and for a
while nothing re-armed ANCHOR either.

Both documented entry points are reproduced here through the production
composition on a controlled clock: a case that has sat through a wait, and a
case whose refresh run was interrupted by its own budget. Nothing is prepared,
nothing is submitted directly, and no network or model call is made.
"""

from datetime import timedelta

from sqlalchemy import select

from src.core.models import AgentRole
from src.data.tables import (
    ExecutionRow,
    TradeCaseRiskBindingRow,
    TradeCaseRiskRequestRow,
)
from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactKind, RiskFactOrigin
from src.orchestration.workflow.models import EvidenceType
from src.runner.service import order_key
from tests.refresh.conftest import (
    ANCHOR_SOURCE,
    ATLAS_SOURCE,
    FRESH,
    at,
    at_the_recheck,
    chain_sources,
    evidence_rows,
    first_pass,
    ports_at,
    refreshes,
    scripted,
    task_row,
    traded_case,
)
from tests.refresh.test_refresh import progress_for, waited
from tests.runner.conftest import executions, run, stack_for

# `ANCHOR_EXECUTION_V1.max_reference_age`, restated so the arithmetic below is
# readable. Nothing in this phase changes it.
EXECUTION_LIFE = timedelta(seconds=90)


async def anchor_envelopes(sessions):
    return sorted(
        await evidence_rows(sessions, EvidenceType.LIQUIDITY_EXECUTION),
        key=lambda item: item.recorded_at,
    )


async def nothing_final(sessions):
    """No canonical request, no binding, no execution. Read from the rows."""
    async with sessions() as session:
        assert (await session.scalars(select(TradeCaseRiskRequestRow))).all() == []
        assert (await session.scalars(select(TradeCaseRiskBindingRow))).all() == []
        assert (await session.scalars(select(ExecutionRow))).all() == []


async def test_after_a_wait_the_execution_assessment_ages_out_of_readiness(risk_db, now, trace):
    """Three explicit passes, and the exact instants that decide the outcome.

    Pass one assembles the case and PULSE genuinely waits. Pass two finds the
    trigger, and ANCHOR assesses execution against the market observation
    recorded for that pass. Pass three is ninety-five seconds later again — and
    that assessment's ninety-second life has run out, while the case, the setup
    and the trigger are all still valid and no risk decision has been spent.

    The chain is never observed again here, so the holder reading stays as old as
    it was throughout. That is deliberate: it keeps a fill out of the way, and it
    is what makes the third pass show both gaps being handled at once.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)

    second_at = await at_the_recheck(sessions, now, label="second")
    second = await run(
        sessions, settings, second_at, ports=ports_at(second_at, model, **chain_sources(now))
    )
    case = await traded_case(sessions)
    assert case.status == "READY_FOR_RISK"
    assert progress_for(second, case).risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT"
    assert second.fills == 0

    # What ANCHOR wrote, when it observed, and how long that is good for.
    assessed = (await anchor_envelopes(sessions))[0]
    assert at(assessed.observed_at) == second_at - FRESH, "built on the observation, not on the run"
    assert at(assessed.valid_until) == at(assessed.observed_at) + EXECUTION_LIFE
    assert assessed.supersedes_id is None
    anchor_task = await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")
    assert anchor_task.status == "SUCCEEDED"
    assessed_at_attempt = anchor_task.attempt

    # --- pass three, past that life ---
    third_at = await at_the_recheck(sessions, second_at, label="third")
    assert third_at > at(assessed.valid_until), (third_at, at(assessed.valid_until))

    # Asked before this run touches anything, the real service refuses on the
    # readiness contract and names the origin. A refusal writes nothing, so this
    # observes the state rather than changing it.
    stack = stack_for(sessions, settings, third_at, ports=ports_at(third_at, model))
    refused = await stack.risk.request_risk_evaluation(case.id, request_key=order_key(case.id))
    assert refused.kind == "risk_request_refused"
    assert refused.reason.value == "RISK_DATA_INCOMPLETE"
    assert [(item.kind, item.code, item.expected_origin) for item in refused.data_gaps] == [
        (
            RiskFactKind.ROUTING_AVAILABILITY,
            RiskDataGapCode.STALE,
            RiskFactOrigin.ANCHOR_EXECUTION_EVIDENCE,
        )
    ]
    assert refused.blockers == ()
    await nothing_final(sessions)

    third = await run(
        sessions, settings, third_at, ports=ports_at(third_at, model, **chain_sources(now))
    )

    # Both gaps were asked about, in one pass, each exactly once.
    progress = progress_for(third, case)
    assert refreshes(progress) == {ANCHOR_SOURCE: "ORDERED", ATLAS_SOURCE: "ORDERED"}, progress

    # ANCHOR really did assess again: a second envelope, superseding the first,
    # observed against the market recorded for this pass.
    envelopes = await anchor_envelopes(sessions)
    assert len(envelopes) == 2
    current = envelopes[1]
    assert current.supersedes_id == assessed.evidence_id
    assert at(current.observed_at) == third_at - FRESH
    assert at(current.valid_until) == at(current.observed_at) + EXECUTION_LIFE
    reassessed = await task_row(sessions, AgentRole.ANCHOR, "ASSESS_EXECUTION")
    assert reassessed.attempt == assessed_at_attempt + 1
    assert reassessed.status == "SUCCEEDED"

    # And no fill, because the holder reading never became current: one gap
    # closing does not make the other one go away.
    assert progress.risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT", progress
    assert third.fills == 0
    await nothing_final(sessions)


async def test_an_interrupted_refresh_run_is_continued_to_exactly_one_fill(risk_db, now, trace):
    """The other entry point, carried through to the end.

    Pass two orders a new holder observation and runs out of budget before it can
    be made. Pass three makes it — and recording it re-evaluates the case onto
    what is now wrong with it, which is an execution assessment that expired
    while the run was interrupted. That is a blocker a new observation answers,
    and the same pass answers it and fills once.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    second_at = await at_the_recheck(sessions, now, label="second")
    tight = type(settings).model_validate({**settings.model_dump(), "paper_runner_max_steps": 5})

    second = await run(sessions, tight, second_at, ports=ports_at(second_at, model))

    case = await traded_case(sessions)
    assert refreshes(progress_for(second, case)) == {ATLAS_SOURCE: "ORDERED"}
    assert second.fills == 0
    ordered = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (ordered.status, ordered.attempt, ordered.lease_id) == ("PENDING", 2, None)
    assessed = (await anchor_envelopes(sessions))[0]
    await nothing_final(sessions)

    third_at = await at_the_recheck(sessions, second_at, label="third")
    assert third_at > at(assessed.valid_until)

    third = await run(sessions, settings, third_at, ports=ports_at(third_at, model))

    # The ordered observation was made on this pass, by the real handler.
    onchain = await evidence_rows(sessions, EvidenceType.ONCHAIN)
    assert len(onchain) == 2
    assert max(at(item.observed_at) for item in onchain) == third_at

    # And the expired assessment was replaced rather than worked around.
    progress = progress_for(third, case)
    assert refreshes(progress) == {ANCHOR_SOURCE: "ORDERED"}, progress
    envelopes = await anchor_envelopes(sessions)
    assert len(envelopes) == 2
    assert envelopes[1].supersedes_id == assessed.evidence_id
    assert at(envelopes[1].observed_at) == third_at - FRESH

    assert progress.risk_outcome == "APPROVE"
    assert progress.execution_id is not None
    assert third.fills == 1
    assert len(await executions(sessions)) == 1
