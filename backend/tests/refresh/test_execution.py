"""Refreshing an execution assessment: what it proves and what it refuses.

Every case here is carried by the production stack through a real wait and a
real trigger before anything below is asked. The external fixtures are the
outside edge only — recorded market rows, the scripted model, and the chain,
holder, origin, social, history and quote sources. No network call is made or
simulated, and every instant is a test input on a controlled clock.
"""

from decimal import Decimal

from sqlalchemy import func, select

from src.core.models import AgentRole, RiskContext
from src.data.tables import TradeCaseRiskRequestRow
from src.ledger.portfolio import replay_portfolio_basis
from src.markets.fake_quotes import QuoteFailure
from src.orchestration.workflow.models import EvidenceType
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
from tests.refresh.test_execution_reproduction import anchor_envelopes, nothing_final
from tests.refresh.test_refresh import progress_for, waited
from tests.runner.conftest import executions, run


async def carried_to_an_expired_assessment(sessions, now, model):
    """Two passes that leave a valid case whose execution assessment has expired.

    The chain is not observed again on the second pass, so nothing fills there
    and the case is still standing — with its holder reading old, and with the
    execution assessment ANCHOR wrote on that pass about to run out.
    """
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    second_at = await at_the_recheck(sessions, now, label="second")
    second = await run(
        sessions, settings, second_at, ports=ports_at(second_at, model, **chain_sources(now))
    )
    assert second.fills == 0
    assessed = (await anchor_envelopes(sessions))[0]
    third_at = await at_the_recheck(sessions, second_at, label="third")
    assert third_at > at(assessed.valid_until)
    return settings, third_at, assessed, await observer_attempts(sessions)


async def observer_attempts(sessions):
    """Where each observing slot stands, so a later check can read the change.

    The absolute numbers carry the case's whole history — a first ANCHOR attempt
    before the trigger existed, a holder observation ordered on an earlier pass.
    What a proof about one pass needs is what that pass added.
    """
    return {
        role: (await task_row(sessions, role, task_type)).attempt
        for role, task_type in (
            (AgentRole.ANCHOR, "ASSESS_EXECUTION"),
            (AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY"),
        )
    }


async def test_both_stale_sources_are_refreshed_once_each_and_the_case_fills(risk_db, now, trace):
    """The control case: two gaps, two observations, one PAPER fill.

    Each observer is asked exactly once, both new readings are recorded as
    evidence that supersedes what it replaces, and the decision that follows is
    taken on those readings and no others.
    """
    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed, before = await carried_to_an_expired_assessment(
        sessions, now, model
    )

    third = await run(sessions, settings, third_at, ports=ports_at(third_at, model))

    case = await traded_case(sessions)
    progress = progress_for(third, case)
    assert refreshes(progress) == {ANCHOR_SOURCE: "ORDERED", ATLAS_SOURCE: "ORDERED"}
    after = await observer_attempts(sessions)
    assert [after[role] - before[role] for role in before] == [1, 1], (before, after)
    assert sorted(item.attempt for item in progress.refreshes) == sorted(after.values())
    assert progress.risk_outcome == "APPROVE"
    assert third.fills == 1
    assert len(await executions(sessions)) == 1

    # Each observer ran again, once, and superseded its own previous reading.
    for role, task_type, kind in (
        (AgentRole.ANCHOR, "ASSESS_EXECUTION", EvidenceType.LIQUIDITY_EXECUTION),
        (AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY", EvidenceType.ONCHAIN),
    ):
        observer = await task_row(sessions, role, task_type)
        assert observer.status == "SUCCEEDED", role
        envelopes = sorted(await evidence_rows(sessions, kind), key=lambda item: item.recorded_at)
        current, replaced = envelopes[-1], envelopes[-2]
        assert current.supersedes_id == replaced.evidence_id, kind
        assert at(current.observed_at) > at(replaced.observed_at), kind

    # And the decision names exactly those readings.
    async with sessions() as session:
        request = await session.scalar(select(TradeCaseRiskRequestRow))
    named = request.basis["evidence"]
    for kind in (EvidenceType.LIQUIDITY_EXECUTION, EvidenceType.ONCHAIN):
        envelopes = sorted(await evidence_rows(sessions, kind), key=lambda item: item.recorded_at)
        current = envelopes[-1]
        assert named[kind.value]["evidence_id"] == str(current.evidence_id), kind
        assert named[kind.value]["observed_at"] == at(current.observed_at).isoformat(), kind
        assert named[kind.value]["evidence_id"] != str(envelopes[-2].evidence_id), kind
    assert (
        request.basis["evidence"][EvidenceType.LIQUIDITY_EXECUTION.value]["observed_at"]
        == (third_at - FRESH).isoformat()
    )

    # The portfolio SENTINEL judged still recomputes from what was stored.
    recomputed = replay_portfolio_basis(request.basis["portfolio"]).context
    assert recomputed == RiskContext.model_validate(request.basis["risk_context"])


async def test_an_unchanged_quote_source_leaves_the_assessment_stale(risk_db, now, trace):
    """Nothing newer to assess, so nothing is made newer.

    The market is not observed again on the third pass, so the reference ANCHOR
    would have to judge against is older than its own tolerance. The order is
    placed, the real handler runs, and it declines to write an assessment rather
    than re-dating the one it already had.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await first_pass(sessions, now, model)
    await waited(first, sessions)
    second_at = await at_the_recheck(sessions, now, label="second")
    await run(sessions, settings, second_at, ports=ports_at(second_at, model, **chain_sources(now)))
    assessed = (await anchor_envelopes(sessions))[0]

    # No new observation of anything: the clock moves, the world does not.
    third_at = second_at + (second_at - now)
    assert third_at > at(assessed.valid_until)

    third = await run(
        sessions, settings, third_at, ports=ports_at(third_at, model, **chain_sources(now))
    )

    case = await traded_case(sessions)
    assert refreshes(progress_for(third, case)).get(ANCHOR_SOURCE) == "ORDERED"
    still = await anchor_envelopes(sessions)
    assert len(still) == 1, "no second assessment was written"
    assert still[0].evidence_id == assessed.evidence_id
    assert at(still[0].observed_at) == at(assessed.observed_at)
    assert at(still[0].valid_until) == at(assessed.valid_until)
    assert third.fills == 0
    await nothing_final(sessions)


async def test_a_quote_source_with_no_route_does_not_fill(risk_db, now, trace):
    """The source is there and has nothing to offer. That is an answer, not a fill."""
    from tests.anchor.conftest import source as quote_source

    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed, before = await carried_to_an_expired_assessment(
        sessions, now, model
    )
    routeless = quote_source(third_at, always_fails=QuoteFailure.NO_ROUTE)

    third = await run(
        sessions, settings, third_at, ports=ports_at(third_at, model, quotes=routeless)
    )

    case = await traded_case(sessions)
    assert refreshes(progress_for(third, case)).get(ANCHOR_SOURCE) == "ORDERED"
    # The assessment it came back with is a finding about the market and is
    # recorded as one. What it is not is a route, so nothing follows from it.
    written = await anchor_envelopes(sessions)
    assert len(written) == 2
    assert written[1].supersedes_id == assessed.evidence_id
    assert written[1].payload["payload"]["maximum_safe_size_usd"] is None, written[1].payload
    assert third.fills == 0
    assert await executions(sessions) == []
    await nothing_final(sessions)


async def test_a_negative_reassessment_stays_negative(risk_db, now, trace):
    """A market that will not serve any size is re-assessed and still will not.

    The new assessment is recorded — it is a real finding about the market — and
    it establishes no capacity, so no risk request follows. Asking for the same
    source again is then refused as a matter of contract: what is wrong with this
    case is not that anything is old.
    """
    from tests.anchor.conftest import source as quote_source

    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed, before = await carried_to_an_expired_assessment(
        sessions, now, model
    )
    shallow = quote_source(third_at, fails_above=Decimal("1"))

    third = await run(sessions, settings, third_at, ports=ports_at(third_at, model, quotes=shallow))

    case = await traded_case(sessions)
    assert refreshes(progress_for(third, case)).get(ANCHOR_SOURCE) == "ORDERED"
    assert third.fills == 0
    assert await executions(sessions) == []
    await nothing_final(sessions)

    # What it wrote establishes no route, and the case is blocked on that finding
    # rather than on anything being old.
    assert case.status == "BLOCKED"
    assert [item["code"] for item in case.blockers] == ["ANCHOR_BLOCKED_EXECUTION_EVIDENCE"]

    # So a further order is refused, by contract, rather than turned into a
    # second opinion on a market that answered the question.
    from tests.runner.conftest import stack_for

    stack = stack_for(sessions, settings, third_at, ports=ports_at(third_at, model))
    order = await stack.cases.refresh_source(case.id, ANCHOR_SOURCE)
    assert order.outcome.value == "SOURCE_NOT_STALE", order
    assert order.attempt is None


async def test_a_budget_that_ends_between_refreshes_is_continued_by_the_next_run(
    risk_db, now, trace
):
    """Out of steps after one observation and before the next.

    What was ordered is durable, what was observed is recorded, and the next
    explicit pass carries on from there to exactly one fill — without ordering
    anything a second time.
    """
    _, sessions = risk_db
    model = scripted()
    settings, third_at, assessed, before = await carried_to_an_expired_assessment(
        sessions, now, model
    )
    tight = type(settings).model_validate({**settings.model_dump(), "paper_runner_max_steps": 4})

    interrupted = await run(sessions, tight, third_at, ports=ports_at(third_at, model))

    case = await traded_case(sessions)
    assert interrupted.fills == 0
    assert await executions(sessions) == []
    await nothing_final(sessions)
    placed = refreshes(progress_for(interrupted, case))
    assert placed.get(ANCHOR_SOURCE) == "ORDERED", placed

    continued = await run(sessions, settings, third_at, ports=ports_at(third_at, model))

    assert continued.fills == 1
    assert len(await executions(sessions)) == 1
    after = await observer_attempts(sessions)
    assert [after[role] - before[role] for role in before] == [1, 1], (before, after)
    for role, task_type in (
        (AgentRole.ANCHOR, "ASSESS_EXECUTION"),
        (AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY"),
    ):
        assert (await task_row(sessions, role, task_type)).status == "SUCCEEDED", role
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        ) == 1
