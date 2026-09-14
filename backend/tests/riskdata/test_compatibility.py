"""Holder metrics were added to a payload whose rows are already in the database.

Evidence is append-only and its submission fingerprints are stored, so a new
field is a change to bytes that already exist. An absent key and a key holding
`null` are different bytes, and emitting one for a row that never had it breaks
replay while parsing perfectly — the exact failure an earlier round of this
system spent three commits closing.

The fixtures below were produced by **running the model as it stood at
`779261d`**, the merge this branch is based on, not by constructing them with
the current one. Building both sides with the current model would put it on both
sides of the comparison and prove nothing.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from src.core.models import AgentRole
from src.orchestration.workflow.models import (
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    HolderDistributionFacts,
    OnchainIntelligence,
    OnchainPayload,
)
from tests.riskdata.conftest import TOTAL_SUPPLY, holder_block

FIXTURES = Path(__file__).parent / "fixtures"
GENERATION = "779261d"
ENTRIES = ("with_intelligence", "without_intelligence")


def historical(entry: str) -> tuple[str, str]:
    payload = json.loads((FIXTURES / f"onchain-{GENERATION}.json").read_text())[entry]
    return payload["submission_raw"], payload["fingerprint"]


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_historical_payload_reserialises_to_exactly_its_stored_bytes(entry):
    raw, _ = historical(entry)
    assert EvidenceSubmission.model_validate_json(raw).model_dump_json() == raw


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_historical_payload_keeps_its_original_fingerprint(entry):
    raw, original = historical(entry)
    assert EvidenceSubmission.model_validate_json(raw).fingerprint() == original


def test_the_new_key_is_absent_from_historical_bytes_rather_than_null():
    """The distinction the whole mechanism exists for."""
    raw, _ = historical("with_intelligence")
    intelligence = json.loads(raw)["payload"]["intelligence"]
    assert intelligence is not None
    assert "holders" not in intelligence


async def test_an_unchanged_historical_submission_replays_through_the_real_service(
    worker_db, now, trace
):
    """Replay through the workflow itself, not merely through the model.

    The stored identity must survive all the way into the row, because that is
    what a replay compares against.
    """
    from src.core.clock import FixedClock
    from src.orchestration.workflow.service import TradeCaseService
    from tests.riskdata.conftest import open_case

    _, sessions = worker_db
    raw, original = historical("with_intelligence")
    stored = EvidenceSubmission.model_validate_json(raw)

    cases = TradeCaseService(sessions, clock=FixedClock(stored.observed_at))
    trade_case = await open_case(cases, stored.observed_at, stored.correlation_id, "compat-case")
    first = await cases.record_evidence(trade_case.id, stored)
    replay = await cases.record_evidence(trade_case.id, stored)

    assert replay.evidence_id == first.evidence_id
    assert first.submission_fingerprint == original


def test_a_new_payload_records_its_holder_block(now):
    """New rows carry the metrics, at the end of the object."""
    intelligence = OnchainIntelligence(
        verdict="CLEAR",
        policy_version="atlas-policy-v2",
        domain_status={"HOLDERS": "AVAILABLE"},
        chain_id=4663,
        block_number=1,
        snapshot_digest="a" * 64,
        holders=holder_block(now),
    )
    emitted = json.loads(intelligence.model_dump_json())
    assert list(emitted)[-1] == "holders"
    assert emitted["holders"]["measurement"] == "TOP_TEN_OVER_TOTAL_SUPPLY"
    assert emitted["holders"]["total_supply_raw"] == TOTAL_SUPPLY
    assert (
        intelligence.model_dump_json()
        == OnchainIntelligence.model_validate_json(intelligence.model_dump_json()).model_dump_json()
    )


def test_an_explicitly_recorded_absence_is_not_the_same_as_a_missing_key(now):
    """A run that looked and found nothing is not a row written before the field.

    Both mean "no holder metrics here", and they are different bytes with
    different fingerprints, so they must stay distinguishable.
    """
    common = dict(
        verdict="CLEAR",
        policy_version="atlas-policy-v2",
        domain_status={"HOLDERS": "UNAVAILABLE"},
        chain_id=4663,
        block_number=1,
        snapshot_digest="a" * 64,
    )
    recorded = OnchainIntelligence(**common, holders=None)
    historical_shape = OnchainIntelligence(**common)

    assert json.loads(recorded.model_dump_json())["holders"] is None
    assert "holders" not in json.loads(historical_shape.model_dump_json())
    assert recorded.model_dump_json() != historical_shape.model_dump_json()


def test_unknown_fields_are_still_refused():
    """Compatibility is about reproducing what was written, never accepting more."""
    raw, _ = historical("with_intelligence")
    payload = json.loads(raw)
    payload["payload"]["intelligence"]["invented_field"] = 1
    with pytest.raises(ValidationError, match="[Ee]xtra"):
        EvidenceSubmission.model_validate(payload)


def test_a_holder_block_cannot_carry_a_supply_that_is_not_a_number():
    """The denominator travels as exact digits, never as a float or a label."""
    for bad in ("1e18", "-5", "", "0x10", "1" * 79):
        with pytest.raises(ValidationError):
            HolderDistributionFacts(
                source="test-indexer",
                observed_at=datetime(2026, 9, 14, tzinfo=UTC),
                observation_basis="SOURCE_BLOCK",
                completeness="COMPLETE",
                total_supply_raw=bad,
            )


def test_a_recorded_submission_with_holder_metrics_round_trips(now, trace):
    """The whole envelope, not just the nested block."""
    submission = EvidenceSubmission(
        idempotency_key="onchain-new",
        producer_role=AgentRole.ATLAS,
        evidence_type=EvidenceType.ONCHAIN,
        provenance=EvidenceProvenance(
            source="atlas:evm-rpc", reference_id=uuid5(NAMESPACE_URL, "case")
        ),
        observed_at=now,
        valid_until=now + timedelta(minutes=10),
        status=EvidenceStatus.AVAILABLE,
        payload=OnchainPayload(
            holder_integrity="PASS",
            dev_wallet_integrity="PASS",
            contract_integrity="PASS",
            intelligence=OnchainIntelligence(
                verdict="CLEAR",
                policy_version="atlas-policy-v2",
                domain_status={"HOLDERS": "AVAILABLE"},
                chain_id=4663,
                block_number=1,
                snapshot_digest="a" * 64,
                holders=holder_block(now),
            ),
        ),
        correlation_id=trace,
    )
    raw = submission.model_dump_json()
    parsed = EvidenceSubmission.model_validate_json(raw)
    assert parsed.model_dump_json() == raw
    assert parsed.fingerprint() == submission.fingerprint()
