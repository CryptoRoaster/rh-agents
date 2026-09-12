"""VECTOR inside the real workflow and the real Phase 2B runtime.

What the system does with a setup is stricter than for any earlier specialist.
TRADE_SETUP is safety-critical and sits in the risk snapshot, so a new setup does
not merely replace the old one: it invalidates the trigger that was watching it,
the execution assessment built on it, and any risk authorization granted against
it. Those semantics are Phase 2A's, not this phase's — they are pinned here so a
later change has to be deliberate.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.vector.context import VectorContextReader
from src.agents.vector.handler import VECTOR_TASK_TYPE, VectorWorkerHandler
from src.core.clock import FixedClock
from src.core.models import AgentRole, RiskDecision, RiskMetrics, RiskOutcome
from src.orchestration.worker.capabilities import VectorCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
    TradeSetupPayload,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1
from src.orchestration.workflow.service import TradeCaseService
from src.reasoning.fake import DeterministicReasoningProvider
from tests.vector.conftest import StubMarkets, market_identity
from tests.vector.test_context import snapshot_for
from tests.vector.test_scenarios import Fixed, NoSubmit, lease_for, reply
from tests.worker.conftest import (
    anchor_payload,
    atlas_payload,
    signal_payload,
    submission,
    trigger_payload,
)

# The case outlives every setup in these tests, so an expiring setup is never
# confused with an expiring case.
CASE_LIFETIME = timedelta(hours=6)


def proposal(instant, **overrides):
    """A scripted reply that cites nothing it was not shown.

    The unit fixtures cite synthetic observation ids. Here the observations are
    the real snapshot's, so an uncited proposal is the honest default and the one
    test that does cite reads the identifier out of the assembled context.
    """
    overrides.setdefault("cited_observation_ids", ())
    return reply(instant, **overrides)


def build_stack(sessions, instant, *, snapshot=None):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = VectorContextReader(
        cases=cases,
        markets=StubMarkets(snapshot_for(instant) if snapshot is None else snapshot),
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


async def run_vector(runtime, reader, key, *, instant=None):
    handler = VectorWorkerHandler(
        provider=DeterministicReasoningProvider.returning(proposal(instant or runtime.clock.now()))
    )
    runner = WorkerRunner(
        runtime,
        handler,
        CapabilityProvider(service=runtime, setup=reader),
        registration_key=key,
    )
    await runner.register()
    return await runner.run_once()


async def setup_evidence(cases, trade_case_id):
    return [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type == EvidenceType.TRADE_SETUP
    ]


async def current_digest(cases, trade_case_id):
    """Recompute the canonical risk digest from what is currently active."""
    trade_case = await cases.get_trade_case(trade_case_id)
    evidence = await cases.evidence(trade_case_id)
    return risk_input_digest(trade_case, active_evidence(evidence))


async def surround_setup(cases, trade_case, setup_id, now):
    """Everything except the setup, so the case can reach READY_FOR_RISK."""
    await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case, now, AgentRole.ATLAS, EvidenceType.ONCHAIN, atlas_payload(), key="v-chain"
        ),
    )
    await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case, now, AgentRole.SIGNAL, EvidenceType.SENTIMENT, signal_payload(), key="v-sig"
        ),
    )
    trigger = await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup_id),
            key="v-trig",
        ),
    )
    anchor = await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            anchor_payload(setup_id, trigger.evidence_id),
            key="v-anch",
        ),
    )
    return trigger, anchor


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


async def replace_setup(sessions, trade_case_id, instant, trace, **overrides):
    """Produce a second setup through the real handler and record it.

    The VECTOR task is terminal after its first success, so a periodic
    re-assessment would need a trigger Phase 2H does not introduce. The
    supersession semantics are exercised through the ordinary evidence service,
    exactly as Phase 2D did for ATLAS.
    """
    runtime, reader = build_stack(sessions, instant)
    refreshed = await reader.setup_context(trade_case_id, uuid4())
    handler = VectorWorkerHandler(
        provider=DeterministicReasoningProvider.returning(proposal(instant, **overrides))
    )
    lease = lease_for(refreshed, instant, trace)
    report = await handler.handle(
        lease, VectorCapabilities(lease=lease, context=Fixed(refreshed), submit=NoSubmit())
    )
    assert isinstance(report, EvidenceTaskResult)
    await runtime.cases.record_evidence(trade_case_id, report.submission)
    return runtime, refreshed


# ----------------------------------------------- what the repository says


def test_vector_is_required_and_safety_critical():
    """Audited, not assumed: a setup is a risk input, unlike sentiment."""
    requirement = TRADE_CASE_V1.requirement(EvidenceType.TRADE_SETUP)
    assert requirement.role == AgentRole.VECTOR
    assert requirement.required is True
    assert requirement.safety_critical is True
    assert requirement.before_trigger is True
    assert requirement.task_type == VECTOR_TASK_TYPE
    assert EvidenceType.TRADE_SETUP in TRADE_CASE_V1.safety_types


# ---------------------------------------------------------- the happy path


async def test_a_coherent_setup_satisfies_the_requirement(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-ok")

    disposition = await run_vector(runtime, reader, "vector-ok-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    envelope = (await setup_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.producer_role == AgentRole.VECTOR
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert envelope.payload.setup is not None
    assert envelope.supersedes_id is None

    updated = await runtime.cases.get_trade_case(trade_case.id)
    # The setup no longer blocks; the case waits on the other specialists.
    assert updated.status == TradeCaseStatus.EVIDENCE_PENDING
    assert not any(blocker.role == AgentRole.VECTOR for blocker in updated.blockers)


async def test_a_setup_alone_authorizes_nothing(worker_db, now, trace):
    """A proposal is not an approval, and the case status says so."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-unauthorized")
    await run_vector(runtime, reader, "vector-unauthorized-worker")

    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status not in {
        TradeCaseStatus.RISK_APPROVED,
        TradeCaseStatus.RISK_LIMITED,
        TradeCaseStatus.TRIGGERED,
    }
    assert updated.risk_input_digest is None


# ------------------------------------------------ K: setup supersession


async def test_scenario_k_a_new_setup_invalidates_the_trigger_watching_the_old_one(
    worker_db, now, trace
):
    """The trigger and the execution assessment both named setup A. A is gone."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-supersede")
    await run_vector(runtime, reader, "vector-supersede-worker")
    first = (await setup_evidence(runtime.cases, trade_case.id))[0]

    trigger, anchor = await surround_setup(runtime.cases, trade_case, first.evidence_id, now)
    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK

    later = now + timedelta(minutes=10)
    later_runtime, refreshed = await replace_setup(
        sessions,
        trade_case.id,
        later,
        trace,
        entry_low=Decimal("1.20"),
        entry_high=Decimal("1.20"),
    )
    assert refreshed.supersedes_evidence_id == first.evidence_id

    envelopes = await setup_evidence(later_runtime.cases, trade_case.id)
    assert len(envelopes) == 2
    second = next(item for item in envelopes if item.supersedes_id is not None)
    assert second.supersedes_id == first.evidence_id
    assert second.payload.entry_price == Decimal("1.20")

    # The case backtracks: the trigger is no longer about the current setup, and
    # the execution assessment underneath it is not current either.
    backtracked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert backtracked.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert backtracked.reason_code == "TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP"
    assert any(
        blocker.code == "PULSE_TRIGGER_SETUP_MISMATCH"
        and blocker.evidence_id == trigger.evidence_id
        for blocker in backtracked.blockers
    )
    assert anchor.payload.setup_evidence_id == first.evidence_id


async def test_only_one_setup_is_ever_active(worker_db, now, trace):
    """Two live envelopes of one type are an integrity fault, never a race winner."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-single")
    await run_vector(runtime, reader, "vector-single-worker")

    later = now + timedelta(minutes=10)
    later_runtime, _ = await replace_setup(
        sessions,
        trade_case.id,
        later,
        trace,
        entry_low=Decimal("1.20"),
        entry_high=Decimal("1.20"),
    )
    current = active_evidence(await later_runtime.cases.evidence(trade_case.id))
    assert current[EvidenceType.TRADE_SETUP].payload.entry_price == Decimal("1.20")


# ------------------------------------------------- L: risk invalidation


async def test_the_setup_participates_in_the_canonical_risk_digest(worker_db, now, trace):
    """Proof rather than assumption: removing the setup changes the digest."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-digest")
    await run_vector(runtime, reader, "vector-digest-worker")

    case = await runtime.cases.get_trade_case(trade_case.id)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    without = {k: v for k, v in current.items() if k != EvidenceType.TRADE_SETUP}
    assert risk_input_digest(case, current) != risk_input_digest(case, without)


async def test_scenario_l_a_new_setup_revokes_an_approval(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-risk-approved")
    await run_vector(runtime, reader, "vector-risk-approved-worker")
    first = (await setup_evidence(runtime.cases, trade_case.id))[0]
    await surround_setup(runtime.cases, trade_case, first.evidence_id, now)

    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    before_digest = ready.risk_input_digest
    assert before_digest == await current_digest(runtime.cases, trade_case.id)

    approved = await runtime.cases.record_risk_decision(
        trade_case.id, risk_decision(ready, now), risk_input_digest=before_digest
    )
    assert approved.status == TradeCaseStatus.RISK_APPROVED

    later = now + timedelta(minutes=10)
    later_runtime, _ = await replace_setup(
        sessions,
        trade_case.id,
        later,
        trace,
        entry_low=Decimal("1.20"),
        entry_high=Decimal("1.20"),
    )

    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status != TradeCaseStatus.RISK_APPROVED
    # The authorization was pinned to a digest that no longer describes the case.
    assert await current_digest(later_runtime.cases, trade_case.id) != before_digest
    assert revoked.risk_input_digest != before_digest


async def test_scenario_l_a_new_setup_also_revokes_a_limited_authorization(worker_db, now, trace):
    """RISK_LIMITED is an authorization too, and it is revoked the same way."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-risk-limited")
    await run_vector(runtime, reader, "vector-risk-limited-worker")
    first = (await setup_evidence(runtime.cases, trade_case.id))[0]
    await surround_setup(runtime.cases, trade_case, first.evidence_id, now)

    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    before_digest = ready.risk_input_digest

    # A resizable sizing-only rejection with bounded capacity: LIMITED, not APPROVED.
    limited_decision = risk_decision(ready, now).model_copy(
        update={
            "outcome": RiskOutcome.REJECT,
            "reason_codes": ("MAX_POSITION_SIZE",),
            "max_additional_notional_usd": Decimal("137.125"),
        }
    )
    limited = await runtime.cases.record_risk_decision(
        trade_case.id, limited_decision, risk_input_digest=before_digest
    )
    assert limited.status == TradeCaseStatus.RISK_LIMITED

    later = now + timedelta(minutes=10)
    later_runtime, _ = await replace_setup(
        sessions,
        trade_case.id,
        later,
        trace,
        entry_low=Decimal("1.20"),
        entry_high=Decimal("1.20"),
    )
    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status != TradeCaseStatus.RISK_LIMITED
    assert revoked.risk_input_digest != before_digest


# ------------------------------------------------------ M: lease expiry


async def test_scenario_m_a_worker_that_lost_its_lease_cannot_submit(worker_db, now, trace):
    """Reasoning outlived the lease. The setup arrives with no authority behind it."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-lease")

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="vector-lease-slow",
            role=AgentRole.VECTOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    assert lease.trade_case_id == trade_case.id
    assert lease.task_type == VECTOR_TASK_TYPE

    handler = VectorWorkerHandler(provider=DeterministicReasoningProvider.returning(proposal(now)))
    report = await handler.handle(
        lease, CapabilityProvider(service=runtime, setup=reader).build(lease)
    )
    assert isinstance(report, EvidenceTaskResult)

    later = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    later_runtime, _ = build_stack(sessions, later)
    with pytest.raises(WorkerFailure) as caught:
        await later_runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert await setup_evidence(runtime.cases, trade_case.id) == []


# ---------------------------------------------------------- N, O: replay


async def test_scenario_n_a_replayed_submission_creates_nothing_twice(worker_db, now, trace):
    """The result committed, the acknowledgement was lost, the worker retried."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-replay")

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="vector-replay-worker",
            role=AgentRole.VECTOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    handler = VectorWorkerHandler(provider=DeterministicReasoningProvider.returning(proposal(now)))
    report = await handler.handle(
        lease, CapabilityProvider(service=runtime, setup=reader).build(lease)
    )

    first = await runtime.submit_task_result(lease, report)
    assert first.replayed is False
    replay = await runtime.submit_task_result(lease, report)
    assert replay.replayed is True
    assert replay.outcome == first.outcome
    assert len(await setup_evidence(runtime.cases, trade_case.id)) == 1
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1


async def test_scenario_o_a_different_setup_on_the_same_attempt_is_refused(worker_db, now, trace):
    """At-least-once model calls may differ; one attempt still has one answer.

    The runtime checks replay before it checks the lease, so an identical
    resubmission is idempotent while a divergent one is refused outright: a
    successful submission released the lease, and the attempt that already
    answered has no standing to answer differently.
    """
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-divergent")

    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="vector-divergent-worker",
            role=AgentRole.VECTOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    capabilities = CapabilityProvider(service=runtime, setup=reader).build(lease)

    handler = VectorWorkerHandler(provider=DeterministicReasoningProvider.returning(proposal(now)))
    first = await handler.handle(lease, capabilities)
    await runtime.submit_task_result(lease, first)

    divergent = VectorWorkerHandler(
        provider=DeterministicReasoningProvider.returning(
            proposal(now, entry_low=Decimal("1.20"), entry_high=Decimal("1.20"))
        )
    )
    second = await divergent.handle(lease, capabilities)
    assert isinstance(second, EvidenceTaskResult)
    assert second.result_key != first.result_key
    with pytest.raises(WorkerFailure) as caught:
        await runtime.submit_task_result(lease, second)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert len(await setup_evidence(runtime.cases, trade_case.id)) == 1
    # The first setup stands; the divergent one left no trace at all.
    envelope = (await setup_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.payload.entry_price == Decimal("1.10")


async def test_vector_may_only_submit_trade_setup_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    await open_case(runtime.cases, now, trace, "vector-role")
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="vector-role-worker",
            role=AgentRole.VECTOR,
            runtime_version="worker-runtime-v1",
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    assert lease.role == AgentRole.VECTOR

    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
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


# ----------------------------------------------------- P: the setup expires


async def test_scenario_p_an_expired_setup_blocks_rather_than_lingers(worker_db, now, trace):
    """An expired proposal is not a current opinion, and the case stops."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "vector-expiry")
    await run_vector(runtime, reader, "vector-expiry-worker")

    envelope = (await setup_evidence(runtime.cases, trade_case.id))[0]
    detail = envelope.payload.setup
    assert detail is not None
    # The envelope stops being current exactly when the setup does.
    assert envelope.valid_until == detail.expires_at

    after = detail.expires_at + timedelta(minutes=1)
    assert after < now + CASE_LIFETIME
    assert envelope.effective_status(after) == EvidenceStatus.STALE

    later_runtime, _ = build_stack(sessions, after)
    stopped = await later_runtime.cases.evaluate_trade_case(trade_case.id)
    assert stopped.status == TradeCaseStatus.BLOCKED
    assert stopped.reason_code == "SAFETY_EVIDENCE_BLOCKED"
    assert any(
        blocker.role == AgentRole.VECTOR and blocker.code == "VECTOR_STALE"
        for blocker in stopped.blockers
    )
    # And nothing was silently renewed in place of the expired proposal.
    assert len(await setup_evidence(later_runtime.cases, trade_case.id)) == 1


# ------------------------------------------------------- Q: wrong market


async def test_scenario_q_an_observation_for_another_market_produces_no_setup(
    worker_db, now, trace
):
    """A wiring fault, answered before the model and never turned into levels."""
    _, sessions = worker_db
    runtime, reader = build_stack(
        sessions, now, snapshot=snapshot_for(now, pair_id="robinhood:mainnet:contract_address:0xff")
    )
    trade_case = await open_case(runtime.cases, now, trace, "vector-wrong-market")

    disposition = await run_vector(runtime, reader, "vector-wrong-market-worker")
    assert disposition is not None
    assert disposition.reason_code == "MARKET_IDENTITY_MISMATCH"
    assert await setup_evidence(runtime.cases, trade_case.id) == []


# --------------------------------------------------------- back-compat


def test_a_pre_phase_2h_setup_payload_still_parses():
    """Evidence written before this phase must stay readable and acceptable."""
    legacy = {
        "kind": "trade_setup",
        "setup_id": str(uuid4()),
        "side": "BUY",
        "entry_price": "1.10",
        "invalidation_price": "0.92",
        "target_prices": ["1.25"],
    }
    payload = TradeSetupPayload.model_validate(legacy)
    assert payload.setup is None
    assert payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert TradeSetupPayload.model_validate_json(payload.model_dump_json()) == payload


def test_the_other_specialists_are_untouched():
    from src.agents.atlas.prompt import ATLAS_PROMPT_VERSION
    from src.agents.orbit.prompt import ORBIT_PROMPT_VERSION
    from src.agents.signal.prompt import SIGNAL_PROMPT_VERSION

    assert (ORBIT_PROMPT_VERSION, ATLAS_PROMPT_VERSION, SIGNAL_PROMPT_VERSION) == (
        "orbit-v1",
        "atlas-v1",
        "signal-v1",
    )
