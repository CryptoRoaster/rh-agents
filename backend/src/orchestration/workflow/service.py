"""PostgreSQL-authoritative TradeCase command and read interface.

Every mutating command locks one TradeCase row. Future workers submit typed
evidence here; they never receive sessions, status setters, risk overrides, or
execution capabilities.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import AgentRole, RiskDecision, RiskOutcome
from src.data.repository import aware
from src.data.tables import (
    TradeCaseEventRow,
    TradeCaseEvidenceRow,
    TradeCaseRiskBindingRow,
    TradeCaseRow,
    TradeCaseTaskRow,
    TradeCaseTransitionRow,
)
from src.markets.models import MarketIdentity
from src.orchestration.workflow.engine import Evaluation, TradeCaseEvaluator, active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    TERMINAL_TASK_STATUSES,
    DiscoveryPayload,
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceSubmission,
    EvidenceType,
    LiquidityExecutionPayload,
    RiskBinding,
    SourceRefreshOrder,
    SourceRefreshOutcome,
    SpecialistTask,
    SpecialistTaskStatus,
    TimelineEvent,
    TradeCase,
    TradeCaseStatus,
    TriggerPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import (
    TRADE_CASE_V1,
    EvidenceRequirement,
    WorkflowPolicy,
)
from src.risk.authorization import RiskAuthorization, classify_decision

TASK_TRANSITIONS: dict[SpecialistTaskStatus, frozenset[SpecialistTaskStatus]] = {
    SpecialistTaskStatus.PENDING: frozenset(
        {
            SpecialistTaskStatus.RUNNING,
            SpecialistTaskStatus.SUCCEEDED,
            SpecialistTaskStatus.BLOCKED,
            SpecialistTaskStatus.FAILED,
            SpecialistTaskStatus.CANCELLED,
            SpecialistTaskStatus.EXPIRED,
        }
    ),
    SpecialistTaskStatus.RUNNING: frozenset(
        {
            SpecialistTaskStatus.SUCCEEDED,
            SpecialistTaskStatus.BLOCKED,
            SpecialistTaskStatus.FAILED,
            SpecialistTaskStatus.CANCELLED,
            SpecialistTaskStatus.EXPIRED,
        }
    ),
    SpecialistTaskStatus.BLOCKED: frozenset(
        {
            SpecialistTaskStatus.PENDING,
            SpecialistTaskStatus.RUNNING,
            SpecialistTaskStatus.SUCCEEDED,
            SpecialistTaskStatus.CANCELLED,
            SpecialistTaskStatus.EXPIRED,
        }
    ),
}

COMMON_BACKTRACKS = frozenset(
    {
        TradeCaseStatus.EVIDENCE_PENDING,
        TradeCaseStatus.BLOCKED,
        TradeCaseStatus.READY_FOR_TRIGGER,
        TradeCaseStatus.EXECUTION_EVIDENCE_PENDING,
        TradeCaseStatus.READY_FOR_RISK,
        TradeCaseStatus.EXPIRED,
        TradeCaseStatus.CANCELLED,
    }
)
CASE_TRANSITIONS: dict[TradeCaseStatus, frozenset[TradeCaseStatus]] = {
    TradeCaseStatus.DISCOVERED: frozenset(
        {
            TradeCaseStatus.EVIDENCE_PENDING,
            TradeCaseStatus.BLOCKED,
            TradeCaseStatus.READY_FOR_TRIGGER,
            TradeCaseStatus.EXPIRED,
            TradeCaseStatus.CANCELLED,
        }
    ),
    TradeCaseStatus.EVIDENCE_PENDING: COMMON_BACKTRACKS,
    TradeCaseStatus.BLOCKED: COMMON_BACKTRACKS | {TradeCaseStatus.TRIGGERED},
    TradeCaseStatus.READY_FOR_TRIGGER: COMMON_BACKTRACKS | {TradeCaseStatus.TRIGGERED},
    TradeCaseStatus.TRIGGERED: COMMON_BACKTRACKS,
    TradeCaseStatus.EXECUTION_EVIDENCE_PENDING: COMMON_BACKTRACKS,
    TradeCaseStatus.READY_FOR_RISK: COMMON_BACKTRACKS
    | {
        TradeCaseStatus.RISK_REJECTED,
        TradeCaseStatus.RISK_LIMITED,
        TradeCaseStatus.RISK_APPROVED,
    },
    # Both authorized states are revalidatable. Neither may reach another risk
    # verdict directly: revocation always returns through the evaluator, and a
    # fresh SENTINEL decision is bound only from READY_FOR_RISK.
    # An approval may be revoked by the evaluator, or spent by an execution.
    # `EXECUTED` is reachable from nowhere else: only a booked fill ends a case
    # that way, and only from the one status that authorised it.
    TradeCaseStatus.RISK_APPROVED: COMMON_BACKTRACKS | {TradeCaseStatus.EXECUTED},
    TradeCaseStatus.RISK_LIMITED: COMMON_BACKTRACKS,
}


@dataclass(frozen=True)
class WorkflowInputs:
    """Everything the central evaluator reads, loaded once and held.

    Exists so loading and judging can happen at two clearly separated moments:
    the reads under the caller's locks, then one clock read, then a synchronous
    verdict on exactly what was loaded.
    """

    trade_case: TradeCase
    evidence: tuple[EvidenceEnvelope, ...]
    risk_binding: RiskBinding | None


def canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def case_from_row(row: TradeCaseRow) -> TradeCase:
    return TradeCase.model_validate(
        {
            "id": row.id,
            "workflow_version": row.workflow_version,
            "market": MarketIdentity.model_validate(row.market_payload),
            "chain": row.chain,
            "network": row.network,
            "status": TradeCaseStatus(row.status),
            "opened_at": aware(row.opened_at),
            "updated_at": aware(row.updated_at),
            "expires_at": aware(row.expires_at) if row.expires_at is not None else None,
            "originating_discovery_reference": row.originating_discovery_reference,
            "strategy_policy_id": row.strategy_policy_id,
            "revision": row.revision,
            "reason_code": row.reason_code,
            "blockers": row.blockers,
            "risk_input_digest": row.risk_input_digest,
            "correlation_id": row.correlation_id,
            "open_idempotency_key": row.open_idempotency_key,
            "open_fingerprint": row.open_fingerprint,
        }
    )


def evidence_from_row(row: TradeCaseEvidenceRow) -> EvidenceEnvelope:
    return EvidenceEnvelope.model_validate(row.payload)


def task_from_row(row: TradeCaseTaskRow) -> SpecialistTask:
    return SpecialistTask(
        task_id=row.task_id,
        trade_case_id=row.trade_case_id,
        role=AgentRole(row.role),
        task_type=row.task_type,
        required=row.required,
        status=SpecialistTaskStatus(row.status),
        created_at=aware(row.created_at),
        started_at=aware(row.started_at) if row.started_at is not None else None,
        completed_at=aware(row.completed_at) if row.completed_at is not None else None,
        expires_at=aware(row.expires_at) if row.expires_at is not None else None,
        attempt=row.attempt,
        correlation_id=row.correlation_id,
        reason_code=row.reason_code,
        idempotency_key=row.idempotency_key,
    )


def risk_from_row(row: TradeCaseRiskBindingRow) -> RiskBinding:
    # Enforcement values come from their own typed columns; the JSON payload is
    # audit provenance and is never parsed for a limit.
    return RiskBinding(
        binding_id=row.binding_id,
        trade_case_id=row.trade_case_id,
        risk_decision_id=row.risk_decision_id,
        risk_input_digest=row.risk_input_digest,
        outcome=RiskOutcome(row.outcome),
        authorization=RiskAuthorization(row.authorization),
        reason_codes=tuple(row.reason_codes),
        position_size_limit_usd=row.position_size_limit_usd,
        max_additional_notional_usd=row.max_additional_notional_usd,
        max_slippage_bps=row.max_slippage_bps,
        evaluated_at=aware(row.evaluated_at),
        expires_at=aware(row.expires_at),
        correlation_id=row.correlation_id,
        decision_payload=row.payload,
    )


class TradeCaseService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
        policy: WorkflowPolicy = TRADE_CASE_V1,
    ) -> None:
        self.sessions = sessions
        self.clock = clock if clock is not None else SystemClock()
        self.policy = policy
        self.evaluator = TradeCaseEvaluator(policy)

    async def open_trade_case_in_session(
        self,
        session: AsyncSession,
        market: MarketIdentity,
        *,
        originating_discovery_reference: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        expires_at: datetime,
        strategy_policy_id: str | None = None,
    ) -> tuple[TradeCase, bool]:
        """Open a case inside a caller-owned transaction, saying whether it was new.

        Mirrors ``record_evidence_in_session``: a caller that must make opening a
        case atomic with something else — a lock it already holds, a condition it
        has already checked — joins this transaction rather than reimplementing
        any workflow rule.

        The boolean is the part a separate call cannot supply. Opening is
        idempotent by key, so a caller that only receives the case cannot tell a
        creation from a replay, and asking afterwards is a second unsynchronised
        read of exactly the state the transaction exists to pin down.
        """
        market = MarketIdentity.model_validate_json(market.model_dump_json())
        now = self.clock.now()
        if expires_at.utcoffset() is None or expires_at <= now:
            raise ValueError("TradeCase must be opened before its expiry")
        fingerprint = canonical_digest(
            {
                "market": market.model_dump(mode="json"),
                "originating_discovery_reference": str(originating_discovery_reference),
                "correlation_id": str(correlation_id),
                "expires_at": expires_at.isoformat(),
                "strategy_policy_id": strategy_policy_id,
                "workflow_version": self.policy.version,
            }
        )
        existing = await session.scalar(
            select(TradeCaseRow).where(TradeCaseRow.open_idempotency_key == idempotency_key)
        )
        if existing is not None:
            return self._verify_open_replay(existing, fingerprint), False
        row = self._new_case_row(
            market,
            now=now,
            expires_at=expires_at,
            originating_discovery_reference=originating_discovery_reference,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            strategy_policy_id=strategy_policy_id,
        )
        session.add(row)
        await session.flush()
        self._event(
            session, row, "CASE_OPENED", "CASE_OPENED", {"workflow_version": self.policy.version}
        )
        await self._create_tasks(session, row)
        await self._record_discovery(session, row)
        await self._stabilize(session, row)
        return case_from_row(row), True

    def _new_case_row(
        self,
        market: MarketIdentity,
        *,
        now: datetime,
        expires_at: datetime,
        originating_discovery_reference: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
        strategy_policy_id: str | None,
    ) -> TradeCaseRow:
        return TradeCaseRow(
            id=uuid5(NAMESPACE_URL, f"rh-agents:trade-case:{idempotency_key}"),
            workflow_version=self.policy.version,
            market_key=market.pair_id,
            chain=market.chain,
            network=market.network,
            status=TradeCaseStatus.DISCOVERED.value,
            opened_at=now,
            updated_at=now,
            expires_at=expires_at,
            originating_discovery_reference=originating_discovery_reference,
            strategy_policy_id=strategy_policy_id,
            revision=1,
            reason_code="CASE_OPENED",
            blockers=[],
            risk_input_digest=None,
            correlation_id=correlation_id,
            open_idempotency_key=idempotency_key,
            open_fingerprint=fingerprint,
            market_payload=market.model_dump(mode="json"),
        )

    async def open_trade_case(
        self,
        market: MarketIdentity,
        *,
        originating_discovery_reference: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        expires_at: datetime,
        strategy_policy_id: str | None = None,
    ) -> TradeCase:
        market = MarketIdentity.model_validate_json(market.model_dump_json())
        now = self.clock.now()
        if expires_at.utcoffset() is None or expires_at <= now:
            raise ValueError("TradeCase must be opened before its expiry")
        request = {
            "market": market.model_dump(mode="json"),
            "originating_discovery_reference": str(originating_discovery_reference),
            "correlation_id": str(correlation_id),
            "expires_at": expires_at.isoformat(),
            "strategy_policy_id": strategy_policy_id,
            "workflow_version": self.policy.version,
        }
        fingerprint = canonical_digest(request)
        try:
            async with self.sessions.begin() as session:
                existing = await session.scalar(
                    select(TradeCaseRow).where(TradeCaseRow.open_idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return self._verify_open_replay(existing, fingerprint)
                case_id = uuid5(NAMESPACE_URL, f"rh-agents:trade-case:{idempotency_key}")
                row = TradeCaseRow(
                    id=case_id,
                    workflow_version=self.policy.version,
                    market_key=market.pair_id,
                    chain=market.chain,
                    network=market.network,
                    status=TradeCaseStatus.DISCOVERED.value,
                    opened_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    originating_discovery_reference=originating_discovery_reference,
                    strategy_policy_id=strategy_policy_id,
                    revision=1,
                    reason_code="CASE_OPENED",
                    blockers=[],
                    risk_input_digest=None,
                    correlation_id=correlation_id,
                    open_idempotency_key=idempotency_key,
                    open_fingerprint=fingerprint,
                    market_payload=market.model_dump(mode="json"),
                )
                session.add(row)
                await session.flush()
                self._event(
                    session,
                    row,
                    "CASE_OPENED",
                    "CASE_OPENED",
                    {"workflow_version": self.policy.version},
                )
                await self._create_tasks(session, row)
                await self._record_discovery(session, row)
                await self._stabilize(session, row)
                return case_from_row(row)
        except IntegrityError:
            async with self.sessions() as session:
                existing = await session.scalar(
                    select(TradeCaseRow).where(TradeCaseRow.open_idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                return self._verify_open_replay(existing, fingerprint)

    def _verify_open_replay(self, row: TradeCaseRow, fingerprint: str) -> TradeCase:
        if row.open_fingerprint != fingerprint:
            raise WorkflowFailure(WorkflowErrorCode.IDEMPOTENCY_CONFLICT)
        return case_from_row(row)

    async def _locked_case(self, session: AsyncSession, trade_case_id: UUID) -> TradeCaseRow:
        row = await session.scalar(
            select(TradeCaseRow).where(TradeCaseRow.id == trade_case_id).with_for_update()
        )
        if row is None:
            raise WorkflowFailure(WorkflowErrorCode.NOT_FOUND)
        return row

    async def _create_tasks(self, session: AsyncSession, row: TradeCaseRow) -> None:
        now = self.clock.now()
        for definition in self.policy.tasks:
            task_id = uuid5(
                NAMESPACE_URL,
                f"rh-agents:trade-case-task:{row.id}:{definition.role.value}:{definition.task_type}:1",
            )
            completed = definition.completed_on_open
            session.add(
                TradeCaseTaskRow(
                    task_id=task_id,
                    trade_case_id=row.id,
                    role=definition.role.value,
                    task_type=definition.task_type,
                    required=definition.required,
                    status=(
                        SpecialistTaskStatus.SUCCEEDED.value
                        if completed
                        else SpecialistTaskStatus.PENDING.value
                    ),
                    created_at=now,
                    started_at=now if completed else None,
                    completed_at=now if completed else None,
                    expires_at=row.expires_at,
                    attempt=1,
                    correlation_id=row.correlation_id,
                    reason_code="COMPLETED_ON_OPEN" if completed else "TASK_CREATED",
                    idempotency_key=f"{row.id}:{definition.role.value}:{definition.task_type}:1",
                    # The outer claim bound covers waits and failures together.
                    # The precise budgets are enforced when an outcome is
                    # reported; this only stops a slot being claimed without end.
                    **(
                        {}
                        if definition.claim_ceiling is None
                        else {"max_attempts": definition.claim_ceiling}
                    ),
                )
            )
            self._event(
                session,
                row,
                "TASK_CREATED",
                "TASK_CREATED",
                {
                    "task_id": str(task_id),
                    "role": definition.role.value,
                    "task_type": definition.task_type,
                    "required": definition.required,
                },
            )

    async def create_required_tasks(self, trade_case_id: UUID) -> tuple[SpecialistTask, ...]:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            existing = (
                await session.scalars(
                    select(TradeCaseTaskRow)
                    .where(TradeCaseTaskRow.trade_case_id == trade_case_id)
                    .order_by(TradeCaseTaskRow.created_at, TradeCaseTaskRow.task_id)
                )
            ).all()
            if not existing:
                await self._create_tasks(session, row)
                await session.flush()
                existing = (
                    await session.scalars(
                        select(TradeCaseTaskRow)
                        .where(TradeCaseTaskRow.trade_case_id == trade_case_id)
                        .order_by(TradeCaseTaskRow.created_at, TradeCaseTaskRow.task_id)
                    )
                ).all()
            return tuple(task_from_row(item) for item in existing)

    async def _record_discovery(self, session: AsyncSession, row: TradeCaseRow) -> None:
        if row.expires_at is None:
            raise WorkflowFailure(WorkflowErrorCode.ILLEGAL_TRANSITION)
        key = f"{row.id}:ORBIT:DISCOVERY:1"
        submission = EvidenceSubmission(
            idempotency_key=key,
            producer_role=AgentRole.ORBIT,
            evidence_type=EvidenceType.DISCOVERY,
            provenance=EvidenceProvenance(
                source="ORBIT", reference_id=row.originating_discovery_reference
            ),
            observed_at=aware(row.opened_at),
            valid_until=aware(row.expires_at),
            status=EvidenceStatus.AVAILABLE,
            payload=DiscoveryPayload(discovery_reference=row.originating_discovery_reference),
            correlation_id=row.correlation_id,
        )
        await self._insert_evidence(session, row, submission)

    async def record_evidence(
        self,
        trade_case_id: UUID,
        submission: EvidenceSubmission,
        *,
        expected_revision: int | None = None,
    ) -> EvidenceEnvelope:
        async with self.sessions.begin() as session:
            return await self.record_evidence_in_session(
                session, trade_case_id, submission, expected_revision=expected_revision
            )

    async def record_evidence_in_session(
        self,
        session: AsyncSession,
        trade_case_id: UUID,
        submission: EvidenceSubmission,
        *,
        expected_revision: int | None = None,
    ) -> EvidenceEnvelope:
        """Record evidence inside a caller-owned transaction.

        The worker runtime needs evidence recording and task completion to commit
        atomically, so it joins this transaction instead of reimplementing any
        workflow rule. Re-locking an already locked case in the same transaction
        is a no-op.
        """
        submission = EvidenceSubmission.model_validate_json(submission.model_dump_json())
        row = await self._locked_case(session, trade_case_id)
        existing = await session.scalar(
            select(TradeCaseEvidenceRow).where(
                TradeCaseEvidenceRow.idempotency_key == submission.idempotency_key
            )
        )
        if existing is not None:
            envelope = evidence_from_row(existing)
            if (
                existing.trade_case_id != trade_case_id
                or existing.submission_fingerprint != submission.fingerprint()
            ):
                raise WorkflowFailure(WorkflowErrorCode.IDEMPOTENCY_CONFLICT)
            return envelope
        self._mutable(row, expected_revision)
        envelope = await self._insert_evidence(session, row, submission)
        await self._complete_evidence_task(session, row, submission.evidence_type)
        await self._rearm_derived_tasks(session, row, submission.evidence_type)
        await self._stabilize(session, row)
        return envelope

    async def _insert_evidence(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        submission: EvidenceSubmission,
    ) -> EvidenceEnvelope:
        requirement = self.policy.requirement(submission.evidence_type)
        if (
            submission.producer_role != requirement.role
            or submission.correlation_id != row.correlation_id
        ):
            raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_BINDING)
        evidence_rows = (
            await session.scalars(
                select(TradeCaseEvidenceRow)
                .where(TradeCaseEvidenceRow.trade_case_id == row.id)
                .order_by(TradeCaseEvidenceRow.recorded_at, TradeCaseEvidenceRow.evidence_id)
            )
        ).all()
        evidence = tuple(evidence_from_row(item) for item in evidence_rows)
        current = active_evidence(evidence).get(submission.evidence_type)
        if current is None and submission.supersedes_id is not None:
            raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_SUPERSESSION)
        if current is not None and submission.supersedes_id != current.evidence_id:
            raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_SUPERSESSION)
        if isinstance(submission.payload, DiscoveryPayload):
            if submission.payload.discovery_reference != row.originating_discovery_reference:
                raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_BINDING)
        if isinstance(submission.payload, TriggerPayload):
            referenced = next(
                (
                    item
                    for item in evidence
                    if item.evidence_id == submission.payload.setup_evidence_id
                    and item.evidence_type == EvidenceType.TRADE_SETUP
                ),
                None,
            )
            if referenced is None:
                raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_BINDING)
        if isinstance(submission.payload, LiquidityExecutionPayload):
            setup_reference = next(
                (
                    item
                    for item in evidence
                    if item.evidence_id == submission.payload.setup_evidence_id
                    and item.evidence_type == EvidenceType.TRADE_SETUP
                ),
                None,
            )
            trigger_reference = next(
                (
                    item
                    for item in evidence
                    if item.evidence_id == submission.payload.trigger_evidence_id
                    and item.evidence_type == EvidenceType.TRIGGER
                ),
                None,
            )
            if setup_reference is None or trigger_reference is None:
                raise WorkflowFailure(WorkflowErrorCode.EVIDENCE_BINDING)
        now = self.clock.now()
        evidence_id = uuid5(
            NAMESPACE_URL, f"rh-agents:trade-case-evidence:{submission.idempotency_key}"
        )
        envelope = EvidenceEnvelope(
            evidence_id=evidence_id,
            trade_case_id=row.id,
            producer_role=submission.producer_role,
            evidence_type=submission.evidence_type,
            schema_version=submission.schema_version,
            provenance=submission.provenance,
            observed_at=submission.observed_at,
            created_at=now,
            recorded_at=now,
            valid_until=submission.valid_until,
            status=submission.status,
            confidence=submission.confidence,
            reason_codes=submission.reason_codes,
            payload=submission.payload,
            correlation_id=submission.correlation_id,
            supersedes_id=submission.supersedes_id,
            idempotency_key=submission.idempotency_key,
            submission_fingerprint=submission.fingerprint(),
        )
        session.add(
            TradeCaseEvidenceRow(
                evidence_id=envelope.evidence_id,
                trade_case_id=row.id,
                producer_role=envelope.producer_role.value,
                evidence_type=envelope.evidence_type.value,
                schema_version=envelope.schema_version,
                observed_at=envelope.observed_at,
                created_at=envelope.created_at,
                recorded_at=envelope.recorded_at,
                valid_until=envelope.valid_until,
                status=envelope.status.value,
                correlation_id=envelope.correlation_id,
                supersedes_id=envelope.supersedes_id,
                idempotency_key=envelope.idempotency_key,
                submission_fingerprint=envelope.submission_fingerprint,
                payload=envelope.model_dump(mode="json"),
            )
        )
        await session.flush()
        self._event(
            session,
            row,
            "EVIDENCE_RECORDED",
            "EVIDENCE_RECORDED",
            {
                "evidence_id": str(envelope.evidence_id),
                "evidence_type": envelope.evidence_type.value,
                "producer_role": envelope.producer_role.value,
                "status": envelope.status.value,
            },
        )
        if envelope.supersedes_id is not None:
            self._event(
                session,
                row,
                "EVIDENCE_SUPERSEDED",
                "EVIDENCE_SUPERSEDED",
                {
                    "evidence_id": str(envelope.evidence_id),
                    "supersedes_id": str(envelope.supersedes_id),
                },
            )
        return envelope

    async def _complete_evidence_task(
        self, session: AsyncSession, row: TradeCaseRow, evidence_type: EvidenceType
    ) -> None:
        requirement = self.policy.requirement(evidence_type)
        # One row per (case, role, task_type) slot; `attempt` is a mutable counter,
        # so completion must not be pinned to the first attempt.
        task = await session.scalar(
            select(TradeCaseTaskRow).where(
                TradeCaseTaskRow.trade_case_id == row.id,
                TradeCaseTaskRow.role == requirement.role.value,
                TradeCaseTaskRow.task_type == requirement.task_type,
            )
        )
        if task is None or SpecialistTaskStatus(task.status) in TERMINAL_TASK_STATUSES:
            return
        now = self.clock.now()
        task.status = SpecialistTaskStatus.SUCCEEDED.value
        task.started_at = task.started_at or now
        task.completed_at = now
        task.reason_code = "EVIDENCE_SUBMITTED"
        self._event(
            session,
            row,
            "TASK_COMPLETED",
            "EVIDENCE_SUBMITTED",
            {"task_id": str(task.task_id)},
        )

    async def _rearm_derived_tasks(
        self, session: AsyncSession, row: TradeCaseRow, evidence_type: EvidenceType
    ) -> None:
        """Re-arm a completed derived task when one of its inputs is replaced.

        A derived result is a view over other evidence. The moment one of those
        inputs is superseded the view describes an evidence set the case has
        left behind — so the task that produced it becomes claimable again and a
        fresh result is derived for the new set. Without this the view would be
        written once and then quietly disagree with the case forever.

        Four things bound it, and each is doing real work.

        **Only derived tasks.** `derived_from` is empty for everything that
        observes rather than derives, so ATLAS finishing does not make SIGNAL
        runnable again. Re-running every completed task on any evidence change
        would turn one supersession into an unbounded cascade.

        **Only on an input.** Recording the derived output itself is not an
        input to it, so a synthesis cannot re-arm its own task and loop. The set
        of inputs is declared by the policy rather than inferred.

        **Only before the stage closes.** Once a trigger exists the case has
        moved past the pre-trigger question this task answers, and an advisory
        refresh of that question would be work queued to describe a stage nobody
        is at.

        **Only a live case.** A terminal case cannot be re-derived into.

        Re-arming reuses the task's own row and bumps its attempt counter, so a
        case keeps one slot per role rather than accumulating one per revision.
        """
        definitions = self.policy.derived_tasks(evidence_type)
        if not definitions:
            return
        if TradeCaseStatus(row.status) in TERMINAL_CASE_STATUSES:
            return
        rows = (
            await session.scalars(
                select(TradeCaseEvidenceRow).where(TradeCaseEvidenceRow.trade_case_id == row.id)
            )
        ).all()
        evidence = tuple(evidence_from_row(item) for item in rows)
        if EvidenceType.TRIGGER in active_evidence(evidence):
            # The pre-trigger stage is over. Nothing re-derives into it.
            return

        now = self.clock.now()
        for definition in definitions:
            task = await session.scalar(
                select(TradeCaseTaskRow).where(
                    TradeCaseTaskRow.trade_case_id == row.id,
                    TradeCaseTaskRow.role == definition.role.value,
                    TradeCaseTaskRow.task_type == definition.task_type,
                )
            )
            if task is None:
                continue
            if SpecialistTaskStatus(task.status) != SpecialistTaskStatus.SUCCEEDED:
                # Still pending, running or permanently finished. A task that has
                # not yet produced anything needs no second chance, and one that
                # failed terminally is not resurrected by an input changing.
                continue
            if task.expires_at is not None and now >= aware(task.expires_at):
                continue
            self._rearm(
                session,
                row,
                task,
                "DERIVED_INPUT_CHANGED",
                {"changed_evidence_type": evidence_type.value},
            )

    def _rearm(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        task: TradeCaseTaskRow,
        reason_code: str,
        detail: dict[str, object],
    ) -> None:
        """Put one finished task slot back on the queue.

        Re-arming reuses the task's own row and bumps its attempt counter, so a
        case keeps one slot per role rather than accumulating one per revision —
        and the runtime's claim ceiling on that slot therefore bounds how often
        it can ever be re-armed, without a second budget being invented here.

        The caller decides *whether* a slot may be re-armed. This decides what
        re-arming is, so every reason produces the same durable shape.
        """
        task.status = SpecialistTaskStatus.PENDING.value
        task.attempt += 1
        task.started_at = None
        task.completed_at = None
        task.failure_category = None
        task.next_eligible_at = None
        # The finished attempt's lease has no claim on the new one. Leaving
        # it would make the re-armed task look busy until that lease's own
        # expiry, which is a delay measured in whatever the lease duration
        # happens to be rather than in anything meaningful.
        task.lease_id = None
        task.worker_instance_id = None
        task.lease_started_at = None
        task.lease_expires_at = None
        task.lease_renewals = 0
        task.reason_code = reason_code
        self._event(
            session,
            row,
            "TASK_STATUS_CHANGED",
            reason_code,
            {
                "task_id": str(task.task_id),
                "role": task.role,
                "to": SpecialistTaskStatus.PENDING.value,
                "attempt": task.attempt,
                **detail,
            },
        )

    async def refresh_source(
        self, trade_case_id: UUID, source: str, *, expected_revision: int | None = None
    ) -> SourceRefreshOrder:
        """Order a new observation of one risk source, through the task that owns it.

        A case that reaches its trigger after a wait can be complete by every
        workflow rule and still be judged on a reading older than the risk
        engine's own bound. The gap is real and it is not closed by reading the
        same reading again: what is needed is a *new observation*, produced by
        the handler that observes that source, recorded as evidence, and bound
        to the case revision it was produced for.

        This orders exactly that, and nothing more. It arms a task; it does not
        observe, does not record evidence, does not touch a status and does not
        decide anything about the case. Whatever the handler then finds goes
        through the ordinary submission path, supersedes the reading it
        replaces, and is re-evaluated by this service like any other evidence —
        which is also why a source that has genuinely not changed stays exactly
        as old as it was.

        Five things bound it.

        **Only a declared source.** `refreshable_sources` names the sources this
        workflow can observe again and which evidence carries each. A source no
        task observes is refused rather than approximated.

        **Only a case waiting on a risk request.** `READY_FOR_RISK` is the one
        state where a fresher precondition changes anything: earlier, the case
        is still being built and the ordinary tasks are running anyway; later,
        the decision has been made and remaking its inputs would be reopening a
        settled question.

        **Only a slot that finished.** A task still pending or running already
        represents outstanding work, so a second order is refused instead of
        queued. Two runs racing here serialize on the case row and the second
        sees the first's order, which is what keeps a restart or a parallel run
        from creating duplicate observation work.

        **Only inside the existing claim budget.** Re-arming spends an attempt
        of the slot's own ceiling, so a case cannot be refreshed without end;
        when the ceiling is reached the order is refused rather than written and
        then refused later by the claim.

        **Never a status change.** The evidence set is unchanged until a new
        reading is recorded, so the case stays exactly where it was and the
        workflow remains the only thing that moves it.
        """
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            self._revision(row, expected_revision)
            refreshable = self.policy.refreshable(source)
            if refreshable is None:
                return self._refresh_refused(
                    row, source, SourceRefreshOutcome.SOURCE_NOT_REFRESHABLE
                )
            if TradeCaseStatus(row.status) is not TradeCaseStatus.READY_FOR_RISK:
                return self._refresh_refused(row, source, SourceRefreshOutcome.CASE_NOT_READY)
            requirement = self.policy.requirement(refreshable.evidence_type)
            task = await session.scalar(
                select(TradeCaseTaskRow).where(
                    TradeCaseTaskRow.trade_case_id == row.id,
                    TradeCaseTaskRow.role == requirement.role.value,
                    TradeCaseTaskRow.task_type == requirement.task_type,
                )
            )
            if task is None:
                return self._refresh_refused(row, source, SourceRefreshOutcome.OBSERVER_UNAVAILABLE)
            status = SpecialistTaskStatus(task.status)
            if status in (SpecialistTaskStatus.PENDING, SpecialistTaskStatus.RUNNING):
                return self._refresh_refused(
                    row,
                    source,
                    SourceRefreshOutcome.ALREADY_ORDERED,
                    task=task,
                    requirement=requirement,
                )
            now = self.clock.now()
            expiries = (task.expires_at, row.expires_at)
            if (
                status is not SpecialistTaskStatus.SUCCEEDED
                or any(item is not None and now >= aware(item) for item in expiries)
                or task.attempt >= task.max_attempts
            ):
                return self._refresh_refused(
                    row,
                    source,
                    SourceRefreshOutcome.OBSERVER_UNAVAILABLE,
                    task=task,
                    requirement=requirement,
                )
            self._rearm(session, row, task, "RISK_SOURCE_TOO_OLD", {"source": source})
            return SourceRefreshOrder(
                outcome=SourceRefreshOutcome.ORDERED,
                source=source,
                trade_case_id=row.id,
                case_revision=row.revision,
                role=requirement.role,
                task_type=requirement.task_type,
                task_id=task.task_id,
                attempt=task.attempt,
            )

    @staticmethod
    def _refresh_refused(
        row: TradeCaseRow,
        source: str,
        outcome: SourceRefreshOutcome,
        *,
        task: TradeCaseTaskRow | None = None,
        requirement: EvidenceRequirement | None = None,
    ) -> SourceRefreshOrder:
        """A refusal that still names what was asked about, where that is known.

        `attempt` is deliberately left out: nothing was armed, and reporting a
        counter next to a refusal invites it to be read as progress.
        """
        return SourceRefreshOrder(
            outcome=outcome,
            source=source,
            trade_case_id=row.id,
            case_revision=row.revision,
            role=None if requirement is None else requirement.role,
            task_type=None if requirement is None else requirement.task_type,
            task_id=None if task is None else task.task_id,
        )

    async def transition_task(
        self,
        trade_case_id: UUID,
        task_id: UUID,
        target: SpecialistTaskStatus,
        *,
        reason_code: str,
    ) -> SpecialistTask:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            self._mutable(row, None)
            task = await session.scalar(
                select(TradeCaseTaskRow)
                .where(
                    TradeCaseTaskRow.task_id == task_id,
                    TradeCaseTaskRow.trade_case_id == trade_case_id,
                )
                .with_for_update()
            )
            if task is None:
                raise WorkflowFailure(WorkflowErrorCode.NOT_FOUND)
            current = SpecialistTaskStatus(task.status)
            if target not in TASK_TRANSITIONS.get(current, frozenset()):
                raise WorkflowFailure(WorkflowErrorCode.TASK_TRANSITION)
            now = self.clock.now()
            task.status = target.value
            task.reason_code = reason_code
            if target == SpecialistTaskStatus.RUNNING:
                task.started_at = now
            if target in TERMINAL_TASK_STATUSES:
                task.completed_at = now
            self._event(
                session,
                row,
                "TASK_STATUS_CHANGED",
                reason_code,
                {"task_id": str(task_id), "from": current.value, "to": target.value},
            )
            await session.flush()
            return task_from_row(task)

    async def evaluate_trade_case(
        self, trade_case_id: UUID, *, expected_revision: int | None = None
    ) -> TradeCase:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            self._revision(row, expected_revision)
            await self._expire_tasks(session, row)
            await self._stabilize(session, row)
            return case_from_row(row)

    async def workflow_inputs_in_session(
        self, session: AsyncSession, row: TradeCaseRow
    ) -> WorkflowInputs:
        """Load everything an evaluation needs, inside a caller's transaction.

        Separated from the evaluation itself so a caller can read its clock
        *after* the last of these reads. An instant taken before them describes
        the moment the loading started, not the moment the answer is used, and a
        case can lapse in between — which is exactly the window this split
        closes.

        Read-only, and the same reads `_stabilize` performs.
        """
        trade_case = case_from_row(row)
        evidence = tuple(
            evidence_from_row(item)
            for item in (
                await session.scalars(
                    select(TradeCaseEvidenceRow)
                    .where(TradeCaseEvidenceRow.trade_case_id == row.id)
                    .order_by(
                        TradeCaseEvidenceRow.recorded_at,
                        TradeCaseEvidenceRow.evidence_id,
                    )
                )
            ).all()
        )
        binding_row = await session.scalar(
            select(TradeCaseRiskBindingRow)
            .where(TradeCaseRiskBindingRow.trade_case_id == row.id)
            .order_by(
                TradeCaseRiskBindingRow.case_revision.desc(),
                TradeCaseRiskBindingRow.recorded_at.desc(),
                TradeCaseRiskBindingRow.binding_id.desc(),
            )
            .limit(1)
        )
        return WorkflowInputs(
            trade_case=trade_case,
            evidence=evidence,
            risk_binding=risk_from_row(binding_row) if binding_row is not None else None,
        )

    def evaluate_inputs(self, inputs: WorkflowInputs, now: datetime) -> Evaluation:
        """What the case is at `now`, computed without touching the database.

        Synchronous on purpose: nothing between a caller's last clock read and
        this answer may await, or the instant the answer describes drifts again.

        Deliberately the *same* evaluator `_stabilize` uses. There is one
        requirement table, one transition matrix and one freshness rule, and
        this reads them at the instant given instead of trusting a row written
        at an earlier one. Nothing here writes, so the caller decides what the
        answer means.
        """
        return self.evaluator.evaluate(inputs.trade_case, inputs.evidence, inputs.risk_binding, now)

    async def _stabilize(self, session: AsyncSession, row: TradeCaseRow) -> None:
        for _ in range(12):
            trade_case = case_from_row(row)
            evidence = tuple(
                evidence_from_row(item)
                for item in (
                    await session.scalars(
                        select(TradeCaseEvidenceRow)
                        .where(TradeCaseEvidenceRow.trade_case_id == row.id)
                        .order_by(
                            TradeCaseEvidenceRow.recorded_at,
                            TradeCaseEvidenceRow.evidence_id,
                        )
                    )
                ).all()
            )
            binding_row = await session.scalar(
                select(TradeCaseRiskBindingRow)
                .where(TradeCaseRiskBindingRow.trade_case_id == row.id)
                .order_by(
                    TradeCaseRiskBindingRow.case_revision.desc(),
                    TradeCaseRiskBindingRow.recorded_at.desc(),
                    TradeCaseRiskBindingRow.binding_id.desc(),
                )
                .limit(1)
            )
            binding = risk_from_row(binding_row) if binding_row is not None else None
            result = self.evaluator.evaluate(trade_case, evidence, binding, self.clock.now())
            blockers = [item.model_dump(mode="json") for item in result.blockers]
            changed = (
                row.status != result.status.value
                or row.reason_code != result.reason_code
                or row.blockers != blockers
                or row.risk_input_digest != result.risk_input_digest
            )
            if not changed:
                return
            old_status = row.status
            old_blockers = row.blockers
            self._allowed_case_transition(TradeCaseStatus(old_status), result.status)
            row.status = result.status.value
            row.reason_code = result.reason_code
            row.blockers = blockers
            row.risk_input_digest = result.risk_input_digest
            row.updated_at = self.clock.now()
            row.revision += 1
            if old_status != row.status:
                session.add(
                    TradeCaseTransitionRow(
                        transition_id=uuid4(),
                        trade_case_id=row.id,
                        revision=row.revision,
                        from_status=old_status,
                        to_status=row.status,
                        reason_code=row.reason_code,
                        recorded_at=row.updated_at,
                        correlation_id=row.correlation_id,
                        blockers=row.blockers,
                        risk_input_digest=row.risk_input_digest,
                    )
                )
                self._event(
                    session,
                    row,
                    "STATE_TRANSITION",
                    row.reason_code,
                    {
                        "from_status": old_status,
                        "to_status": row.status,
                        "revision": row.revision,
                    },
                )
            else:
                self._event(
                    session,
                    row,
                    "CASE_REEVALUATED",
                    row.reason_code,
                    {"status": row.status, "revision": row.revision},
                )
            if old_blockers != blockers:
                self._event(
                    session,
                    row,
                    "BLOCKERS_CHANGED",
                    "BLOCKERS_UPDATED",
                    {"before": old_blockers, "after": blockers},
                )
            await session.flush()
        raise WorkflowFailure(WorkflowErrorCode.ILLEGAL_TRANSITION)

    async def record_risk_decision(
        self,
        trade_case_id: UUID,
        decision: RiskDecision,
        *,
        risk_input_digest: str,
        expected_revision: int | None = None,
    ) -> TradeCase:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            await self.record_risk_decision_in_session(
                session,
                row,
                decision,
                risk_input_digest=risk_input_digest,
                expected_revision=expected_revision,
            )
            return case_from_row(row)

    async def record_risk_decision_in_session(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        decision: RiskDecision,
        *,
        risk_input_digest: str,
        expected_revision: int | None = None,
    ) -> UUID:
        """Bind one SENTINEL decision inside a transaction the caller owns.

        Exists because a decision and the state it was reached from have to
        commit together. A caller that has already locked the paper account and
        assembled a portfolio cannot hand the write to a service that opens its
        own transaction: the two would commit independently, and a crash between
        them would leave a decision bound to a portfolio nobody can reconstruct.

        The caller must already hold the case row locked, which is what the
        row argument makes visible. Lock order stays paper account, then trade
        case.
        """
        decision = RiskDecision.model_validate_json(decision.model_dump_json())
        trade_case_id = row.id
        payload = decision.model_dump(mode="json")
        existing = await session.scalar(
            select(TradeCaseRiskBindingRow).where(
                TradeCaseRiskBindingRow.risk_decision_id == decision.id
            )
        )
        if existing is not None:
            binding = risk_from_row(existing)
            if (
                binding.trade_case_id != trade_case_id
                or binding.risk_input_digest != risk_input_digest
                or binding.decision_payload != payload
            ):
                raise WorkflowFailure(WorkflowErrorCode.IDEMPOTENCY_CONFLICT)
            await self._stabilize(session, row)
            return binding.binding_id
        self._mutable(row, expected_revision)
        if (
            row.status != TradeCaseStatus.READY_FOR_RISK.value
            or row.risk_input_digest != risk_input_digest
            or decision.correlation_id != row.correlation_id
            or decision.evaluated_at > self.clock.now()
            or self.clock.now() >= decision.expires_at
        ):
            raise WorkflowFailure(WorkflowErrorCode.RISK_BINDING)
        recorded_at = self.clock.now()
        authorization = classify_decision(decision)
        binding_id = uuid5(
            NAMESPACE_URL,
            f"rh-agents:trade-case-risk:{trade_case_id}:{decision.id}",
        )
        session.add(
            TradeCaseRiskBindingRow(
                binding_id=binding_id,
                trade_case_id=trade_case_id,
                risk_decision_id=decision.id,
                case_revision=row.revision,
                risk_input_digest=risk_input_digest,
                outcome=decision.outcome.value,
                authorization=authorization.value,
                reason_codes=list(decision.reason_codes),
                position_size_limit_usd=decision.position_size_limit_usd,
                max_additional_notional_usd=decision.max_additional_notional_usd,
                max_slippage_bps=decision.max_slippage_bps,
                evaluated_at=decision.evaluated_at,
                expires_at=decision.expires_at,
                recorded_at=recorded_at,
                correlation_id=decision.correlation_id,
                payload=payload,
            )
        )
        self._event(
            session,
            row,
            "RISK_DECISION_BOUND",
            "SENTINEL_DECISION_BOUND",
            {
                "risk_decision_id": str(decision.id),
                "risk_input_digest": risk_input_digest,
                "outcome": decision.outcome.value,
                "authorization": authorization.value,
            },
        )
        await session.flush()
        await self._stabilize(session, row)
        return binding_id

    async def complete_execution_in_session(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        *,
        detail: dict[str, object],
    ) -> None:
        """End a case because its authorised entry was filled.

        The workflow stays the status owner: this goes through the same guarded
        transition every other status change uses, so the matrix decides whether
        `EXECUTED` is reachable from where the case actually is. It is reachable
        only from `RISK_APPROVED`, which means only an authorised case can be
        ended this way and no caller can write the status directly.

        Terminal on purpose. The case asked one question, received one
        authorization and spent it. What may happen to the resulting position —
        adding to it, exiting it, or deciding that a later entry is a different
        trade — has no contract yet, and inventing one here would be a strategy
        hidden in a state machine.
        """
        self._mutable(row, None)
        await self._direct_transition(session, row, TradeCaseStatus.EXECUTED, "ENTRY_EXECUTED", ())
        self._event(session, row, "ENTRY_EXECUTED", "ENTRY_EXECUTED", detail)
        await session.flush()

    async def expire_trade_case(self, trade_case_id: UUID) -> TradeCase:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            if TradeCaseStatus(row.status) in TERMINAL_CASE_STATUSES:
                raise WorkflowFailure(WorkflowErrorCode.TERMINAL_CASE)
            if row.expires_at is None or self.clock.now() < aware(row.expires_at):
                raise WorkflowFailure(WorkflowErrorCode.ILLEGAL_TRANSITION)
            await self._stabilize(session, row)
            return case_from_row(row)

    async def cancel_trade_case(
        self, trade_case_id: UUID, *, reason_code: str = "CASE_CANCELLED"
    ) -> TradeCase:
        async with self.sessions.begin() as session:
            row = await self._locked_case(session, trade_case_id)
            self._mutable(row, None)
            await self._direct_transition(session, row, TradeCaseStatus.CANCELLED, reason_code, ())
            return case_from_row(row)

    async def _direct_transition(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        target: TradeCaseStatus,
        reason_code: str,
        blockers: tuple[dict[str, object], ...],
    ) -> None:
        old = row.status
        self._allowed_case_transition(TradeCaseStatus(old), target)
        row.status = target.value
        row.reason_code = reason_code
        row.blockers = list(blockers)
        row.risk_input_digest = None
        row.updated_at = self.clock.now()
        row.revision += 1
        session.add(
            TradeCaseTransitionRow(
                transition_id=uuid4(),
                trade_case_id=row.id,
                revision=row.revision,
                from_status=old,
                to_status=row.status,
                reason_code=reason_code,
                recorded_at=row.updated_at,
                correlation_id=row.correlation_id,
                blockers=row.blockers,
                risk_input_digest=None,
            )
        )
        self._event(
            session,
            row,
            "STATE_TRANSITION",
            reason_code,
            {"from_status": old, "to_status": row.status, "revision": row.revision},
        )

    def _mutable(self, row: TradeCaseRow, expected_revision: int | None) -> None:
        self._revision(row, expected_revision)
        if TradeCaseStatus(row.status) in TERMINAL_CASE_STATUSES:
            raise WorkflowFailure(WorkflowErrorCode.TERMINAL_CASE)

    @staticmethod
    def _revision(row: TradeCaseRow, expected_revision: int | None) -> None:
        if expected_revision is not None and row.revision != expected_revision:
            raise WorkflowFailure(WorkflowErrorCode.CONCURRENCY_CONFLICT)

    @staticmethod
    def _allowed_case_transition(current: TradeCaseStatus, target: TradeCaseStatus) -> None:
        if target not in CASE_TRANSITIONS.get(current, frozenset()):
            raise WorkflowFailure(WorkflowErrorCode.ILLEGAL_TRANSITION)

    async def _expire_tasks(self, session: AsyncSession, row: TradeCaseRow) -> None:
        tasks = (
            await session.scalars(
                select(TradeCaseTaskRow).where(
                    TradeCaseTaskRow.trade_case_id == row.id,
                    TradeCaseTaskRow.status.in_(
                        [
                            SpecialistTaskStatus.PENDING.value,
                            SpecialistTaskStatus.RUNNING.value,
                            SpecialistTaskStatus.BLOCKED.value,
                        ]
                    ),
                )
            )
        ).all()
        now = self.clock.now()
        for task in tasks:
            if task.expires_at is not None and now >= aware(task.expires_at):
                task.status = SpecialistTaskStatus.EXPIRED.value
                task.completed_at = now
                task.reason_code = "TASK_EXPIRED"
                self._event(
                    session,
                    row,
                    "TASK_STATUS_CHANGED",
                    "TASK_EXPIRED",
                    {"task_id": str(task.task_id), "to": SpecialistTaskStatus.EXPIRED.value},
                )

    def _event(
        self,
        session: AsyncSession,
        row: TradeCaseRow,
        event_type: str,
        reason_code: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            TradeCaseEventRow(
                event_id=uuid4(),
                trade_case_id=row.id,
                event_type=event_type,
                reason_code=reason_code,
                recorded_at=self.clock.now(),
                correlation_id=row.correlation_id,
                payload=payload,
            )
        )

    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase:
        async with self.sessions() as session:
            row = await session.get(TradeCaseRow, trade_case_id)
            if row is None:
                raise WorkflowFailure(WorkflowErrorCode.NOT_FOUND)
            return case_from_row(row)

    async def list_trade_cases(
        self,
        *,
        status: TradeCaseStatus | None = None,
        chain: str | None = None,
        market: str | None = None,
        updated_since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[TradeCase, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("TradeCase list limit must be between 1 and 100")
        statement = select(TradeCaseRow)
        if status is not None:
            statement = statement.where(TradeCaseRow.status == status.value)
        if chain is not None:
            statement = statement.where(TradeCaseRow.chain == chain)
        if market is not None:
            statement = statement.where(TradeCaseRow.market_key == market)
        if updated_since is not None:
            statement = statement.where(TradeCaseRow.updated_at >= updated_since)
        statement = statement.order_by(
            TradeCaseRow.updated_at.desc(), TradeCaseRow.id.desc()
        ).limit(limit)
        async with self.sessions() as session:
            return tuple(case_from_row(row) for row in (await session.scalars(statement)).all())

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]:
        async with self.sessions() as session:
            await self._require_case(session, trade_case_id)
            rows = (
                await session.scalars(
                    select(TradeCaseEvidenceRow)
                    .where(TradeCaseEvidenceRow.trade_case_id == trade_case_id)
                    .order_by(
                        TradeCaseEvidenceRow.recorded_at,
                        TradeCaseEvidenceRow.evidence_id,
                    )
                )
            ).all()
            return tuple(evidence_from_row(row) for row in rows)

    async def tasks(self, trade_case_id: UUID) -> tuple[SpecialistTask, ...]:
        async with self.sessions() as session:
            await self._require_case(session, trade_case_id)
            rows = (
                await session.scalars(
                    select(TradeCaseTaskRow)
                    .where(TradeCaseTaskRow.trade_case_id == trade_case_id)
                    .order_by(TradeCaseTaskRow.created_at, TradeCaseTaskRow.task_id)
                )
            ).all()
            return tuple(task_from_row(row) for row in rows)

    async def timeline(self, trade_case_id: UUID) -> tuple[TimelineEvent, ...]:
        async with self.sessions() as session:
            await self._require_case(session, trade_case_id)
            rows = (
                await session.scalars(
                    select(TradeCaseEventRow)
                    .where(TradeCaseEventRow.trade_case_id == trade_case_id)
                    .order_by(TradeCaseEventRow.sequence)
                )
            ).all()
            return tuple(
                TimelineEvent(
                    sequence=row.sequence,
                    event_id=row.event_id,
                    trade_case_id=row.trade_case_id,
                    event_type=row.event_type,
                    reason_code=row.reason_code,
                    recorded_at=aware(row.recorded_at),
                    correlation_id=row.correlation_id,
                    payload=row.payload,
                )
                for row in rows
            )

    @staticmethod
    async def _require_case(session: AsyncSession, trade_case_id: UUID) -> None:
        if await session.get(TradeCaseRow, trade_case_id) is None:
            raise WorkflowFailure(WorkflowErrorCode.NOT_FOUND)
