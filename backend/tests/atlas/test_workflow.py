"""ATLAS inside the real workflow: does a known-bad fact actually stop a case?

Before Phase 2D the evaluator asked only whether evidence was available. These
tests pin down the extension that lets available evidence block on its content,
so a measured danger never has to be disguised as a missing measurement.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.atlas.context import AtlasContextReader, AtlasTaskInput
from src.agents.atlas.handler import ATLAS_TASK_TYPE, AtlasWorkerHandler
from src.agents.atlas.models import AtlasSourceFailure, AtlasVerdict
from src.agents.atlas.unavailable import UnconfiguredHolderSource, UnconfiguredOriginSource
from src.core.clock import FixedClock
from src.core.models import AgentRole, RiskDecision, RiskMetrics, RiskOutcome
from src.markets.models import Availability
from src.orchestration.worker.capabilities import AtlasCapabilities
from src.orchestration.worker.models import (
    TaskAttemptOutcome,
    WorkerErrorCode,
    WorkerFailure,
    WorkerRegistration,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1
from src.orchestration.worker.runner import CapabilityProvider, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    TradeCaseStatus,
)
from src.orchestration.workflow.service import TradeCaseService
from tests.atlas.conftest import (
    StubContracts,
    StubHolders,
    StubOrigins,
    chain_snapshot,
    contract_facts,
    holder_facts,
    market_identity,
    origin_facts,
)


def build_stack(sessions, instant, *, contract=None, holders=None, origin=None):
    from src.agents.atlas.context import AtlasSnapshotBuilder

    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    builder = AtlasSnapshotBuilder(
        contracts=StubContracts(chain_snapshot(instant), contract or contract_facts()),
        holders=StubHolders(holders if holders is not None else holder_facts(instant)),
        origins=StubOrigins(origin if origin is not None else origin_facts()),
        clock=clock,
    )
    return runtime, AtlasContextReader(cases=cases, builder=builder)


async def open_atlas_case(cases, now, trace, key):
    return await cases.open_trade_case(
        market_identity(),
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
    )


async def run_atlas(runtime, reader, key, provider=None, clock_at=None):
    handler = AtlasWorkerHandler(
        provider=provider, clock=FixedClock(clock_at or runtime.clock.now())
    )
    runner = WorkerRunner(
        runtime,
        handler,
        CapabilityProvider(service=runtime, onchain=reader),
        registration_key=key,
    )
    await runner.register()
    return await runner.run_once()


async def atlas_evidence(cases, trade_case_id):
    return [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type == EvidenceType.ONCHAIN
    ]


# ------------------------------------------------------- available and blocked


async def test_a_measured_violation_blocks_the_case_while_evidence_stays_available(
    worker_db, now, trace
):
    """The Phase 2D headline. Complete, fresh, available facts that are dangerous."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, contract=contract_facts(code_present=False))
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-blocked")

    disposition = await run_atlas(runtime, reader, "atlas-blocked-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED

    evidence = await atlas_evidence(runtime.cases, trade_case.id)
    assert len(evidence) == 1
    envelope = evidence[0]
    # The fact was obtainable, so the envelope is available. Nothing pretends the
    # danger was merely unknown.
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.contract_integrity == "FAIL"
    assert envelope.payload.acceptance() == EvidenceAcceptance.BLOCKED
    assert envelope.payload.intelligence is not None
    assert envelope.payload.intelligence.verdict == AtlasVerdict.BLOCKED.value
    assert envelope.reason_codes

    # And the case is stopped despite the evidence being available.
    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.BLOCKED
    assert any(
        blocker.role == AgentRole.ATLAS and blocker.code.endswith("BLOCKED")
        for blocker in updated.blockers
    )


async def test_unobtainable_required_facts_block_as_insufficient(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(
        sessions,
        now,
        holders=holder_facts(
            now, status=Availability.UNAVAILABLE, failure=AtlasSourceFailure.NOT_CONFIGURED
        ),
    )
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-unknown")
    disposition = await run_atlas(runtime, reader, "atlas-unknown-worker")
    assert disposition is not None

    envelope = (await atlas_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.UNKNOWN
    assert envelope.payload.holder_integrity == "UNKNOWN"
    assert envelope.payload.acceptance() == EvidenceAcceptance.INSUFFICIENT
    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.BLOCKED


async def test_an_unconfigured_holder_source_fails_closed(worker_db, now, trace):
    """No provider is connected for either chain, and ATLAS says so plainly.

    Reaching CLEAR here would require inventing holder data. Blocking is the
    correct outcome, and this test exists to keep it that way.
    """
    _, sessions = worker_db
    from src.agents.atlas.context import AtlasSnapshotBuilder

    clock = FixedClock(now)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = AtlasContextReader(
        cases=cases,
        builder=AtlasSnapshotBuilder(
            contracts=StubContracts(chain_snapshot(now), contract_facts()),
            holders=UnconfiguredHolderSource(),
            origins=UnconfiguredOriginSource(),
            clock=clock,
        ),
    )
    trade_case = await open_atlas_case(cases, now, trace, "atlas-noprovider")
    await run_atlas(runtime, reader, "atlas-noprovider-worker")

    envelope = (await atlas_evidence(cases, trade_case.id))[0]
    assert envelope.payload.intelligence is not None
    assert envelope.payload.intelligence.verdict == AtlasVerdict.INSUFFICIENT_DATA.value
    assert "HOLDER_SOURCE_NOT_CONFIGURED" in envelope.payload.intelligence.data_gaps
    assert (await cases.get_trade_case(trade_case.id)).status == TradeCaseStatus.BLOCKED


async def test_clear_facts_let_the_case_continue(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-clear")
    await run_atlas(runtime, reader, "atlas-clear-worker")

    envelope = (await atlas_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    updated = await runtime.cases.get_trade_case(trade_case.id)
    # ATLAS no longer blocks; the case waits on the other specialists.
    assert updated.status == TradeCaseStatus.EVIDENCE_PENDING
    assert not any(blocker.role == AgentRole.ATLAS for blocker in updated.blockers)
    tasks = {item.role: item for item in await runtime.cases.tasks(trade_case.id)}
    assert tasks[AgentRole.ATLAS].task_type == ATLAS_TASK_TYPE


# ------------------------------------------------- supersession and risk binding


async def test_new_atlas_evidence_invalidates_a_prior_risk_authorization(worker_db, now, trace):
    """Safety-critical evidence changing must revoke an existing authorization."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-supersede")
    await run_atlas(runtime, reader, "atlas-supersede-worker")

    cases = runtime.cases
    await _complete_remaining_evidence(cases, trade_case, now, trace)
    ready = await cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    before_digest = ready.risk_input_digest

    approved = await cases.record_risk_decision(
        trade_case.id, _risk_decision(ready, now), risk_input_digest=before_digest
    )
    assert approved.status == TradeCaseStatus.RISK_APPROVED

    # A fresh ATLAS pass now finds the contract has no code. The ATLAS task is
    # already terminal after its first success, so a periodic re-assessment needs
    # a trigger Phase 2D does not introduce; the supersession semantics are
    # exercised directly through the ordinary evidence service instead.
    later = now + timedelta(minutes=1)
    later_runtime, later_reader = build_stack(
        sessions, later, contract=contract_facts(code_present=False)
    )
    refreshed = await later_reader.onchain_context(trade_case.id, uuid4())
    assert refreshed.supersedes_evidence_id is not None
    handler = AtlasWorkerHandler(clock=FixedClock(later))
    lease = _lease_for(refreshed, later, trace)
    report = await handler.handle(
        lease,
        AtlasCapabilities(lease=lease, context=_Fixed(refreshed), submit=_NoSubmit()),
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)

    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status == TradeCaseStatus.BLOCKED
    assert revoked.risk_input_digest != before_digest
    envelopes = await atlas_evidence(later_runtime.cases, trade_case.id)
    assert len(envelopes) == 2
    current = next(item for item in envelopes if item.supersedes_id is not None)
    assert current.payload.acceptance() == EvidenceAcceptance.BLOCKED


def _risk_decision(trade_case, now):
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
        expires_at=now + timedelta(minutes=5),
    )


async def _complete_remaining_evidence(cases, trade_case, now, trace):
    """Drive the case to READY_FOR_RISK around ATLAS, using the ordinary services."""
    from tests.worker.conftest import (
        anchor_payload,
        setup_payload,
        signal_payload,
        submission,
        trigger_payload,
    )

    await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case, now, AgentRole.SIGNAL, EvidenceType.SENTIMENT, signal_payload(), key="a-sig"
        ),
    )
    setup = await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            setup_payload(),
            key="a-setup",
        ),
    )
    trigger = await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup.evidence_id),
            key="a-trig",
        ),
    )
    await cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            anchor_payload(setup.evidence_id, trigger.evidence_id),
            key="a-anch",
        ),
    )


# ---------------------------------------------------------- runtime interaction


async def test_a_stale_lease_cannot_submit_atlas_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-stale")
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="atlas-stale-a", role=AgentRole.ATLAS, runtime_version="atlas-v1"
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None

    # Collection and analysis outlive the lease; another runtime reclaims.
    expiry = now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1)
    expiry_runtime, _ = build_stack(sessions, expiry)
    await expiry_runtime.recover_expired_leases()
    later = now + WORKER_RUNTIME_V1.lease_duration + WORKER_RUNTIME_V1.retry_delay(1)
    later_runtime, later_reader = build_stack(sessions, later + timedelta(seconds=2))
    accepted = await run_atlas(later_runtime, later_reader, "atlas-stale-b")
    assert accepted is not None
    assert accepted.outcome == TaskAttemptOutcome.SUCCEEDED

    handler = AtlasWorkerHandler(clock=FixedClock(now))
    capabilities = CapabilityProvider(service=later_runtime, onchain=later_reader).build(lease)
    report = await handler.handle(lease, capabilities)
    with pytest.raises(WorkerFailure) as caught:
        await later_runtime.submit_task_result(lease, report)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    assert len(await atlas_evidence(later_runtime.cases, trade_case.id)) == 1


async def test_a_replayed_atlas_result_creates_no_duplicate(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-replay")
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="atlas-replay-w", role=AgentRole.ATLAS, runtime_version="atlas-v1"
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    handler = AtlasWorkerHandler(clock=FixedClock(now))
    report = await handler.handle(
        lease, CapabilityProvider(service=runtime, onchain=reader).build(lease)
    )

    first = await runtime.submit_task_result(lease, report)
    assert first.replayed is False
    replay = await runtime.submit_task_result(lease, report)
    assert replay.replayed is True
    assert len(await atlas_evidence(runtime.cases, trade_case.id)) == 1
    assert len(await runtime.attempts(task_id=lease.task_id)) == 1


async def test_atlas_may_only_submit_onchain_evidence(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    await open_atlas_case(runtime.cases, now, trace, "atlas-role")
    worker = await runtime.register_worker(
        WorkerRegistration(
            registration_key="atlas-role-w", role=AgentRole.ATLAS, runtime_version="atlas-v1"
        )
    )
    lease = await runtime.claim_next_task(worker.worker_instance_id)
    assert lease is not None
    assert lease.role == AgentRole.ATLAS

    from src.orchestration.worker.models import EvidenceTaskResult
    from tests.worker.conftest import signal_payload, submission

    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    wrong = EvidenceTaskResult(
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
        await runtime.submit_task_result(lease, wrong)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


async def test_a_non_evm_token_address_fails_deterministically(worker_db, now, trace):
    """Fixture markets use placeholder asset ids, which ATLAS must refuse."""
    _, sessions = worker_db
    from src.markets.fake import fixture_snapshot

    runtime, reader = build_stack(sessions, now)
    await runtime.cases.open_trade_case(
        fixture_snapshot(now, trace).pair.market_identity,
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key="atlas-nonevm",
        expires_at=now + timedelta(hours=1),
    )
    disposition = await run_atlas(runtime, reader, "atlas-nonevm-worker")
    assert disposition is not None
    assert disposition.reason_code == "TOKEN_ADDRESS_NOT_EVM"


class _Fixed:
    def __init__(self, task_input: AtlasTaskInput) -> None:
        self._task_input = task_input

    async def onchain_context(self, trade_case_id, task_id):
        return self._task_input


class _NoSubmit:
    def __init__(self) -> None:
        self.lease = None

    async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
        raise AssertionError("the handler must not submit directly")


def _lease_for(task_input: AtlasTaskInput, now, trace):
    from src.orchestration.worker.models import TaskLease

    return TaskLease(
        lease_id=uuid4(),
        task_id=task_input.snapshot.task_id,
        trade_case_id=task_input.snapshot.trade_case_id,
        role=AgentRole.ATLAS,
        task_type=ATLAS_TASK_TYPE,
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace,
    )


async def test_atlas_supersession_also_invalidates_a_limited_authorization(worker_db, now, trace):
    """RISK_LIMITED is an authorization too, and safety evidence changing revokes it."""
    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "atlas-limited")
    await run_atlas(runtime, reader, "atlas-limited-worker")

    cases = runtime.cases
    await _complete_remaining_evidence(cases, trade_case, now, trace)
    ready = await cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK

    # A resizable sizing-only rejection with bounded capacity: LIMITED, not APPROVED.
    limited_decision = _risk_decision(ready, now).model_copy(
        update={
            "outcome": RiskOutcome.REJECT,
            "reason_codes": ("MAX_POSITION_SIZE",),
            "max_additional_notional_usd": Decimal("137.125"),
        }
    )
    limited = await cases.record_risk_decision(
        trade_case.id, limited_decision, risk_input_digest=ready.risk_input_digest
    )
    assert limited.status == TradeCaseStatus.RISK_LIMITED
    before_digest = limited.risk_input_digest

    later = now + timedelta(minutes=1)
    later_runtime, later_reader = build_stack(
        sessions, later, contract=contract_facts(code_present=False)
    )
    refreshed = await later_reader.onchain_context(trade_case.id, uuid4())
    handler = AtlasWorkerHandler(clock=FixedClock(later))
    lease = _lease_for(refreshed, later, trace)
    report = await handler.handle(
        lease, AtlasCapabilities(lease=lease, context=_Fixed(refreshed), submit=_NoSubmit())
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)

    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status == TradeCaseStatus.BLOCKED
    assert revoked.risk_input_digest != before_digest
