"""Pure deterministic reducer for TradeCase state and blockers."""

import json
from datetime import datetime
from hashlib import sha256

from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    Blocker,
    EvidenceEnvelope,
    EvidenceStatus,
    EvidenceType,
    LiquidityExecutionPayload,
    RiskBinding,
    TradeCase,
    TradeCaseStatus,
    TradeSetupPayload,
    TriggerPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1, WorkflowPolicy
from src.risk.authorization import RiskAuthorization, classify_risk_authorization

# evidence_type, producer_role, evidence_id, schema_version, submission_fingerprint
DigestEntry = tuple[str, str, str, int, str]


class Evaluation:
    def __init__(
        self,
        status: TradeCaseStatus,
        reason_code: str,
        blockers: tuple[Blocker, ...] = (),
        risk_input_digest: str | None = None,
    ) -> None:
        self.status = status
        self.reason_code = reason_code
        self.blockers = blockers
        self.risk_input_digest = risk_input_digest


def active_evidence(evidence: tuple[EvidenceEnvelope, ...]) -> dict[EvidenceType, EvidenceEnvelope]:
    """Select the one live envelope per evidence type, independent of input order.

    Each type holds a single supersession chain, so two simultaneously active
    envelopes of one type mean the stored history is inconsistent. That is never
    resolved by retrieval order; it fails closed as an integrity error.
    """
    superseded = {item.supersedes_id for item in evidence if item.supersedes_id is not None}
    current: dict[EvidenceType, EvidenceEnvelope] = {}
    for item in sorted(evidence, key=lambda entry: (entry.evidence_type.value, entry.evidence_id)):
        if item.evidence_id in superseded:
            continue
        if item.evidence_type in current:
            raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_INTEGRITY)
        current[item.evidence_type] = item
    return current


def risk_input_digest(
    trade_case: TradeCase,
    current: dict[EvidenceType, EvidenceEnvelope],
    policy: WorkflowPolicy = TRADE_CASE_V1,
) -> str:
    """Hash the active safety-critical risk inputs into a canonical fingerprint.

    Only policy safety-critical evidence participates. DISCOVERY and SENTIMENT
    gate the workflow through required-evidence blockers but are not part of the
    risk snapshot, so a change to either moves the case out of an authorized
    state through the evaluator rather than through this digest.

    Construction is canonical by value: entries are built as explicit tuples,
    totally ordered by a stable key, and serialized as sorted-key JSON with fixed
    separators. No database, dict, set or arrival ordering can reach the hash.
    """
    entries: list[DigestEntry] = sorted(
        (
            item.evidence_type.value,
            item.producer_role.value,
            str(item.evidence_id),
            item.schema_version,
            item.submission_fingerprint,
        )
        for evidence_type, item in current.items()
        if evidence_type in policy.safety_types
    )
    canonical = json.dumps(
        {
            "trade_case_id": str(trade_case.id),
            "workflow_version": trade_case.workflow_version,
            "evidence": [
                {
                    "evidence_type": entry[0],
                    "producer_role": entry[1],
                    "evidence_id": entry[2],
                    "schema_version": entry[3],
                    "submission_fingerprint": entry[4],
                }
                for entry in entries
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


class TradeCaseEvaluator:
    def __init__(self, policy: WorkflowPolicy = TRADE_CASE_V1) -> None:
        self.policy = policy

    def evaluate(
        self,
        trade_case: TradeCase,
        evidence: tuple[EvidenceEnvelope, ...],
        risk_binding: RiskBinding | None,
        now: datetime,
    ) -> Evaluation:
        if trade_case.status in TERMINAL_CASE_STATUSES:
            return Evaluation(
                trade_case.status,
                trade_case.reason_code,
                trade_case.blockers,
                trade_case.risk_input_digest,
            )
        if trade_case.expires_at is not None and now >= trade_case.expires_at:
            return Evaluation(TradeCaseStatus.EXPIRED, "TRADE_CASE_EXPIRED")

        current = active_evidence(evidence)
        pre_blockers: list[Blocker] = []
        pre_missing: list[Blocker] = []
        for requirement in self.policy.requirements:
            if not requirement.before_trigger or not requirement.required:
                continue
            item = current.get(requirement.evidence_type)
            if item is None:
                pre_missing.append(
                    Blocker(
                        code=f"{requirement.role.value}_{requirement.evidence_type.value}_MISSING",
                        role=requirement.role,
                        evidence_type=requirement.evidence_type,
                    )
                )
                continue
            effective = item.effective_status(now)
            if effective != EvidenceStatus.AVAILABLE:
                target = pre_blockers if requirement.safety_critical else pre_missing
                target.append(
                    Blocker(
                        code=f"{requirement.role.value}_{effective.value}",
                        role=requirement.role,
                        evidence_type=requirement.evidence_type,
                        evidence_id=item.evidence_id,
                    )
                )
        if pre_blockers:
            return Evaluation(
                TradeCaseStatus.BLOCKED, "SAFETY_EVIDENCE_BLOCKED", tuple(pre_blockers)
            )
        if pre_missing:
            return Evaluation(
                TradeCaseStatus.EVIDENCE_PENDING, "REQUIRED_EVIDENCE_PENDING", tuple(pre_missing)
            )

        setup = current[EvidenceType.TRADE_SETUP]
        assert isinstance(setup.payload, TradeSetupPayload)
        trigger = current.get(EvidenceType.TRIGGER)
        if trigger is None:
            return Evaluation(TradeCaseStatus.READY_FOR_TRIGGER, "WAITING_FOR_CURRENT_TRIGGER")
        trigger_status = trigger.effective_status(now)
        if trigger_status != EvidenceStatus.AVAILABLE:
            return Evaluation(
                TradeCaseStatus.BLOCKED,
                "TRIGGER_EVIDENCE_BLOCKED",
                (
                    Blocker(
                        code=f"PULSE_{trigger_status.value}",
                        role=self.policy.requirement(EvidenceType.TRIGGER).role,
                        evidence_type=EvidenceType.TRIGGER,
                        evidence_id=trigger.evidence_id,
                    ),
                ),
            )
        assert isinstance(trigger.payload, TriggerPayload)
        if trigger.payload.setup_evidence_id != setup.evidence_id:
            return Evaluation(
                TradeCaseStatus.READY_FOR_TRIGGER,
                "TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP",
                (
                    Blocker(
                        code="PULSE_TRIGGER_SETUP_MISMATCH",
                        role=self.policy.requirement(EvidenceType.TRIGGER).role,
                        evidence_type=EvidenceType.TRIGGER,
                        evidence_id=trigger.evidence_id,
                    ),
                ),
            )
        if trade_case.status == TradeCaseStatus.READY_FOR_TRIGGER:
            return Evaluation(TradeCaseStatus.TRIGGERED, "CURRENT_SETUP_TRIGGERED")

        anchor = current.get(EvidenceType.LIQUIDITY_EXECUTION)
        if anchor is None:
            return Evaluation(
                TradeCaseStatus.EXECUTION_EVIDENCE_PENDING, "EXECUTION_EVIDENCE_PENDING"
            )
        anchor_status = anchor.effective_status(now)
        if anchor_status != EvidenceStatus.AVAILABLE:
            return Evaluation(
                TradeCaseStatus.BLOCKED,
                "EXECUTION_EVIDENCE_BLOCKED",
                (
                    Blocker(
                        code=f"ANCHOR_{anchor_status.value}_EXECUTION_EVIDENCE",
                        role=self.policy.requirement(EvidenceType.LIQUIDITY_EXECUTION).role,
                        evidence_type=EvidenceType.LIQUIDITY_EXECUTION,
                        evidence_id=anchor.evidence_id,
                    ),
                ),
            )

        assert isinstance(anchor.payload, LiquidityExecutionPayload)
        if (
            anchor.payload.setup_evidence_id != setup.evidence_id
            or anchor.payload.trigger_evidence_id != trigger.evidence_id
        ):
            return Evaluation(
                TradeCaseStatus.EXECUTION_EVIDENCE_PENDING,
                "EXECUTION_EVIDENCE_NOT_CURRENT",
            )

        digest = risk_input_digest(trade_case, current, self.policy)
        if (
            risk_binding is None
            or risk_binding.risk_input_digest != digest
            or now >= risk_binding.expires_at
        ):
            return Evaluation(
                TradeCaseStatus.READY_FOR_RISK, "SENTINEL_EVALUATION_REQUIRED", (), digest
            )
        # One authoritative interpretation of the decision, re-derived here from
        # the binding's own typed values rather than trusted from a stored label.
        authorization = classify_risk_authorization(
            risk_binding.outcome,
            risk_binding.reason_codes,
            position_size_limit_usd=risk_binding.position_size_limit_usd,
            max_additional_notional_usd=risk_binding.max_additional_notional_usd,
        )
        match authorization:
            case RiskAuthorization.APPROVED:
                return Evaluation(TradeCaseStatus.RISK_APPROVED, "SENTINEL_APPROVED", (), digest)
            case RiskAuthorization.LIMITED:
                return Evaluation(TradeCaseStatus.RISK_LIMITED, "SENTINEL_SIZE_LIMITED", (), digest)
            case _:
                return Evaluation(TradeCaseStatus.RISK_REJECTED, "SENTINEL_REJECTED", (), digest)
