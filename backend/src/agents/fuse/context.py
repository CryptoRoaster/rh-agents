"""Assembly of the FUSE view: what is current, what is usable, what is missing.

Admissibility is decided here, on the server, before a worker sees anything.
That ordering is the point of the file. A synthesizer that could choose which
evidence to read could choose the evidence that suited its conclusion; one that
receives a finished set cannot, and the difference costs nothing because the
choice was never a judgement call — the workflow already defines exactly one
current envelope per type.

Three rules shape it.

**Only current evidence.** `active_evidence` walks the supersession chains and
yields one live envelope per type. A setup that VECTOR has replaced is durable
history and is never handed over as though it were the case's current geometry.

**Unusable is reported, not hidden.** Evidence that is missing, stale, unknown or
insufficient becomes a typed gap rather than an exception. Recording *why* a case
cannot proceed is more useful than recording nothing, and it is the only way the
reason becomes visible to anything that reads evidence rather than logs.

**Bounded views.** Each source is reduced to the facts a synthesis reads. Full
payloads carry advisory prose, cited observation ids, provider metadata and
recorded market structure; none of it is read here, and forwarding it would copy
it into durable storage a second time for no one's benefit.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.agents.fuse.models import (
    DiscoveryView,
    FuseSourceEvidence,
    FuseTaskInput,
    GapOrigin,
    MissingSource,
    OnchainView,
    SentimentView,
    SourceReference,
    TradeSetupView,
)
from src.agents.fuse.policy import FUSE_SYNTHESIS_V1, ROLE_FOR_TYPE, FuseSynthesisPolicy
from src.agents.fuse.ports import FuseContextUnavailable
from src.core.clock import Clock, SystemClock
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    DiscoveryPayload,
    EvidenceEnvelope,
    EvidenceStatus,
    OnchainPayload,
    SentimentPayload,
    TradeCase,
    TradeSetupPayload,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1, WorkflowPolicy


class TradeCaseSynthesisSource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


STATUS_ORIGINS: dict[EvidenceStatus, GapOrigin] = {
    EvidenceStatus.UNKNOWN: GapOrigin.NOT_AVAILABLE,
    EvidenceStatus.UNAVAILABLE: GapOrigin.NOT_AVAILABLE,
    EvidenceStatus.INVALID: GapOrigin.NOT_AVAILABLE,
    EvidenceStatus.STALE: GapOrigin.STALE,
}


def discovery_view(payload: DiscoveryPayload) -> DiscoveryView:
    detail = payload.assessment
    if detail is None:
        return DiscoveryView()
    return DiscoveryView(
        classification=detail.classification,
        strength=detail.strength,
        reason_codes=detail.reason_codes[:12],
        data_gaps=detail.data_gaps[:12],
    )


def onchain_view(payload: OnchainPayload) -> OnchainView:
    detail = payload.intelligence
    return OnchainView(
        holder_integrity=payload.holder_integrity,
        dev_wallet_integrity=payload.dev_wallet_integrity,
        contract_integrity=payload.contract_integrity,
        verdict=None if detail is None else detail.verdict,
        blockers=() if detail is None else detail.blockers[:12],
        data_gaps=() if detail is None else detail.data_gaps[:12],
    )


def sentiment_view(payload: SentimentPayload) -> SentimentView:
    detail = payload.intelligence
    return SentimentView(
        assessment=payload.assessment,
        data_quality=None if detail is None else detail.data_quality,
        attention_level=None if detail is None else detail.attention_level,
        organic_breadth=None if detail is None else detail.organic_breadth,
        manipulation_concern=None if detail is None else detail.manipulation_concern,
        gaps=() if detail is None else detail.gaps[:12],
    )


def trade_setup_view(payload: TradeSetupPayload) -> TradeSetupView:
    detail = payload.setup
    trigger = None if detail is None else detail.trigger
    return TradeSetupView(
        setup_id=payload.setup_id,
        side=payload.side.value,
        setup_fingerprint=None if detail is None else detail.setup_fingerprint,
        trigger_type=None if trigger is None else trigger.type,
        valid_from=None if trigger is None else trigger.valid_from,
        expires_at=None if trigger is None else trigger.expires_at,
        history_bar_count=None if detail is None else detail.history_bar_count,
        history_timeframe=None if detail is None else detail.history_timeframe,
        reason_codes=() if detail is None else detail.reason_codes[:12],
    )


def source_view(payload: object) -> dict[str, object]:
    """The one bounded view for this payload's own type, keyed for the contract."""
    if isinstance(payload, DiscoveryPayload):
        return {"discovery": discovery_view(payload)}
    if isinstance(payload, OnchainPayload):
        return {"onchain": onchain_view(payload)}
    if isinstance(payload, SentimentPayload):
        return {"sentiment": sentiment_view(payload)}
    if isinstance(payload, TradeSetupPayload):
        return {"trade_setup": trade_setup_view(payload)}
    raise FuseContextUnavailable("UNSUPPORTED_EVIDENCE_PAYLOAD")


@dataclass(frozen=True)
class FuseContextReader:
    """Assembles the synthesis view from existing workflow services only."""

    cases: TradeCaseSynthesisSource
    policy: FuseSynthesisPolicy = FUSE_SYNTHESIS_V1
    workflow: WorkflowPolicy = TRADE_CASE_V1
    clock: Clock = SystemClock()

    async def synthesis_context(self, trade_case_id: UUID, task_id: UUID) -> FuseTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        now = self.clock.now()
        if trade_case.status in TERMINAL_CASE_STATUSES:
            # Nothing a synthesis says could change a finished case, and a
            # summary written after the fact would read as though it had.
            raise FuseContextUnavailable("TRADE_CASE_TERMINAL")

        evidence = await self.cases.evidence(trade_case_id)
        current = active_evidence(evidence)
        sources: list[FuseSourceEvidence] = []
        missing: list[MissingSource] = []

        for evidence_type in self.policy.sources:
            requirement = self.workflow.requirement(evidence_type)
            role = ROLE_FOR_TYPE[evidence_type]
            item = current.get(evidence_type)
            if item is None:
                missing.append(
                    MissingSource(
                        role=role,
                        evidence_type=evidence_type,
                        origin=GapOrigin.MISSING,
                        safety_critical=requirement.safety_critical,
                    )
                )
                continue
            if item.producer_role != role:
                # A role that filed another role's evidence type is an integrity
                # problem, not a synthesis input.
                raise FuseContextUnavailable("EVIDENCE_ROLE_MISMATCH")
            effective = item.effective_status(now)
            if effective != EvidenceStatus.AVAILABLE:
                missing.append(
                    MissingSource(
                        role=role,
                        evidence_type=evidence_type,
                        origin=STATUS_ORIGINS.get(effective, GapOrigin.NOT_AVAILABLE),
                        safety_critical=requirement.safety_critical,
                        evidence_id=item.evidence_id,
                        detail=effective.value,
                    )
                )
                continue
            sources.append(
                FuseSourceEvidence(
                    reference=SourceReference(
                        role=role,
                        evidence_type=evidence_type,
                        evidence_id=item.evidence_id,
                        submission_fingerprint=item.submission_fingerprint,
                        status=effective,
                        acceptance=item.payload.acceptance(),
                        observed_at=item.observed_at,
                        valid_until=item.valid_until,
                        required=requirement.required,
                        safety_critical=requirement.safety_critical,
                    ),
                    **source_view(item.payload),  # type: ignore[arg-type]
                )
            )

        # No guard for "nothing at all" here, deliberately. The policy refuses to
        # exist with an empty source list, and every type it names lands in one
        # of the two lists above, so both being empty is unreachable. A branch
        # that cannot fire is not a safeguard but an untested claim that one
        # exists — and the real case, every source missing, is answered by the
        # synthesis refusing rather than by the context pretending it cannot look.
        return FuseTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            workflow_version=trade_case.workflow_version,
            policy_version=self.policy.version,
            sources=tuple(sources),
            missing=tuple(missing),
            evaluated_at=now,
        )
