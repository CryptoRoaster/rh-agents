"""The refresh order itself: what it is, and every case where it refuses.

These go through `TradeCaseService` directly, because what is being checked is
the contract a runner calls rather than the run that calls it. The cases they
check are still built by the production stack — a refusal is only interesting
against a case that really reached the state it refuses in.
"""

from src.core.models import AgentRole
from src.orchestration.workflow.models import SourceRefreshOutcome
from tests.refresh.conftest import (
    ATLAS_SOURCE,
    at_the_recheck,
    first_pass,
    ports_at,
    scripted,
    task_row,
    traded_case,
)
from tests.runner.conftest import run, stack_for


async def ready_with_a_stale_reading(sessions, now, model):
    """A case that reached `READY_FOR_RISK` on a reading from before the wait.

    The second pass runs without an ATLAS runtime, so the run orders the new
    observation it needs and nothing is there to produce it. That is exactly the
    state these refusals are about.
    """
    settings, _ = await first_pass(sessions, now, model)
    later = await at_the_recheck(sessions, now)
    without_atlas = type(settings).model_validate(
        {**settings.model_dump(), "atlas_worker_enabled": False}
    )
    await run(sessions, without_atlas, later, ports=ports_at(later, model))
    return settings, later


async def test_a_source_no_task_observes_is_refused(risk_db, now, trace):
    """The price, the token and the liquidity come from the recorded market.

    Nothing in this workflow can make the market be recorded again, so asking is
    answered rather than approximated with whatever task is nearest.
    """
    _, sessions = risk_db
    model = scripted()
    settings, later = await ready_with_a_stale_reading(sessions, now, model)
    stack = stack_for(sessions, settings, later, ports=ports_at(later, model))
    case = await traded_case(sessions)

    for source in (
        "RECORDED_MARKET_OBSERVATION",
        "OPERATOR_CONFIGURED_ASSUMPTION",
        "SOMETHING_ELSE",
    ):
        order = await stack.cases.refresh_source(case.id, source)
        assert order.outcome is SourceRefreshOutcome.SOURCE_NOT_REFRESHABLE, source
        assert order.task_id is None
        assert order.attempt is None


async def test_a_case_still_being_assembled_is_refused(risk_db, now, trace):
    """Before the trigger there is nothing waiting on a fresher precondition.

    The ordinary tasks are still running; arming one of them again would be a
    second copy of work already outstanding.
    """
    _, sessions = risk_db
    model = scripted()
    settings, _ = await first_pass(sessions, now, model)
    stack = stack_for(sessions, settings, now, ports=ports_at(now, model))
    case = await traded_case(sessions)
    assert case.status != "READY_FOR_RISK"

    order = await stack.cases.refresh_source(case.id, ATLAS_SOURCE)

    assert order.outcome is SourceRefreshOutcome.CASE_NOT_READY
    assert order.attempt is None
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert observer.attempt == 1, "nothing was armed"


async def test_a_second_order_finds_the_first(risk_db, now, trace):
    """Outstanding work is not ordered twice, whoever asks."""
    _, sessions = risk_db
    model = scripted()
    settings, later = await ready_with_a_stale_reading(sessions, now, model)
    stack = stack_for(sessions, settings, later, ports=ports_at(later, model))
    case = await traded_case(sessions)
    armed = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (armed.status, armed.attempt) == ("PENDING", 2)

    order = await stack.cases.refresh_source(case.id, ATLAS_SOURCE)

    assert order.outcome is SourceRefreshOutcome.ALREADY_ORDERED
    assert order.role is AgentRole.ATLAS
    assert order.attempt is None
    unchanged = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert (unchanged.status, unchanged.attempt) == ("PENDING", 2)


async def test_a_decided_case_is_not_reopened(risk_db, now, trace):
    """Once the request has been spent, its inputs are history.

    Remaking them would be reopening a settled decision, which is a different
    thing from establishing a precondition before one is taken.
    """
    _, sessions = risk_db
    model = scripted()
    settings, _ = await first_pass(sessions, now, model)
    later = await at_the_recheck(sessions, now)
    summary = await run(sessions, settings, later, ports=ports_at(later, model))
    assert summary.fills == 1
    stack = stack_for(sessions, settings, later, ports=ports_at(later, model))
    case = await traded_case(sessions)

    order = await stack.cases.refresh_source(case.id, ATLAS_SOURCE)

    assert order.outcome is SourceRefreshOutcome.CASE_NOT_READY
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert observer.status == "SUCCEEDED"
    assert observer.attempt == 2, "the fill's own refresh, and no other"


async def test_the_order_is_recorded_against_the_case_and_the_source(risk_db, now, trace):
    """An order nobody can trace is not an audit trail.

    The event names the task, the role, the attempt it moved to and the source
    that was too old, and it sits in the case's own timeline at the revision it
    was ordered against.
    """
    _, sessions = risk_db
    model = scripted()
    settings, later = await ready_with_a_stale_reading(sessions, now, model)
    stack = stack_for(sessions, settings, later, ports=ports_at(later, model))
    case = await traded_case(sessions)

    timeline = await stack.cases.timeline(case.id)

    ordered = [item for item in timeline if item.reason_code == "RISK_SOURCE_TOO_OLD"]
    assert len(ordered) == 1
    event = ordered[0]
    assert event.event_type == "TASK_STATUS_CHANGED"
    assert event.payload["role"] == AgentRole.ATLAS.value
    assert event.payload["source"] == ATLAS_SOURCE
    assert event.payload["to"] == "PENDING"
    assert event.payload["attempt"] == 2
    observer = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert event.payload["task_id"] == str(observer.task_id)


async def test_an_order_against_a_stale_revision_is_refused(risk_db, now, trace):
    """A caller that names a revision must name the one it actually read."""
    from src.orchestration.workflow.models import WorkflowErrorCode, WorkflowFailure

    _, sessions = risk_db
    model = scripted()
    settings, later = await ready_with_a_stale_reading(sessions, now, model)
    stack = stack_for(sessions, settings, later, ports=ports_at(later, model))
    case = await traded_case(sessions)

    try:
        await stack.cases.refresh_source(case.id, ATLAS_SOURCE, expected_revision=case.revision + 1)
    except WorkflowFailure as error:
        assert error.code is WorkflowErrorCode.CONCURRENCY_CONFLICT
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("a stale revision must not be accepted")


def test_the_declared_origins_are_the_ones_the_risk_data_contract_publishes():
    """The one table, held against the vocabulary it is written in.

    `policy.py` cannot import the risk-data package — the risk-data package
    reads the policy — so the origins are held there as the strings that
    vocabulary publishes. This is what stops the two drifting apart: a renamed
    origin, or one silently invented here, fails.

    It also states, executably, which origins are deliberately *not* refreshable.
    Nothing in this workflow records a market, and a configured assumption was
    never observed at all, so no task could ever be armed for either.
    """
    from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactOrigin
    from src.orchestration.workflow.policy import STALE_GAP, TRADE_CASE_V1

    declared = {item.origin for item in TRADE_CASE_V1.refreshable_sources}
    assert declared <= {item.value for item in RiskFactOrigin}
    assert declared == {"ATLAS_ONCHAIN_EVIDENCE", "ANCHOR_EXECUTION_EVIDENCE"}
    assert {item.value for item in RiskFactOrigin} - declared == {
        "RECORDED_MARKET_OBSERVATION",
        "OPERATOR_CONFIGURED_ASSUMPTION",
    }
    assert STALE_GAP == RiskDataGapCode.STALE.value
    # One declaration per evidence type, so a lookup can never be ambiguous.
    assert len({item.evidence_type for item in TRADE_CASE_V1.refreshable_sources}) == len(declared)


def test_only_an_age_gap_maps_to_an_observer():
    """Every other cause a readiness gap can name keeps its own refusal."""
    from src.orchestration.riskdata.models import (
        RiskDataGap,
        RiskDataGapCode,
        RiskFactKind,
        RiskFactOrigin,
    )
    from src.orchestration.workflow.policy import TRADE_CASE_V1

    def gap(code: RiskDataGapCode) -> RiskDataGap:
        return RiskDataGap(
            kind=RiskFactKind.ROUTING_AVAILABILITY,
            code=code,
            expected_origin=RiskFactOrigin.ANCHOR_EXECUTION_EVIDENCE,
        )

    assert TRADE_CASE_V1.refreshable_for_gap(gap(RiskDataGapCode.STALE)) is not None
    for code in RiskDataGapCode:
        if code is RiskDataGapCode.STALE:
            continue
        assert TRADE_CASE_V1.refreshable_for_gap(gap(code)) is None, code
    # And an origin nothing observes stays unobservable whatever the cause.
    assert (
        TRADE_CASE_V1.refreshable_for_gap(
            RiskDataGap(
                kind=RiskFactKind.REFERENCE_PRICE,
                code=RiskDataGapCode.STALE,
                expected_origin=RiskFactOrigin.RECORDED_MARKET_OBSERVATION,
            )
        )
        is None
    )
