"""ANCHOR inside the real workflow and the real Phase 2B runtime.

Execution evidence is safety-critical and sits in the risk snapshot, so replacing
it revokes an authorisation granted against the old one. It is also bound to two
other pieces of evidence at once — a setup and the trigger that fired for it —
which gives it two ways to become stale and two races to survive.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.agents.anchor.context import AnchorContextReader
from src.agents.anchor.handler import ANCHOR_TASK_TYPE, AnchorWorkerHandler
from src.core.clock import FixedClock
from src.core.models import AgentRole, RiskDecision, RiskMetrics, RiskOutcome
from src.markets.quotes import QuoteFailure
from src.orchestration.worker.capabilities import AnchorCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskFailureReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1, authorized_task_type
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from src.orchestration.workflow.service import TradeCaseService
from tests.anchor.conftest import (
    StubMarkets,
    market_identity,
    setup_payload,
    source,
    task_input,
    trigger_payload,
)
from tests.anchor.test_context import snapshot_for
from tests.worker.conftest import atlas_payload, signal_payload, submission

CASE_LIFETIME = timedelta(hours=6)


def build_stack(sessions, instant, *, quotes=None, snapshot=None):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = AnchorContextReader(
        cases=cases,
        markets=StubMarkets(snapshot if snapshot is not None else snapshot_for(instant)),
        quotes=quotes if quotes is not None else source(instant),
        clock=clock,
        include_fixtures=True,
    )
    return runtime, reader


async def open_case(cases, now, trace, key):
    return await cases.open_trade_case(
        market_identity(),
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + CASE_LIFETIME,
    )


async def record(cases, trade_case, now, role, evidence_type, payload, *, key, **kw):
    return await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case, now, role, evidence_type, payload, key=f"{trade_case.id}:{key}", **kw
        ),
    )


async def triggered_case(cases, trade_case, now, **setup_kw):
    """Drive a case to the point where execution evidence is the next thing needed."""
    for role, evidence_type, payload, name in (
        (AgentRole.ATLAS, EvidenceType.ONCHAIN, atlas_payload(), "chain"),
        (AgentRole.SIGNAL, EvidenceType.SENTIMENT, signal_payload(), "sentiment"),
    ):
        await record(cases, trade_case, now, role, evidence_type, payload, key=name)
    setup = await record(
        cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(now, **setup_kw),
        key="setup",
        valid_until=now + timedelta(hours=1),
    )
    trigger = await record(
        cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key="trigger",
        valid_until=now + timedelta(hours=1),
    )
    return setup, trigger


async def run_anchor(runtime, reader, key):
    runner = WorkerRunner(
        runtime,
        AnchorWorkerHandler(quote_provider="fixture:quotes"),
        CapabilityProvider(service=runtime, anchor=reader),
        registration_key=key,
    )
    await runner.register()
    return await runner.run_once()


async def execution_evidence(cases, trade_case_id):
    return [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type == EvidenceType.LIQUIDITY_EXECUTION
    ]


# --------------------------------------------------- what the repository says


def test_anchor_is_required_and_safety_critical():
    """Audited, not assumed."""
    requirement = TRADE_CASE_V1.requirement(EvidenceType.LIQUIDITY_EXECUTION)
    assert requirement.role == AgentRole.ANCHOR
    assert requirement.required is True
    assert requirement.safety_critical is True
    assert requirement.before_trigger is False
    assert requirement.task_type == ANCHOR_TASK_TYPE
    assert authorized_task_type(AgentRole.ANCHOR) == ANCHOR_TASK_TYPE
    # Execution conditions are a safety input, so they enter the risk snapshot.
    assert EvidenceType.LIQUIDITY_EXECUTION in TRADE_CASE_V1.safety_types


# ------------------------------------------------------------ the happy path


async def test_a_bracketed_capacity_becomes_evidence_and_reaches_the_risk_stage(
    worker_db, now, trace
):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-ok")
    setup, trigger = await triggered_case(runtime.cases, trade_case, now)

    disposition = await run_anchor(runtime, reader, "anchor-ok-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    envelope = (await execution_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.producer_role == AgentRole.ANCHOR
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert envelope.payload.setup_evidence_id == setup.evidence_id
    assert envelope.payload.trigger_evidence_id == trigger.evidence_id

    detail = envelope.payload.execution
    assert detail is not None
    assert detail.capacity_semantics == "BOUNDED"
    assert detail.largest_tested_acceptable_notional_usd == Decimal(2500)
    assert detail.first_tested_rejected_notional_usd == Decimal(10000)
    assert len(detail.ladder) == 4

    # The case now waits for risk, which is SENTINEL's question and not ANCHOR's.
    assert (
        await runtime.cases.get_trade_case(trade_case.id)
    ).status == TradeCaseStatus.READY_FOR_RISK


async def test_anchor_authorizes_nothing(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-noauth")
    await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, "anchor-noauth-worker")

    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status not in {TradeCaseStatus.RISK_APPROVED, TradeCaseStatus.RISK_LIMITED}
    rendered = (await execution_evidence(runtime.cases, trade_case.id))[0].model_dump_json()
    for forbidden in ("position_size", "risk_outcome", "approved", "authorization"):
        assert f'"{forbidden}"' not in rendered


async def test_the_legacy_scalar_never_over_claims(worker_db, now, trace):
    """It is a size actually tested and accepted, so it can only under-state."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-legacy")
    await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, "anchor-legacy-worker")

    payload = (await execution_evidence(runtime.cases, trade_case.id))[0].payload
    assert payload.maximum_safe_size_usd == payload.execution.largest_tested_acceptable_notional_usd
    # And anything reasoning about capacity has the semantics beside the figure.
    assert payload.execution.capacity_semantics in {"BOUNDED", "AT_LEAST", "NONE", "UNKNOWN"}


# ------------------------------------------- known bad versus unknown


async def test_a_market_that_cannot_be_traded_blocks_the_case(worker_db, now, trace):
    """Known bad is evidence. The provider answered and the answer was no."""
    _, sessions = worker_db
    runtime, reader = build_stack(
        sessions, now, quotes=source(now, always_fails=QuoteFailure.NO_ROUTE)
    )
    trade_case = await open_case(runtime.cases, now, trace, "anchor-noroute")
    await triggered_case(runtime.cases, trade_case, now)

    disposition = await run_anchor(runtime, reader, "anchor-noroute-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    envelope = (await execution_evidence(runtime.cases, trade_case.id))[0]
    # Available, because the fact was obtainable. Blocked, because the fact is bad.
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.BLOCKED
    assert envelope.payload.execution.capacity_semantics == "NONE"

    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.BLOCKED
    assert any(blocker.role == AgentRole.ANCHOR for blocker in updated.blockers)


@pytest.mark.parametrize(
    "failure", [QuoteFailure.TIMEOUT, QuoteFailure.RATE_LIMITED, QuoteFailure.PROVIDER_UNAVAILABLE]
)
async def test_a_provider_outage_writes_no_evidence_at_all(worker_db, now, trace, failure):
    """Scenarios E and F. An absence of evidence is never recorded as a fact."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, quotes=source(now, always_fails=failure))
    trade_case = await open_case(runtime.cases, now, trace, f"anchor-out-{failure.value}")
    await triggered_case(runtime.cases, trade_case, now)

    disposition = await run_anchor(runtime, reader, f"anchor-out-{failure.value}-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.FAILED_RETRYABLE
    assert await execution_evidence(runtime.cases, trade_case.id) == []
    # And the case is still merely waiting, not blocked on a market claim.
    assert (
        await runtime.cases.get_trade_case(trade_case.id)
    ).status == TradeCaseStatus.EXECUTION_EVIDENCE_PENDING


# -------------------------------------- V: the trigger fired, execution says no


async def test_scenario_v_a_market_that_moved_can_refuse_what_a_trigger_permitted(
    worker_db, now, trace
):
    """PULSE recorded a fact. ANCHOR evaluates now, and may disagree.

    A crossing followed by a reversion is precisely why a trigger is not an
    execution guarantee. The trigger evidence stays intact — it describes
    something that happened — while the case stops on execution conditions.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(
        sessions, now, quotes=source(now, deviation_bps_per_step=Decimal(5000))
    )
    trade_case = await open_case(runtime.cases, now, trace, "anchor-reverted")
    _, trigger = await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, "anchor-reverted-worker")

    envelope = (await execution_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.payload.execution.capacity_semantics == "NONE"
    assert (await runtime.cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.BLOCKED

    # The trigger is untouched and still current.
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    assert current[EvidenceType.TRIGGER].evidence_id == trigger.evidence_id
    assert current[EvidenceType.TRIGGER].payload.acceptance() == EvidenceAcceptance.ACCEPTED


# ------------------------------------------------- M, N: the two races


async def test_scenario_m_execution_evidence_for_a_superseded_setup_is_rejected(
    worker_db, now, trace
):
    """Read setup A and its trigger, VECTOR publishes B, submit for A."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-race-setup")
    setup, _ = await triggered_case(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="anchor-race-setup-worker",
            role=AgentRole.ANCHOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        lease, CapabilityProvider(service=runtime, anchor=reader).build(lease)
    )
    assert isinstance(report, EvidenceTaskResult)

    # VECTOR replaces the setup while the ladder was being walked.
    await record(
        runtime.cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        setup_payload(now),
        key="setup-b",
        supersedes_id=setup.evidence_id,
        valid_until=now + timedelta(hours=1),
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.TASK_SUPERSEDED
    assert await execution_evidence(runtime.cases, trade_case.id) == []


async def test_scenario_n_execution_evidence_for_a_superseded_trigger_is_rejected(
    worker_db, now, trace
):
    """The other binding. Both must still be current at submission."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-race-trigger")
    setup, trigger = await triggered_case(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="anchor-race-trigger-worker",
            role=AgentRole.ANCHOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        lease, CapabilityProvider(service=runtime, anchor=reader).build(lease)
    )

    await record(
        runtime.cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id, observed_price=Decimal("211.00")),
        key="trigger-b",
        supersedes_id=trigger.evidence_id,
        valid_until=now + timedelta(hours=1),
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.TASK_SUPERSEDED


# ------------------------------------------------ O, P: risk invalidation


def risk_decision(trade_case, now):
    return RiskDecision(
        source="SENTINEL",
        correlation_id=trade_case.correlation_id,
        created_at=now,
        updated_at=now,
        intent_id=uuid4(),
        intent_fingerprint="intent",
        market_snapshot_id=uuid4(),
        market_fingerprint="market",
        outcome=RiskOutcome.APPROVE,
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
        expires_at=now + timedelta(hours=2),
    )


async def authorized(sessions, now, trace, key, *, limited=False):
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, key)
    await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, f"{key}-worker")

    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    decision = risk_decision(ready, now)
    if limited:
        decision = decision.model_copy(
            update={
                "outcome": RiskOutcome.REJECT,
                "reason_codes": ("MAX_POSITION_SIZE",),
                "max_additional_notional_usd": Decimal("137.125"),
            }
        )
    bound = await runtime.cases.record_risk_decision(
        trade_case.id, decision, risk_input_digest=ready.risk_input_digest
    )
    return runtime, trade_case, ready.risk_input_digest, bound


async def test_execution_evidence_participates_in_the_risk_digest(worker_db, now, trace):
    """Proof rather than assumption: removing it changes the snapshot."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-digest")
    await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, "anchor-digest-worker")

    case = await runtime.cases.get_trade_case(trade_case.id)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    without = {k: v for k, v in current.items() if k != EvidenceType.LIQUIDITY_EXECUTION}
    assert risk_input_digest(case, current) != risk_input_digest(case, without)


@pytest.mark.parametrize("limited", [False, True])
async def test_scenarios_o_and_p_new_execution_evidence_revokes_an_authorization(
    worker_db, now, trace, limited
):
    """Both authorized states are revalidatable, and execution is a safety input."""
    _, sessions = worker_db
    key = "anchor-risk-limited" if limited else "anchor-risk-approved"
    runtime, trade_case, before_digest, bound = await authorized(
        sessions, now, trace, key, limited=limited
    )
    expected = TradeCaseStatus.RISK_LIMITED if limited else TradeCaseStatus.RISK_APPROVED
    assert bound.status == expected

    # A later assessment finds the market will support less.
    later = now + timedelta(minutes=1)
    later_runtime, later_reader = build_stack(
        sessions, later, quotes=source(later, deviation_bps_per_step=Decimal(60))
    )
    refreshed = await later_reader.execution_context(trade_case.id, uuid4())
    handler = AnchorWorkerHandler(quote_provider="fixture:quotes")
    report = await handler.handle(
        _lease(trade_case, refreshed, later, trace),
        AnchorCapabilities(
            lease=_lease(trade_case, refreshed, later, trace),
            context=_Fixed(refreshed),
            submit=_NoSubmit(),
        ),
    )
    assert isinstance(report, EvidenceTaskResult)
    await later_runtime.cases.record_evidence(
        trade_case.id,
        report.submission.model_copy(
            update={
                "idempotency_key": f"{trade_case.id}:anchor-b",
                "supersedes_id": (await execution_evidence(later_runtime.cases, trade_case.id))[
                    0
                ].evidence_id,
            }
        ),
    )

    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status != expected
    assert revoked.risk_input_digest != before_digest


def _lease(trade_case, context, now, trace):
    from src.orchestration.worker.models import TaskLease

    return TaskLease(
        lease_id=uuid4(),
        task_id=context.task_id,
        trade_case_id=trade_case.id,
        role=AgentRole.ANCHOR,
        task_type=ANCHOR_TASK_TYPE,
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace,
    )


class _Fixed:
    def __init__(self, context) -> None:
        self._context = context

    async def execution_context(self, trade_case_id, task_id):
        return self._context


class _NoSubmit:
    def __init__(self) -> None:
        self.lease = None

    async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
        raise AssertionError("the handler must not submit directly")


# ------------------------------------------------ refusing the wrong surface


async def test_the_wrong_capability_is_refused_before_anything_else(now, trace):
    """Nothing is read, quoted or assessed until the surface is the right one."""
    context = task_input(now)
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        _lease(SimpleNamespace(id=context.trade_case_id), context, now, trace), object()
    )
    assert isinstance(report, TaskFailureReport)
    assert report.category == WorkerFailureCategory.CAPABILITY_DENIED
    assert report.reason_code == "CAPABILITY_MISMATCH"


async def test_a_context_port_returning_the_wrong_shape_is_caught(now, trace):
    """An assessment is never built from something that merely looks like context."""

    class WrongShape:
        async def execution_context(self, trade_case_id, task_id):
            return {"largest_tested_acceptable_notional_usd": "1000000"}

    context = task_input(now)
    lease = _lease(SimpleNamespace(id=context.trade_case_id), context, now, trace)
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        lease, AnchorCapabilities(lease=lease, context=WrongShape(), submit=_NoSubmit())
    )
    assert isinstance(report, TaskFailureReport)
    assert report.category == WorkerFailureCategory.INTERNAL
    assert report.reason_code == "CONTEXT_SCHEMA_MISMATCH"


# --------------------------------------------------- S, T: runtime fencing


async def test_scenario_s_a_worker_that_lost_its_lease_cannot_submit(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-lease")
    await triggered_case(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="anchor-lease-slow",
            role=AgentRole.ANCHOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        lease, CapabilityProvider(service=runtime, anchor=reader).build(lease)
    )

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    later_runtime, _ = build_stack(sessions, later)
    with pytest.raises(WorkerFailure) as caught:
        await later_runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert await execution_evidence(runtime.cases, trade_case.id) == []


async def test_scenario_t_a_replayed_assessment_creates_one_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-replay")
    await triggered_case(runtime.cases, trade_case, now)

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="anchor-replay-worker",
            role=AgentRole.ANCHOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
        lease, CapabilityProvider(service=runtime, anchor=reader).build(lease)
    )
    first = await runtime.submit_task_result(lease, report)
    replay = await runtime.submit_task_result(lease, report)

    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert len(await execution_evidence(runtime.cases, trade_case.id)) == 1
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1


async def test_anchor_may_only_submit_execution_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-role")
    await triggered_case(runtime.cases, trade_case, now)
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="anchor-role-worker",
            role=AgentRole.ANCHOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    smuggled = EvidenceTaskResult(
        submission=submission(
            trade_case,
            now,
            AgentRole.SIGNAL,
            EvidenceType.SENTIMENT,
            signal_payload(),
            key="ignored",
        ),
        result_key="smuggled",
    )
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, smuggled)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


# ---------------------------------------------------------- staleness


async def test_execution_evidence_stops_being_current_quickly(worker_db, now, trace):
    """An offer is a moment. Nothing may treat an old one as live."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-stale")
    await triggered_case(runtime.cases, trade_case, now)
    await run_anchor(runtime, reader, "anchor-stale-worker")

    envelope = (await execution_evidence(runtime.cases, trade_case.id))[0]
    after = envelope.valid_until + timedelta(seconds=1)
    assert envelope.effective_status(after) == EvidenceStatus.STALE

    later_runtime, _ = build_stack(sessions, after)
    stopped = await later_runtime.cases.evaluate_trade_case(trade_case.id)
    assert stopped.status == TradeCaseStatus.BLOCKED


# ---------------------------------------------------------- compatibility


def test_a_legacy_execution_payload_still_parses():
    """Phase 2A evidence predates the assessment and must stay readable."""
    from src.orchestration.workflow.models import LiquidityExecutionPayload

    payload = LiquidityExecutionPayload.model_validate(
        {
            "kind": "liquidity_execution",
            "setup_evidence_id": str(uuid4()),
            "trigger_evidence_id": str(uuid4()),
            "quoted_price": "1.00",
            "maximum_safe_size_usd": "2500",
        }
    )
    assert payload.execution is None
    assert payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert LiquidityExecutionPayload.model_validate_json(payload.model_dump_json()) == payload


def test_the_other_specialists_are_untouched():
    from src.agents.atlas.prompt import ATLAS_PROMPT_VERSION
    from src.agents.orbit.prompt import ORBIT_PROMPT_VERSION
    from src.agents.pulse.policy import PULSE_TRIGGER_V1
    from src.agents.signal.prompt import SIGNAL_PROMPT_VERSION
    from src.agents.vector.prompt import VECTOR_PROMPT_VERSION

    assert (ORBIT_PROMPT_VERSION, ATLAS_PROMPT_VERSION, SIGNAL_PROMPT_VERSION) == (
        "orbit-v1",
        "atlas-v1",
        "signal-v1",
    )
    assert VECTOR_PROMPT_VERSION == "vector-v2"
    assert PULSE_TRIGGER_V1.version == "pulse-trigger-v1"


async def test_an_identical_assessment_recomputed_later_replays_rather_than_conflicting(
    worker_db, now, trace
):
    """The same defect an independent review found in FUSE, in this worker too.

    `ExecutionAssessmentDetail` carried its own evaluation timestamp, which is
    run metadata rather than part of the assessment — so two assessments of
    identical quotes produced one idempotency key with two submission
    fingerprints, which the runtime correctly refuses as a conflict. It was not
    reported here, and it was the same root cause.
    """
    _, sessions = worker_db
    quotes = source(now)
    runtime, _ = build_stack(sessions, now, quotes=quotes)
    trade_case = await open_case(runtime.cases, now, trace, "anchor-replay-identical")
    await triggered_case(runtime.cases, trade_case, now)

    # One fixed market observation for both runs. Letting the fixture derive a
    # snapshot from each reader's clock would vary the *inputs*, which is a
    # different experiment from recomputing the same assessment.
    fixed_snapshot = snapshot_for(now)

    async def assess_at(instant):
        _, reader = build_stack(sessions, instant, quotes=source(now), snapshot=fixed_snapshot)
        context = await reader.execution_context(trade_case.id, uuid4())
        lease = _lease(trade_case, context, instant, trace)
        report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
            lease, AnchorCapabilities(lease=lease, context=_Fixed(context), submit=_NoSubmit())
        )
        assert isinstance(report, EvidenceTaskResult)
        return report.submission

    first = await assess_at(now)
    second = await assess_at(now + timedelta(seconds=1))

    assert first.idempotency_key == second.idempotency_key
    assert first.fingerprint() == second.fingerprint()

    await runtime.cases.record_evidence(trade_case.id, first)
    await runtime.cases.record_evidence(trade_case.id, second)
    stored = [
        item
        for item in await runtime.cases.evidence(trade_case.id)
        if item.producer_role == AgentRole.ANCHOR
    ]
    assert len(stored) == 1


async def test_a_genuinely_different_assessment_is_still_a_different_result(worker_db, now, trace):
    """The control: removing run metadata must not hide a real change."""
    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now, quotes=source(now))
    trade_case = await open_case(runtime.cases, now, trace, "anchor-replay-different")
    await triggered_case(runtime.cases, trade_case, now)

    async def assess_with(quotes):
        _, reader = build_stack(sessions, now, quotes=quotes)
        context = await reader.execution_context(trade_case.id, uuid4())
        lease = _lease(trade_case, context, now, trace)
        report = await AnchorWorkerHandler(quote_provider="fixture:quotes").handle(
            lease, AnchorCapabilities(lease=lease, context=_Fixed(context), submit=_NoSubmit())
        )
        assert isinstance(report, EvidenceTaskResult)
        return report.submission

    shallow = await assess_with(source(now, fails_above=Decimal(2500)))
    deep = await assess_with(source(now))
    assert shallow.idempotency_key != deep.idempotency_key
    assert shallow.fingerprint() != deep.fingerprint()
