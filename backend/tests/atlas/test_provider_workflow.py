"""The provider path driven through the real worker runtime and TradeCase workflow.

These are the Phase 2E behavioural proofs that matter downstream: verified holder
facts now satisfy the ATLAS prerequisite end to end, and a later holder snapshot
revalidates an existing risk authorization exactly as any other safety evidence
does.
"""

from datetime import timedelta
from uuid import uuid4

from src.agents.atlas.context import AtlasContextReader, AtlasSnapshotBuilder
from src.agents.atlas.handler import AtlasWorkerHandler
from src.agents.atlas.models import AtlasSourceFailure, AtlasVerdict
from src.agents.atlas.sources.blockscout import BlockscoutHolderSource
from src.agents.atlas.sources.routing import RoutedHolderSource, RoutedOriginSource
from src.core.clock import FixedClock
from src.orchestration.worker.capabilities import AtlasCapabilities
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    TradeCaseStatus,
)
from src.orchestration.workflow.service import TradeCaseService
from tests.atlas.conftest import StubContracts, chain_snapshot, contract_facts
from tests.atlas.fake_http import RecordingRoutes, json_response
from tests.atlas.test_blockscout import page
from tests.atlas.test_data_enablement import (
    BLOCKSCOUT,
    CHAIN_BLOCK,
    blockscout_routes,
)
from tests.atlas.test_workflow import (
    _complete_remaining_evidence,
    _Fixed,
    _lease_for,
    _NoSubmit,
    _risk_decision,
    atlas_evidence,
    open_atlas_case,
    run_atlas,
)


def outage_routes() -> RecordingRoutes:
    """Every provider path answers with an upstream failure."""
    return RecordingRoutes({"": lambda request: json_response({"error": "down"}, status=503)})


def provider_stack(sessions, instant, *, recording=None, contract=None):
    """The real collector over the real Blockscout adapter, on a fixture transport."""
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    recording = recording if recording is not None else blockscout_routes(now=instant)
    builder = AtlasSnapshotBuilder(
        contracts=StubContracts(
            chain_snapshot(
                instant,
                block=CHAIN_BLOCK,
                block_timestamp=instant - timedelta(seconds=60),
                fetched_at=instant,
            ),
            contract if contract is not None else contract_facts(block=CHAIN_BLOCK),
        ),
        holders=RoutedHolderSource(
            sources={
                "robinhood": BlockscoutHolderSource(
                    config=BLOCKSCOUT,
                    chain="robinhood",
                    transport_factory=recording.transport_factory(),
                )
            }
        ),
        origins=RoutedOriginSource(sources={}),
        clock=clock,
    )
    return runtime, AtlasContextReader(cases=cases, builder=builder)


async def test_verified_holder_facts_satisfy_the_atlas_prerequisite(worker_db, now, trace):
    """The complete Phase 2E proof: real provider data carries a case past ATLAS."""
    _, sessions = worker_db
    runtime, reader = provider_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "e2e-clear")
    disposition = await run_atlas(runtime, reader, "e2e-clear-worker")
    assert disposition is not None

    envelope = (await atlas_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.AVAILABLE
    assert envelope.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    assert envelope.payload.holder_integrity == "PASS"
    assert envelope.payload.contract_integrity == "PASS"
    assert envelope.payload.intelligence.verdict == AtlasVerdict.CLEAR.value
    assert envelope.payload.intelligence.policy_version == "atlas-policy-v2"

    await _complete_remaining_evidence(runtime.cases, trade_case, now, trace)
    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK


async def test_a_provider_outage_leaves_the_case_blocked_not_optimistic(worker_db, now, trace):
    _, sessions = worker_db
    runtime, reader = provider_stack(sessions, now, recording=outage_routes())
    trade_case = await open_atlas_case(runtime.cases, now, trace, "e2e-outage")
    await run_atlas(runtime, reader, "e2e-outage-worker")

    envelope = (await atlas_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.UNKNOWN
    assert envelope.payload.acceptance() == EvidenceAcceptance.INSUFFICIENT
    assert envelope.payload.holder_integrity == "UNKNOWN"

    await _complete_remaining_evidence(runtime.cases, trade_case, now, trace)
    updated = await runtime.cases.get_trade_case(trade_case.id)
    # On-chain evidence is safety-critical, so an unusable holder fact blocks the
    # case rather than leaving it merely waiting.
    assert updated.status == TradeCaseStatus.BLOCKED


async def test_a_changed_holder_snapshot_revalidates_an_approved_authorization(
    worker_db, now, trace
):
    """Holder facts are safety evidence, so a new snapshot revokes an approval."""
    _, sessions = worker_db
    runtime, reader = provider_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "e2e-supersede")
    await run_atlas(runtime, reader, "e2e-supersede-worker")
    await _complete_remaining_evidence(runtime.cases, trade_case, now, trace)

    ready = await runtime.cases.get_trade_case(trade_case.id)
    assert ready.status == TradeCaseStatus.READY_FOR_RISK
    before_digest = ready.risk_input_digest
    approved = await runtime.cases.record_risk_decision(
        trade_case.id, _risk_decision(ready, now), risk_input_digest=before_digest
    )
    assert approved.status == TradeCaseStatus.RISK_APPROVED

    # The indexer has moved on and now reports the holder source as unreachable.
    later = now + timedelta(minutes=1)
    later_runtime, later_reader = provider_stack(sessions, later, recording=outage_routes())
    refreshed = await later_reader.onchain_context(trade_case.id, uuid4())
    assert refreshed.supersedes_evidence_id is not None
    assert refreshed.snapshot.holders.failure == AtlasSourceFailure.UNAVAILABLE

    handler = AtlasWorkerHandler(clock=FixedClock(later))
    lease = _lease_for(refreshed, later, trace)
    report = await handler.handle(
        lease, AtlasCapabilities(lease=lease, context=_Fixed(refreshed), submit=_NoSubmit())
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)

    revoked = await later_runtime.cases.get_trade_case(trade_case.id)
    assert revoked.status == TradeCaseStatus.BLOCKED
    assert revoked.risk_input_digest != before_digest


async def test_a_moved_holder_distribution_changes_the_risk_input_digest(worker_db, now, trace):
    """A different distribution is a different fact, and risk must see that."""
    _, sessions = worker_db
    runtime, reader = provider_stack(sessions, now)
    trade_case = await open_atlas_case(runtime.cases, now, trace, "e2e-moved")
    await run_atlas(runtime, reader, "e2e-moved-worker")
    await _complete_remaining_evidence(runtime.cases, trade_case, now, trace)
    before = (await runtime.cases.get_trade_case(trade_case.id)).risk_input_digest

    later = now + timedelta(minutes=1)
    moved = blockscout_routes(now=later, holders=page(12, top=7 * 10**22))
    later_runtime, later_reader = provider_stack(sessions, later, recording=moved)
    refreshed = await later_reader.onchain_context(trade_case.id, uuid4())
    handler = AtlasWorkerHandler(clock=FixedClock(later))
    lease = _lease_for(refreshed, later, trace)
    report = await handler.handle(
        lease, AtlasCapabilities(lease=lease, context=_Fixed(refreshed), submit=_NoSubmit())
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)

    after = await later_runtime.cases.get_trade_case(trade_case.id)
    assert after.risk_input_digest != before
    envelopes = await atlas_evidence(later_runtime.cases, trade_case.id)
    current = next(item for item in envelopes if item.supersedes_id is not None)
    assert current.payload.acceptance() == EvidenceAcceptance.ACCEPTED
