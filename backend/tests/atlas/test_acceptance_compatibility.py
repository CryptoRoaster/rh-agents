"""The new acceptance axis must not have changed any existing evidence type.

EvidenceAcceptance was added so on-chain evidence could block on its content.
Every other payload keeps its previous behaviour, where availability was the
whole question, and these tests pin that down.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.orchestration.workflow.engine import unusable_reason
from src.orchestration.workflow.models import (
    DiscoveryPayload,
    EvidenceAcceptance,
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    LiquidityExecutionPayload,
    OnchainPayload,
    SentimentPayload,
    TradeSetupPayload,
    TriggerPayload,
)

UNCHANGED_PAYLOADS = {
    "discovery": DiscoveryPayload(discovery_reference=uuid4()),
    "sentiment": SentimentPayload(assessment="NEUTRAL"),
    "trade_setup": TradeSetupPayload(
        setup_id=uuid4(),
        side="BUY",
        entry_price=Decimal("1"),
        invalidation_price=Decimal("0.8"),
        target_prices=(Decimal("1.2"),),
    ),
    "trigger": TriggerPayload(
        setup_evidence_id=uuid4(), observed_price=Decimal("1"), trigger_code="ENTRY"
    ),
    "liquidity_execution": LiquidityExecutionPayload(
        setup_evidence_id=uuid4(),
        trigger_evidence_id=uuid4(),
        quoted_price=Decimal("1"),
        liquidity_usd=Decimal("1"),
        estimated_slippage_bps=Decimal("1"),
        price_impact_bps=Decimal("1"),
        maximum_safe_size_usd=Decimal("1"),
        routing_provenance="route",
    ),
}


def envelope(now, payload, evidence_type, status=EvidenceStatus.AVAILABLE):
    identifier = uuid4()
    return EvidenceEnvelope(
        evidence_id=identifier,
        trade_case_id=uuid4(),
        producer_role=AgentRole.ORBIT,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=now,
        created_at=now,
        recorded_at=now,
        valid_until=now + __import__("datetime").timedelta(minutes=5),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_UNKNOWN",),
        payload=payload,
        correlation_id=uuid4(),
        idempotency_key=str(identifier),
        submission_fingerprint="b" * 64,
    )


@pytest.mark.parametrize("name", sorted(UNCHANGED_PAYLOADS))
def test_every_pre_existing_payload_still_accepts_unconditionally(name):
    """None of these carries a content-level policy, so none of them can block."""
    assert UNCHANGED_PAYLOADS[name].acceptance() == EvidenceAcceptance.ACCEPTED


@pytest.mark.parametrize("name", sorted(UNCHANGED_PAYLOADS))
def test_available_evidence_of_other_types_is_never_silently_blocked(now, name):
    item = envelope(now, UNCHANGED_PAYLOADS[name], EvidenceType.DISCOVERY)
    # Exactly the old behaviour: available means usable for these types.
    assert unusable_reason(item, now) is None


@pytest.mark.parametrize("status", [EvidenceStatus.UNKNOWN, EvidenceStatus.UNAVAILABLE])
@pytest.mark.parametrize("name", sorted(UNCHANGED_PAYLOADS))
def test_unavailable_evidence_still_reports_its_status(now, name, status):
    item = envelope(now, UNCHANGED_PAYLOADS[name], EvidenceType.DISCOVERY, status)
    assert unusable_reason(item, now) == status.value


def test_staleness_is_reported_before_acceptance_is_consulted(now):
    """Freshness remains the first question, exactly as before."""
    item = envelope(now, UNCHANGED_PAYLOADS["discovery"], EvidenceType.DISCOVERY)
    later = now + __import__("datetime").timedelta(minutes=10)
    assert unusable_reason(item, later) == EvidenceStatus.STALE.value


# ------------------------------------------------------- on-chain is the exception


def test_only_onchain_evidence_gained_a_content_level_verdict(now):
    passing = OnchainPayload(
        holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="PASS"
    )
    assert passing.acceptance() == EvidenceAcceptance.ACCEPTED
    assert unusable_reason(envelope(now, passing, EvidenceType.ONCHAIN), now) is None

    blocked = OnchainPayload(
        holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="FAIL"
    )
    assert blocked.acceptance() == EvidenceAcceptance.BLOCKED
    # Available, and still refused — the whole point of the second axis.
    item = envelope(now, blocked, EvidenceType.ONCHAIN)
    assert item.status == EvidenceStatus.AVAILABLE
    assert unusable_reason(item, now) == EvidenceAcceptance.BLOCKED.value

    insufficient = OnchainPayload(
        holder_integrity="UNKNOWN", dev_wallet_integrity="PASS", contract_integrity="PASS"
    )
    assert insufficient.acceptance() == EvidenceAcceptance.INSUFFICIENT


def test_a_stale_onchain_envelope_reports_staleness_not_its_verdict(now):
    """Status is asked first: an expired envelope is stale whatever it contains."""
    blocked = OnchainPayload(
        holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="FAIL"
    )
    item = envelope(now, blocked, EvidenceType.ONCHAIN)
    later = now + __import__("datetime").timedelta(minutes=10)
    assert unusable_reason(item, later) == EvidenceStatus.STALE.value
