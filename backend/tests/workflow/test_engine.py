import random
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import AgentRole
from src.markets.fake import fixture_snapshot
from src.orchestration.workflow.engine import (
    TradeCaseEvaluator,
    active_evidence,
    risk_input_digest,
)
from src.orchestration.workflow.models import (
    DiscoveryPayload,
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    LiquidityExecutionPayload,
    OnchainPayload,
    SentimentPayload,
    TradeCase,
    TradeCaseStatus,
    TradeSetupPayload,
    TriggerPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1


def case(now, trace):
    market = fixture_snapshot(now, trace).pair.market_identity
    return TradeCase(
        id=uuid4(),
        market=market,
        chain=market.chain,
        network=market.network,
        status=TradeCaseStatus.DISCOVERED,
        opened_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        originating_discovery_reference=uuid4(),
        revision=1,
        reason_code="CASE_OPENED",
        correlation_id=trace,
        open_idempotency_key="open-1",
        open_fingerprint="a" * 64,
    )


def envelope(trade_case, now, role, evidence_type, payload, status=EvidenceStatus.AVAILABLE):
    evidence_id = uuid4()
    return EvidenceEnvelope(
        evidence_id=evidence_id,
        trade_case_id=trade_case.id,
        producer_role=role,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=now,
        created_at=now,
        recorded_at=now,
        valid_until=now + timedelta(minutes=5),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_UNKNOWN",),
        payload=payload,
        correlation_id=trade_case.correlation_id,
        idempotency_key=str(evidence_id),
        submission_fingerprint="b" * 64,
    )


def pretrigger_evidence(trade_case, now):
    return (
        envelope(
            trade_case,
            now,
            AgentRole.ORBIT,
            EvidenceType.DISCOVERY,
            DiscoveryPayload(discovery_reference=trade_case.originating_discovery_reference),
        ),
        envelope(
            trade_case,
            now,
            AgentRole.ATLAS,
            EvidenceType.ONCHAIN,
            OnchainPayload(
                holder_integrity="PASS", dev_wallet_integrity="PASS", contract_integrity="PASS"
            ),
        ),
        envelope(
            trade_case,
            now,
            AgentRole.SIGNAL,
            EvidenceType.SENTIMENT,
            SentimentPayload(assessment="NEUTRAL"),
        ),
        envelope(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            TradeSetupPayload(
                setup_id=uuid4(),
                side="BUY",
                entry_price=Decimal("1"),
                invalidation_price=Decimal("0.8"),
                target_prices=(Decimal("1.2"),),
            ),
        ),
    )


def test_missing_evidence_is_pending_and_unknown_atlas_blocks(now, trace):
    trade_case = case(now, trace)
    evaluator = TradeCaseEvaluator()
    pending = evaluator.evaluate(trade_case, (), None, now)
    assert pending.status == TradeCaseStatus.EVIDENCE_PENDING
    assert {blocker.role for blocker in pending.blockers} == {
        AgentRole.ORBIT,
        AgentRole.ATLAS,
        AgentRole.SIGNAL,
        AgentRole.VECTOR,
    }

    evidence = list(pretrigger_evidence(trade_case, now))
    atlas = evidence[1]
    evidence[1] = atlas.model_copy(
        update={"status": EvidenceStatus.UNKNOWN, "reason_codes": ("SOURCE_UNKNOWN",)}
    )
    blocked = evaluator.evaluate(trade_case, tuple(evidence), None, now)
    assert blocked.status == TradeCaseStatus.BLOCKED
    assert blocked.blockers[0].code == "ATLAS_UNKNOWN"


def test_fresh_pretrigger_evidence_waits_for_trigger(now, trace):
    trade_case = case(now, trace)
    result = TradeCaseEvaluator().evaluate(
        trade_case, pretrigger_evidence(trade_case, now), None, now
    )
    assert result.status == TradeCaseStatus.READY_FOR_TRIGGER
    assert result.blockers == ()


def test_valid_until_boundary_is_stale(now, trace):
    trade_case = case(now, trace)
    evidence = list(pretrigger_evidence(trade_case, now))
    atlas = evidence[1]
    evidence[1] = atlas.model_copy(update={"valid_until": now})
    result = TradeCaseEvaluator().evaluate(trade_case, tuple(evidence), None, now)
    assert result.status == TradeCaseStatus.BLOCKED
    assert result.blockers[0].code == "ATLAS_STALE"


def test_future_observation_is_invalid(now, trace):
    trade_case = case(now, trace)
    evidence = list(pretrigger_evidence(trade_case, now))
    atlas = evidence[1]
    evidence[1] = atlas.model_copy(update={"observed_at": now + timedelta(seconds=1)})
    result = TradeCaseEvaluator().evaluate(trade_case, tuple(evidence), None, now)
    assert result.status == TradeCaseStatus.BLOCKED
    assert result.blockers[0].code == "ATLAS_INVALID"


def full_evidence(trade_case, now):
    """Every active envelope a READY_FOR_RISK case holds, including both
    safety-critical and deliberately excluded types."""
    pre = list(pretrigger_evidence(trade_case, now))
    setup_evidence = pre[3]
    trigger_evidence = envelope(
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        TriggerPayload(
            setup_evidence_id=setup_evidence.evidence_id,
            observed_price=Decimal("1"),
            trigger_code="ENTRY_LEVEL_REACHED",
        ),
    )
    anchor_evidence = envelope(
        trade_case,
        now,
        AgentRole.ANCHOR,
        EvidenceType.LIQUIDITY_EXECUTION,
        LiquidityExecutionPayload(
            setup_evidence_id=setup_evidence.evidence_id,
            trigger_evidence_id=trigger_evidence.evidence_id,
            quoted_price=Decimal("1"),
            liquidity_usd=Decimal("500000"),
            estimated_slippage_bps=Decimal("25"),
            price_impact_bps=Decimal("20"),
            maximum_safe_size_usd=Decimal("2500"),
            routing_provenance="quoted-route-v1",
        ),
    )
    return tuple(pre) + (trigger_evidence, anchor_evidence)


def test_digest_ignores_insertion_and_retrieval_order(now, trace):
    trade_case = case(now, trace)
    evidence = full_evidence(trade_case, now)
    baseline = risk_input_digest(trade_case, active_evidence(evidence))
    for seed in range(8):
        shuffled = list(evidence)
        random.Random(seed).shuffle(shuffled)
        assert risk_input_digest(trade_case, active_evidence(tuple(shuffled))) == baseline
    assert risk_input_digest(trade_case, active_evidence(tuple(reversed(evidence)))) == baseline


def test_digest_covers_every_safety_critical_input(now, trace):
    trade_case = case(now, trace)
    evidence = full_evidence(trade_case, now)
    baseline = risk_input_digest(trade_case, active_evidence(evidence))
    changed = set()
    for index, item in enumerate(evidence):
        replacement = list(evidence)
        replacement[index] = item.model_copy(update={"submission_fingerprint": "c" * 64})
        if risk_input_digest(trade_case, active_evidence(tuple(replacement))) != baseline:
            changed.add(item.evidence_type)
    assert changed == TRADE_CASE_V1.safety_types


def test_digest_excludes_non_safety_critical_evidence(now, trace):
    # DISCOVERY and SENTIMENT gate the workflow through required-evidence
    # blockers, not through the risk snapshot. Changing them must not silently
    # invalidate a SENTINEL binding.
    trade_case = case(now, trace)
    evidence = list(full_evidence(trade_case, now))
    baseline = risk_input_digest(trade_case, active_evidence(tuple(evidence)))
    for index, item in enumerate(evidence):
        if item.evidence_type in TRADE_CASE_V1.safety_types:
            continue
        replacement = list(evidence)
        replacement[index] = item.model_copy(update={"submission_fingerprint": "d" * 64})
        assert risk_input_digest(trade_case, active_evidence(tuple(replacement))) == baseline


def test_digest_binds_the_trade_case_identity(now, trace):
    trade_case = case(now, trace)
    evidence = full_evidence(trade_case, now)
    other = case(now, trace)
    assert risk_input_digest(trade_case, active_evidence(evidence)) != risk_input_digest(
        other, active_evidence(evidence)
    )


def test_two_active_envelopes_of_one_type_fail_closed(now, trace):
    trade_case = case(now, trace)
    evidence = pretrigger_evidence(trade_case, now)
    duplicate = evidence[1].model_copy(update={"evidence_id": uuid4()})
    with pytest.raises(WorkflowFailure) as caught:
        active_evidence(evidence + (duplicate,))
    assert caught.value.code == WorkflowErrorCode.EVIDENCE_INTEGRITY


def test_superseded_chain_leaves_exactly_one_active_envelope(now, trace):
    trade_case = case(now, trace)
    evidence = list(pretrigger_evidence(trade_case, now))
    first = evidence[1]
    second = first.model_copy(update={"evidence_id": uuid4(), "supersedes_id": first.evidence_id})
    third = second.model_copy(update={"evidence_id": uuid4(), "supersedes_id": second.evidence_id})
    current = active_evidence(tuple(evidence) + (second, third))
    assert current[EvidenceType.ONCHAIN].evidence_id == third.evidence_id
