"""Assembly of the COMMANDER view: authoritative state, already decided elsewhere.

Every fact here was established by something that owns it. The evaluator decided
the status, the blockers and the freshness; the specialists decided their
findings; SENTINEL decided the authorization; configuration decided the stops.
This gathers them into one typed view and computes a digest over exactly the
facts a decision may rest on.

The server builds it, and that ordering is the point. A coordinator that could
choose which evidence to read could choose the evidence that suited a
conclusion — and unlike a specialist, a coordinator has no domain of its own
whose judgement would be visible if it did.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import TradingMode
from src.data.tables import AccountRow, TradeCaseRiskBindingRow
from src.orchestration.commander.models import (
    AdvisorySynthesis,
    CommanderContext,
    EvidenceState,
    RiskState,
    SystemControls,
    TaskState,
)
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1, CommanderControlPolicy
from src.orchestration.workflow.engine import active_evidence, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceType,
    SynthesisPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1, WorkflowPolicy
from src.orchestration.workflow.service import TradeCaseService, risk_from_row


class SystemPausePort(Protocol):
    """Whether a durable system-wide stop is in force.

    One question, no setter. A control plane may observe a pause and must never
    be able to lift one.
    """

    async def system_paused(self) -> bool: ...


@dataclass(frozen=True)
class AccountPauseReader:
    """Reads the Phase 0 accounting pause, for deployments that carry it."""

    sessions: async_sessionmaker[AsyncSession]

    async def system_paused(self) -> bool:
        async with self.sessions() as session:
            paused = await session.scalar(select(AccountRow.paused).limit(1))
        return bool(paused)


class CommanderContextUnavailable(Exception):
    """No orchestration context exists. Carries a safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def context_digest(
    *,
    trade_case_id: UUID,
    workflow_version: str,
    policy_version: str,
    status: str,
    revision: int,
    evidence: tuple[EvidenceState, ...],
    tasks: tuple[TaskState, ...],
    risk: RiskState | None,
    controls: SystemControls,
    current_risk_input_digest: str | None,
) -> str:
    """A canonical fingerprint of the facts a decision may rest on.

    Covers the case's own identity and published verdict, every current
    envelope by identity and fingerprint, the task slots, the authorization and
    the system stops. Entries are sorted by value, so no database ordering can
    reach the hash.

    Deliberately excludes the lease, the worker, the attempt, the read time and
    anything advisory. Two reads of the same state must produce the same digest,
    or the digest would measure when somebody looked rather than what they saw —
    and stale-context fencing would then reject every honest retry.

    The advisory synthesis is excluded for a second reason: including it would
    make FUSE able to invalidate a COMMANDER action, which is authority the
    advisory layer does not have.
    """
    canonical = json.dumps(
        {
            "trade_case_id": str(trade_case_id),
            "workflow_version": workflow_version,
            "policy_version": policy_version,
            "status": status,
            "revision": revision,
            "risk_input_digest": current_risk_input_digest,
            "evidence": sorted(
                [
                    {
                        "evidence_type": item.evidence_type.value,
                        "producer_role": item.role.value,
                        "evidence_id": str(item.evidence_id),
                        "submission_fingerprint": item.submission_fingerprint,
                        "status": item.status.value,
                        "acceptance": item.acceptance.value,
                    }
                    for item in evidence
                ],
                key=lambda entry: entry["evidence_type"],
            ),
            # Required tasks only. An optional advisory task finishing cannot
            # change what coordination may do next, so letting it move the digest
            # would hand the advisory layer the power to invalidate an in-flight
            # decision — the same authority the synthesis itself is kept out of
            # the evidence list to deny it.
            "tasks": sorted(
                [
                    {
                        "role": item.role.value,
                        "task_type": item.task_type,
                        "status": item.status.value,
                        "attempt": item.attempt,
                    }
                    for item in tasks
                    if item.required
                ],
                key=lambda entry: (entry["role"], entry["task_type"]),
            ),
            "risk": None
            if risk is None
            else {
                "binding_id": str(risk.binding_id),
                "authorization": risk.authorization.value,
                "risk_input_digest": risk.risk_input_digest,
                "matches_current_inputs": risk.matches_current_inputs,
            },
            "controls": {
                "kill_switch": controls.kill_switch,
                "account_paused": controls.account_paused,
                "trading_mode": controls.trading_mode,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CommanderContextReader:
    """Builds the orchestration view from existing authoritative services only."""

    cases: TradeCaseService
    sessions: async_sessionmaker[AsyncSession]
    policy: CommanderControlPolicy = COMMANDER_CONTROL_V1
    workflow: WorkflowPolicy = TRADE_CASE_V1
    clock: Clock = SystemClock()
    kill_switch: bool = False
    # Supplied only by a deployment that also runs the Phase 0 accounting
    # subsystem, which is where the durable pause lives.
    pause: SystemPausePort | None = None

    async def commander_context(self, trade_case_id: UUID, task_id: UUID) -> CommanderContext:
        now = self.clock.now()
        try:
            trade_case = await self.cases.get_trade_case(trade_case_id)
        except WorkflowFailure as error:
            if error.code == WorkflowErrorCode.NOT_FOUND:
                raise CommanderContextUnavailable("TRADE_CASE_NOT_FOUND") from None
            raise

        evidence = await self.cases.evidence(trade_case_id)
        current = active_evidence(evidence)
        states: list[EvidenceState] = []
        for evidence_type, item in sorted(current.items(), key=lambda kv: kv[0].value):
            if evidence_type is EvidenceType.SYNTHESIS:
                # Advisory, and it travels as `advisory` instead. Keeping it out
                # of the canonical evidence list is what stops the advisory layer
                # from acquiring fencing authority: were it here it would enter
                # the digest, and recording a synthesis would then invalidate any
                # in-flight coordination decision — which is real power over the
                # control plane, granted to the one component that is explicitly
                # authoritative over nothing.
                continue
            requirement = _requirement(self.workflow, evidence_type)
            states.append(
                EvidenceState(
                    role=item.producer_role,
                    evidence_type=evidence_type,
                    evidence_id=item.evidence_id,
                    submission_fingerprint=item.submission_fingerprint,
                    status=item.effective_status(now),
                    acceptance=item.payload.acceptance(),
                    required=requirement[0],
                    safety_critical=requirement[1],
                )
            )

        tasks = tuple(
            TaskState(
                role=task.role,
                task_type=task.task_type,
                status=task.status,
                attempt=task.attempt,
                required=task.required,
            )
            for task in sorted(
                await self.cases.tasks(trade_case_id),
                key=lambda item: (item.role.value, item.task_type),
            )
        )

        digest = risk_input_digest(trade_case, current, self.workflow)
        risk = await self._risk(trade_case_id, digest)
        controls = await self._controls()
        advisory = _advisory(current)

        return CommanderContext(
            trade_case_id=trade_case_id,
            task_id=task_id,
            workflow_version=trade_case.workflow_version,
            policy_version=self.policy.version,
            status=trade_case.status,
            revision=trade_case.revision,
            reason_code=trade_case.reason_code,
            blocker_codes=tuple(item.code for item in trade_case.blockers)[:24],
            evidence=tuple(states),
            tasks=tasks,
            risk=risk,
            advisory=advisory,
            controls=controls,
            current_risk_input_digest=digest,
            observed_at=now,
            context_digest=context_digest(
                trade_case_id=trade_case_id,
                workflow_version=trade_case.workflow_version,
                policy_version=self.policy.version,
                status=trade_case.status.value,
                revision=trade_case.revision,
                evidence=tuple(states),
                tasks=tasks,
                risk=risk,
                controls=controls,
                current_risk_input_digest=digest,
            ),
        )

    async def _risk(self, trade_case_id: UUID, digest: str) -> RiskState | None:
        """The newest authorization, and whether it still describes this case."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(TradeCaseRiskBindingRow)
                .where(TradeCaseRiskBindingRow.trade_case_id == trade_case_id)
                .order_by(
                    TradeCaseRiskBindingRow.case_revision.desc(),
                    TradeCaseRiskBindingRow.recorded_at.desc(),
                    TradeCaseRiskBindingRow.binding_id.desc(),
                )
                .limit(1)
            )
        if row is None:
            return None
        binding = risk_from_row(row)
        return RiskState(
            binding_id=binding.binding_id,
            risk_decision_id=binding.risk_decision_id,
            authorization=binding.authorization,
            risk_input_digest=binding.risk_input_digest,
            # The only question worth asking of an authorization: was it granted
            # against the evidence this case currently has? One granted against
            # a different set is not weaker — it is about a case that no longer
            # exists.
            matches_current_inputs=binding.risk_input_digest == digest,
            expires_at=binding.expires_at,
        )

    async def _controls(self) -> SystemControls:
        """The system-wide stops, as facts rather than opinions.

        Two mechanisms exist and they are not equivalent.

        The kill switch is configuration SENTINEL itself honours, and it applies
        to this flow directly.

        The recorded pause is a durable column set when SENTINEL returns
        ``PAUSE_SYSTEM`` — but it lives in the Phase 0 accounting subsystem,
        which the TradeCase workflow has no link to and whose schema a
        workflow-only deployment does not carry. So it is read through an
        injected port rather than assumed: a deployment that runs both
        subsystems supplies one, and a deployment that does not says so by its
        absence instead of a query failing against a table it never created.

        The gap is real and is documented rather than papered over: nothing in
        the TradeCase flow can currently *set* that pause, because the service
        that sets it never runs here. Until that is wired, the kill switch is
        the stop that applies.
        """
        paused = False if self.pause is None else await self.pause.system_paused()
        return SystemControls(
            kill_switch=self.kill_switch,
            account_paused=paused,
            trading_mode=TradingMode.PAPER.value,
        )


def _requirement(policy: WorkflowPolicy, evidence_type: EvidenceType) -> tuple[bool, bool]:
    for item in policy.requirements:
        if item.evidence_type == evidence_type:
            return item.required, item.safety_critical
    return False, False


def _advisory(current: dict[EvidenceType, EvidenceEnvelope]) -> AdvisorySynthesis | None:
    """FUSE's reading, carried only when it describes this case's current evidence.

    A synthesis of a superseded evidence set is not a weaker opinion; it is an
    opinion about a different case. Rather than hand one over with a caveat, the
    caveat is computed here and travels as a field the decision never reads.
    """
    item = current.get(EvidenceType.SYNTHESIS)
    if item is None:
        return None
    payload = item.payload
    if not isinstance(payload, SynthesisPayload):
        # Defensive: the type says it cannot happen, and evidence is durable, so
        # a payload shape that predates a contract change must not crash a read.
        return None
    live = {
        state.evidence_id for kind, state in current.items() if kind is not EvidenceType.SYNTHESIS
    }
    detail = payload.synthesis
    return AdvisorySynthesis(
        evidence_id=item.evidence_id,
        disposition=payload.disposition,
        hard_blocker_count=0 if detail is None else len(detail.hard_blockers),
        unresolved_gap_count=0 if detail is None else len(detail.unresolved_gaps),
        describes_current_inputs=set(payload.source_evidence_ids) <= live,
    )
