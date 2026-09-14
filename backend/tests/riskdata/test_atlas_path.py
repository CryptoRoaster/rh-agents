"""The real ATLAS worker, end to end, to the holder metrics it now stores.

No stubbed collector and no hand-built payload: the deterministic snapshot
builder runs, the versioned policy decides, the handler submits, and the
workflow service writes the row. What is asserted is what a later reader would
actually find in the database.

The providers behind the builder are stubs because the alternative is a live
holder indexer, which no test may depend on. Everything between them and the
stored evidence is the production path.
"""

from datetime import timedelta

import pytest

from src.agents.atlas.models import AtlasSourceFailure, HolderCompleteness
from src.agents.atlas.sources.normalize import concentration
from src.orchestration.worker.models import TaskAttemptOutcome
from src.orchestration.workflow.models import EvidenceStatus, EvidenceType
from tests.atlas.conftest import TOTAL_SUPPLY, holder_rows, holder_source_result
from tests.atlas.test_workflow import build_stack, open_atlas_case, run_atlas


async def stored_onchain(cases, trade_case_id):
    envelopes = [
        item
        for item in await cases.evidence(trade_case_id)
        if item.evidence_type is EvidenceType.ONCHAIN
    ]
    assert len(envelopes) == 1
    return envelopes[0]


async def run_to_evidence(sessions, now, trace, *, holders=None, key="atlas-holders"):
    runtime, reader = build_stack(sessions, now, holders=holders)
    trade_case = await open_atlas_case(runtime.cases, now, trace, key)
    disposition = await run_atlas(runtime, reader, f"{key}-worker")
    assert disposition is not None
    assert disposition.outcome == TaskAttemptOutcome.SUCCEEDED
    return runtime, await stored_onchain(runtime.cases, trade_case.id)


async def test_the_real_path_stores_the_measured_holder_distribution(worker_db, now, trace):
    """The numbers ATLAS derived reach the durable record unchanged."""
    _, sessions = worker_db
    _, envelope = await run_to_evidence(sessions, now, trace)

    holders = envelope.payload.intelligence.holders
    assert holders is not None
    expected = concentration(holder_rows(), TOTAL_SUPPLY, HolderCompleteness.TOP_N_ONLY)
    assert holders.top_ten_fraction == expected.top10_share
    assert holders.top_one_fraction == expected.top1_share
    assert holders.measurement == "TOP_TEN_OVER_TOTAL_SUPPLY"
    assert holders.total_supply_raw == str(TOTAL_SUPPLY)


async def test_the_stored_metrics_carry_their_own_provenance(worker_db, now, trace):
    """Source, source observation time and what that instant means."""
    _, sessions = worker_db
    _, envelope = await run_to_evidence(sessions, now, trace)

    holders = envelope.payload.intelligence.holders
    assert holders.source == "test-indexer"
    assert holders.observed_at == now
    assert holders.observation_basis == "SOURCE_BLOCK"
    assert holders.completeness == "TOP_N_ONLY"
    assert holders.snapshot_block == 1_000_000
    assert holders.holder_count == 4200
    assert holders.holder_count_basis == "PROVIDER_REPORTED"


async def test_a_verdict_and_a_metric_are_both_recorded(worker_db, now, trace):
    """Neither replaces the other. The verdict stays exactly where it was."""
    _, sessions = worker_db
    _, envelope = await run_to_evidence(sessions, now, trace)

    assert envelope.payload.holder_integrity == "PASS"
    assert envelope.payload.intelligence.holders.top_ten_fraction is not None


async def test_an_unavailable_holder_source_records_an_explicit_absence(worker_db, now, trace):
    """A run that looked and found nothing says so, rather than saying nothing.

    The key is present and `null`, which is different bytes — and a different
    fact — from a row written before the metrics existed.
    """
    import json

    _, sessions = worker_db
    unavailable = holder_source_result(
        now, status="UNAVAILABLE", failure=AtlasSourceFailure.UNAVAILABLE
    )
    _, envelope = await run_to_evidence(
        sessions, now, trace, holders=unavailable, key="atlas-no-holders"
    )

    assert envelope.status is EvidenceStatus.UNKNOWN
    assert envelope.payload.intelligence.holders is None
    emitted = json.loads(envelope.payload.intelligence.model_dump_json())
    assert "holders" in emitted and emitted["holders"] is None


async def test_stored_evidence_round_trips_through_the_database(worker_db, now, trace):
    """What was written is what comes back, byte for byte."""
    _, sessions = worker_db
    runtime, envelope = await run_to_evidence(sessions, now, trace)

    reread = await stored_onchain(runtime.cases, envelope.trade_case_id)
    assert reread.payload.model_dump_json() == envelope.payload.model_dump_json()
    assert reread.submission_fingerprint == envelope.submission_fingerprint


async def test_a_burn_adjusted_share_never_stands_in_for_the_raw_one(worker_db, now, trace):
    """Two measures against two denominators, both recorded, never conflated.

    The fixture holder set contains a burn address, so the adjusted figure
    exists and differs. A threshold judging the adjusted number would judge a
    smaller one, which is the direction a safety limit must never be wrong in.
    """
    _, sessions = worker_db
    complete = holder_source_result(now, completeness=HolderCompleteness.COMPLETE)
    _, envelope = await run_to_evidence(sessions, now, trace, holders=complete, key="atlas-burn")

    holders = envelope.payload.intelligence.holders
    assert holders.top_ten_fraction_excluding_burn is not None
    assert holders.top_ten_fraction != holders.top_ten_fraction_excluding_burn
    assert holders.burned_fraction is not None


@pytest.mark.parametrize(
    ("completeness", "expected"),
    [(HolderCompleteness.COMPLETE, "COMPLETE"), (HolderCompleteness.TOP_N_ONLY, "TOP_N_ONLY")],
)
async def test_the_coverage_proof_travels_with_the_metric(
    worker_db, now, trace, completeness, expected
):
    _, sessions = worker_db
    source = holder_source_result(now, completeness=completeness)
    _, envelope = await run_to_evidence(
        sessions, now, trace, holders=source, key=f"atlas-{expected.lower()}"
    )
    assert envelope.payload.intelligence.holders.completeness == expected


async def test_a_stale_holder_observation_still_records_when_it_was_true(worker_db, now, trace):
    """The anchor is the source's own instant, never the moment it was fetched."""
    _, sessions = worker_db
    earlier = now - timedelta(minutes=3)
    source = holder_source_result(now, snapshot_timestamp=earlier)
    _, envelope = await run_to_evidence(
        sessions, now, trace, holders=source, key="atlas-stale-holders"
    )
    assert envelope.payload.intelligence.holders.observed_at == earlier
