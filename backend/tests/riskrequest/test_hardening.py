"""Two contract defects an independent review reproduced against 5736391.

Both let a verdict rest on something it should not: a case that had stopped
being eligible while nothing touched its row, and a market snapshot nobody kept.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import AgentRole, MarketSnapshot, RiskDecision, RiskOutcome
from src.data.tables import TradeCaseRiskBindingRow, TradeCaseRiskRequestRow
from src.orchestration.riskrequest.models import RiskRequestRefusal, risk_request_digest
from src.orchestration.workflow.engine import TradeCaseEvaluator
from src.orchestration.workflow.models import (
    EvidenceType,
    SentimentPayload,
    TradeCaseStatus,
)
from src.orchestration.workflow.service import TradeCaseService
from tests.riskdata.conftest import anchor_payload, record, record_onchain
from tests.riskrequest.conftest import (
    RecordedMarkets,
    build_service,
    fresh_onchain,
    fresh_snapshot,
    open_case,
    ready_case,
)
from tests.worker.conftest import setup_payload, trigger_payload


class SteppingClock:
    """A trusted clock that advances on every read, without any real sleeping.

    Time passing *between* the input reads and the decision instant is the
    situation being reproduced. A real sleep would prove it slowly and flakily
    instead of exactly.
    """

    def __init__(self, instant, step=timedelta(seconds=3)) -> None:
        self.instant = instant
        self.step = step
        self.reads = 0

    def now(self):
        self.reads += 1
        current = self.instant
        self.instant = self.instant + self.step
        return current


class MovingClock:
    """A trusted clock a test can advance, without any real sleeping."""

    def __init__(self, instant) -> None:
        self.instant = instant

    def now(self):
        return self.instant


class DelayedCases(TradeCaseService):
    """A workflow service whose input load takes time.

    The point of the split between loading and judging is that the clock is read
    *after* the last input read. Making that read cost time is the only way a
    test can tell the two orderings apart: with the instant taken first, the
    verdict describes the moment the loading began.
    """

    def __init__(self, sessions, *, clock, delay) -> None:
        super().__init__(sessions, clock=clock)
        self._delay = delay

    async def workflow_inputs_in_session(self, session, row):
        inputs = await super().workflow_inputs_in_session(session, row)
        self.clock.instant = self.clock.instant + self._delay
        return inputs


class RefusingMarkets:
    """A feed that proves no market read happened."""

    async def latest(self, identity, *, include_fixtures=False):
        raise AssertionError("the stored basis must not need a market read")


async def counts(sessions):
    async with sessions() as session:
        requests = await session.scalar(select(func.count()).select_from(TradeCaseRiskRequestRow))
        bindings = await session.scalar(select(func.count()).select_from(TradeCaseRiskBindingRow))
    return requests, bindings


async def aging_case(cases, now, *, lifetime=timedelta(hours=1), trigger_valid=None, key="aging"):
    """A READY_FOR_RISK case whose validity can be made to lapse.

    Built envelope by envelope rather than through the shared helper so the
    trigger's own horizon can be shortened — the case for safety evidence that
    ages out while the case itself is still alive.
    """
    case = await open_case(cases, now, uuid4(), key, lifetime=lifetime)
    await record_onchain(cases, case, now, fresh_onchain(now))
    await record(
        cases,
        case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        SentimentPayload(assessment="NEUTRAL"),
        key=f"hard-signal-{case.id}",
    )
    setup = await record(
        cases,
        case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(),
        key=f"hard-setup-{case.id}",
    )
    trigger = await record(
        cases,
        case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"hard-trigger-{case.id}",
        **({"valid_until": trigger_valid} if trigger_valid is not None else {}),
    )
    await record(
        cases,
        case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        anchor_payload(setup.evidence_id, trigger.evidence_id),
        key=f"hard-anchor-{case.id}",
    )
    return await cases.get_trade_case(case.id)


async def effective_status(cases, case_id, at):
    case = await cases.get_trade_case(case_id)
    evidence = await cases.evidence(case_id)
    return TradeCaseEvaluator().evaluate(case, evidence, None, at).status


# ================================================ 1. eligibility at decision time


async def test_an_expired_case_is_not_asked_about_however_the_row_reads(risk_db, now, trace):
    """The reproduction: the stored status is a snapshot, and time moved.

    The case's own lifetime lapsed and nothing touched the row, so it still says
    READY_FOR_RISK. `_stabilize` would have corrected it — after the binding was
    written, which is far too late to be a precondition.
    """
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(builder.cases, now, lifetime=timedelta(seconds=10), key="hard-expired")
    later = now + timedelta(seconds=15)
    service = build_service(sessions, later, feed=RecordedMarkets(fresh_snapshot(later)))

    stored = await service.cases.get_trade_case(case.id)
    assert stored.status is TradeCaseStatus.READY_FOR_RISK
    assert await effective_status(service.cases, case.id, later) is TradeCaseStatus.EXPIRED

    result = await service.request_risk_evaluation(case.id, request_key="hard-expired-key")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE
    assert result.detail == "EXPIRED"
    assert (await counts(sessions)) == (0, 0)


async def test_aged_out_safety_evidence_is_not_asked_about(risk_db, now, trace):
    """The same hole through the other door: the case lives, its trigger does not.

    The completeness check never looks at the trigger — it is not a SENTINEL
    input — so only the workflow evaluator can see this, and only if it is asked.
    """
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(
        builder.cases, now, trigger_valid=now + timedelta(seconds=10), key="hard-stale"
    )
    later = now + timedelta(seconds=15)
    service = build_service(sessions, later, feed=RecordedMarkets(fresh_snapshot(later)))

    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.READY_FOR_RISK
    assert await effective_status(service.cases, case.id, later) is TradeCaseStatus.BLOCKED

    result = await service.request_risk_evaluation(case.id, request_key="hard-stale-key")

    assert result.reason is RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE
    assert result.detail == "BLOCKED"
    assert (await counts(sessions)) == (0, 0)


async def test_a_refusal_does_not_spend_the_case_s_one_request(risk_db, now, trace):
    """An ineligible moment must not consume what a later valid one needs."""
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(
        builder.cases, now, trigger_valid=now + timedelta(seconds=10), key="hard-spend"
    )
    later = now + timedelta(seconds=15)
    stale = build_service(sessions, later, feed=RecordedMarkets(fresh_snapshot(later)))
    refused = await stale.request_risk_evaluation(case.id, request_key="hard-spend-key")
    assert refused.kind == "risk_request_refused"

    # A fresh trigger restores eligibility, and the request is still available.
    previous = next(
        item
        for item in await stale.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.TRIGGER
    )
    setup = next(
        item
        for item in await stale.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.TRADE_SETUP
    )
    trigger = await record(
        stale.cases,
        case,
        later,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"hard-trigger-2-{case.id}",
        supersedes_id=previous.evidence_id,
    )
    # A new trigger is a new setup instant, so the execution assessment that
    # named the old one is no longer current. The workflow says so, and the
    # restoration has to satisfy it rather than talk around it.
    old_anchor = next(
        item
        for item in await stale.cases.evidence(case.id)
        if item.evidence_type is EvidenceType.LIQUIDITY_EXECUTION
    )
    await record(
        stale.cases,
        case,
        later,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        anchor_payload(setup.evidence_id, trigger.evidence_id),
        key=f"hard-anchor-2-{case.id}",
        supersedes_id=old_anchor.evidence_id,
    )
    granted = await stale.request_risk_evaluation(case.id, request_key="hard-spend-key")

    assert granted.kind == "risk_request_evaluated"
    assert granted.replayed is False
    assert (await counts(sessions)) == (1, 1)


@pytest.mark.parametrize(
    ("offset", "eligible"),
    [
        (timedelta(microseconds=-1), True),
        (timedelta(0), False),
        (timedelta(microseconds=1), False),
    ],
)
async def test_the_case_lifetime_boundary_is_exact(risk_db, now, trace, offset, eligible):
    """`now >= expires_at` ends a case, so the boundary instant is already over."""
    _, sessions = risk_db
    builder = build_service(sessions, now)
    lifetime = timedelta(seconds=20)
    case = await aging_case(
        builder.cases, now, lifetime=lifetime, key=f"hard-edge-{offset.microseconds}-{eligible}"
    )
    at = now + lifetime + offset
    service = build_service(sessions, at, feed=RecordedMarkets(fresh_snapshot(at)))

    result = await service.request_risk_evaluation(case.id, request_key=f"hard-edge-{eligible}")

    if eligible:
        assert result.kind == "risk_request_evaluated"
    else:
        assert result.reason is RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE
        assert result.detail == "EXPIRED"


async def test_expiry_during_the_reads_is_caught(risk_db, now, trace):
    """Time passing between the input reads and the decision instant.

    The readers take their own instants before the locks settle. A case that
    lapses in between would previously have been judged on the earlier reading.
    """
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(builder.cases, now, lifetime=timedelta(seconds=6), key="hard-during")
    clock = SteppingClock(now, step=timedelta(seconds=3))
    service = build_service(sessions, now, feed=RecordedMarkets(fresh_snapshot(now)), clock=clock)

    result = await service.request_risk_evaluation(case.id, request_key="hard-during-key")

    assert clock.reads >= 3, "the readers and the decision must read the clock separately"
    assert result.reason is RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE
    assert (await counts(sessions)) == (0, 0)


async def test_a_basis_that_expires_during_the_reads_is_refused(risk_db, now, trace):
    """The other half: the case is fine and the data it rests on is not.

    Both readings carry the horizon their own sources give them. Neither is
    extended here, and a decision taken after one lapsed is not a decision on it.
    """
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(builder.cases, now, key="hard-basis")
    # Old enough that the horizon falls between the readers' instants and the
    # decision instant, and fresh enough that each reader still accepts it.
    clock = SteppingClock(now, step=timedelta(seconds=3))
    aged = timedelta(seconds=85)
    feed = RecordedMarkets(fresh_snapshot(now, age=aged, metadata_age=aged))
    service = build_service(sessions, now, feed=feed, clock=clock)

    result = await service.request_risk_evaluation(case.id, request_key="hard-basis-key")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.DECISION_BASIS_EXPIRED
    assert (await counts(sessions)) == (0, 0)


async def test_a_fresh_case_still_reaches_a_verdict(risk_db, now, trace):
    """The control. The new precondition refuses lapsed cases, not ordinary ones."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-control")

    result = await service.request_risk_evaluation(case.id, request_key="hard-control-key")

    assert result.kind == "risk_request_evaluated"
    assert result.outcome is RiskOutcome.APPROVE
    assert (await counts(sessions)) == (1, 1)


async def test_replaying_a_stored_verdict_is_not_a_new_authorization(risk_db, now, trace):
    """A historical decision replays as history, whatever the case is now.

    The stored verdict comes back unchanged and marked `replayed`; nothing is
    re-evaluated, no binding is added, and the reading still authorises nothing.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-replay")
    first = await service.request_risk_evaluation(case.id, request_key="hard-replay-key")

    much_later = now + timedelta(days=2)
    stale = build_service(sessions, much_later, feed=RecordedMarkets(fresh_snapshot(much_later)))
    again = await stale.request_risk_evaluation(case.id, request_key="hard-replay-key")

    assert again.replayed is True
    assert again.risk_decision_id == first.risk_decision_id
    assert again.evaluated_at == first.evaluated_at
    assert again.authorizes_execution is False
    assert again.reserves_cash is False
    assert (await counts(sessions)) == (1, 1)


@pytest.mark.parametrize(
    ("lifetime", "trigger_seconds", "delay", "expected"),
    [
        # The case's own lifetime lapses while the workflow inputs are loading.
        (timedelta(seconds=10), None, timedelta(seconds=15), "EXPIRED"),
        # A safety envelope does, with the case itself still alive.
        (timedelta(hours=1), 10, timedelta(seconds=15), "BLOCKED"),
        # The control: the same path with no time passing at all.
        (timedelta(hours=1), None, timedelta(0), None),
    ],
)
async def test_time_passing_during_the_workflow_reads_is_counted(
    risk_db, now, trace, lifetime, trigger_seconds, delay, expected
):
    """The last gap: an instant taken before the last input read.

    `evaluate_in_session` used to take the caller's instant and *then* perform
    two database reads, so the verdict still described a moment before them.
    Loading and judging are separated now, with one clock read in between and
    nothing awaited after it.
    """
    _, sessions = risk_db
    builder = build_service(sessions, now)
    case = await aging_case(
        builder.cases,
        now,
        lifetime=lifetime,
        trigger_valid=None if trigger_seconds is None else now + timedelta(seconds=trigger_seconds),
        key=f"final-{expected}",
    )
    clock = MovingClock(now)
    service = build_service(
        sessions,
        now,
        clock=clock,
        cases=DelayedCases(sessions, clock=clock, delay=delay),
    )

    result = await service.request_risk_evaluation(case.id, request_key=f"final-key-{expected}")

    assert clock.instant == now + delay
    if expected is None:
        assert result.kind == "risk_request_evaluated"
        assert result.outcome is RiskOutcome.APPROVE
        assert (await counts(sessions)) == (1, 1)
    else:
        assert result.kind == "risk_request_refused"
        assert result.reason is RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE
        assert result.detail == expected
        assert (await counts(sessions)) == (0, 0)


def test_nothing_is_awaited_between_the_clock_read_and_the_verdict():
    """The ordering, asserted on the source rather than only in behaviour.

    One instant has to govern eligibility, the validity of the basis, every
    source age, the UTC loss day and SENTINEL. Any await in between would let
    the instant drift away from the state it describes.
    """
    import ast
    import inspect
    import textwrap

    from src.orchestration.riskrequest.service import RiskRequestService

    source = textwrap.dedent(inspect.getsource(RiskRequestService.request_risk_evaluation))
    tree = ast.parse(source)
    lines = source.splitlines()
    clock_read = next(index for index, line in enumerate(lines) if "now = self.clock.now()" in line)
    verdict = next(index for index, line in enumerate(lines) if "decision = evaluate(" in line)
    awaits = [
        node.lineno - 1
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and clock_read < node.lineno - 1 < verdict
    ]
    assert awaits == [], [lines[index].strip() for index in awaits]


# ============================================ 2. the evaluated snapshot is kept


async def test_the_evaluated_snapshot_is_reconstructible_without_a_market_read(risk_db, now, trace):
    """The reproduction: the basis kept provenance and not the values.

    After a commit and a reload, the snapshot `evaluate` was actually given comes
    back whole — and its fingerprint is the one the decision recorded, which is
    the only way that field can ever be checked again.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-basis-keep")
    await service.request_risk_evaluation(case.id, request_key="hard-basis-keep-key")

    reader = build_service(sessions, now, feed=RefusingMarkets())
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    market = MarketSnapshot.model_validate(row.basis["market_snapshot"])
    decision = RiskDecision.model_validate(row.basis["decision"])

    assert market.fingerprint() == decision.market_fingerprint
    assert market.id == decision.market_snapshot_id
    # Nothing beyond the stored row was needed: the feed handed to this reader
    # raises on any read, and none happened.
    assert isinstance(reader.markets, RefusingMarkets)
    with pytest.raises(AssertionError):
        await reader.markets.latest("anything")


async def test_the_stored_snapshot_keeps_the_raw_values_and_source_times(risk_db, now, trace):
    """Every figure SENTINEL judged, and the instant each source recorded it.

    The metadata is deliberately older than the price here — a shape the
    recorder can legitimately produce — so "each source keeps its own instant"
    is something the assertions can actually distinguish from an assembly time.
    """
    _, sessions = risk_db
    feed = RecordedMarkets(
        fresh_snapshot(now, age=timedelta(seconds=5), metadata_age=timedelta(seconds=20))
    )
    service = build_service(sessions, now, feed=feed)
    case = await ready_case(service.cases, now, trace, key="hard-values")
    await service.request_risk_evaluation(case.id, request_key="hard-values-key")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    market = MarketSnapshot.model_validate(row.basis["market_snapshot"])

    assert market.price_usd == Decimal("1.25")
    assert market.liquidity.liquidity_usd == Decimal("750000")
    assert market.holders.holder_count == 4200
    assert market.holders.top_ten_fraction == Decimal("0.31")
    assert market.fee_bps == Decimal("30")
    assert market.liquidity.estimated_slippage_bps == Decimal("25")
    # Each nested observation kept its own instant rather than an assembly time.
    assert market.observed_at == now - timedelta(seconds=5)
    assert market.token.created_at == now - timedelta(seconds=20)
    assert market.token.created_at < market.observed_at
    assert market.holders.created_at == now - timedelta(seconds=5)
    assert market.holders.created_at == market.holders.updated_at
    decided_at = RiskDecision.model_validate(row.basis["decision"]).evaluated_at
    assert {market.observed_at, market.token.created_at} != {decided_at}
    assert {market.token.asset_id, market.liquidity.asset_id, market.holders.asset_id} == {
        market.asset_id
    }


async def test_the_canonical_evidence_is_referenced_by_identity(risk_db, now, trace):
    """Each figure traces to the exact envelope that carried it."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-refs")
    await service.request_risk_evaluation(case.id, request_key="hard-refs-key")

    current = {item.evidence_type.value: item for item in await service.cases.evidence(case.id)}
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    stored = row.basis["evidence"]

    for kind in ("ONCHAIN_EVIDENCE", "LIQUIDITY_EXECUTION_EVIDENCE", "TRADE_SETUP_EVIDENCE"):
        assert stored[kind]["evidence_id"] == str(current[kind].evidence_id)
        assert stored[kind]["submission_fingerprint"] == current[kind].submission_fingerprint


async def test_the_snapshot_is_bound_into_the_request_digest(risk_db, now, trace):
    """Recorded is not enough: the identity has to cover it.

    A stored basis nothing hashed could be edited without trace, which is the
    opposite of what an audit record is for.
    """
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-digest")
    result = await service.request_risk_evaluation(case.id, request_key="hard-digest-key")

    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))

    assert risk_request_digest(row.basis) == result.risk_request_digest
    tampered = json.loads(json.dumps(row.basis))
    tampered["market_snapshot"]["price_usd"] = "999"
    assert risk_request_digest(tampered) != result.risk_request_digest
    without = json.loads(json.dumps(row.basis))
    del without["market_snapshot"]
    assert risk_request_digest(without) != result.risk_request_digest


async def test_replay_still_reads_the_stored_request(risk_db, now, trace):
    """Nothing about the larger basis changed how a retry is answered."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace, key="hard-replay-basis")
    first = await service.request_risk_evaluation(case.id, request_key="hard-replay-basis-key")

    reader = build_service(sessions, now, feed=RefusingMarkets())
    again = await reader.request_risk_evaluation(case.id, request_key="hard-replay-basis-key")

    assert again.replayed is True
    assert again.risk_request_digest == first.risk_request_digest
    assert again.intent_fingerprint == first.intent_fingerprint
