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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.repository import aware
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
from src.orchestration.workflow.engine import (
    TradeCaseEvaluator,
    active_evidence,
    risk_input_digest,
)
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceStatus,
    EvidenceType,
    RiskBinding,
    SynthesisPayload,
    TradeCase,
    TradeSetupPayload,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import TRADE_CASE_V1, WorkflowPolicy
from src.orchestration.workflow.service import TradeCaseService, risk_from_row


class SystemPausePort(Protocol):
    """Whether a durable system-wide stop is in force.

    Two reads, no setter — a control plane may observe a pause and must never be
    able to lift one.

    ``system_paused`` is the unsynchronised read, for building a view. It is
    inherently a snapshot and is never sufficient on its own to gate a write:
    any check-then-act leaves a window, and moving the check later only narrows
    it. ``locked_paused`` is the read that closes it, by taking the same lock
    the writer takes inside the caller's own transaction.
    """

    async def system_paused(self) -> bool: ...

    async def locked_paused(self, session: AsyncSession) -> bool: ...


class SystemPauseUnavailable(Exception):
    """The stop could not be read. Never the same as "there is no stop"."""


@dataclass(frozen=True)
class AccountPauseReader:
    """Reads the Phase 0 accounting pause for the one authoritative account.

    The account is singular by database constraint (`single_paper_account`,
    `id = 1`) and every other reader addresses it by that identity. Selecting
    whichever row came first would read a different account than the one the
    executor pauses, on any deployment that ever gained a second.

    A missing row is an unreadable control, not an unpaused system: the
    accounting subsystem has not been initialised, so nothing can be said about
    whether it is stopped. Fails closed by raising rather than returning False.
    """

    sessions: async_sessionmaker[AsyncSession]
    account_id: int = 1

    async def system_paused(self) -> bool:
        """A snapshot, for building a view. Never a gate on a write."""
        async with self.sessions() as session:
            paused = await session.scalar(
                select(AccountRow.paused).where(AccountRow.id == self.account_id)
            )
        if paused is None:
            raise SystemPauseUnavailable("PAUSE_STATE_UNAVAILABLE")
        return bool(paused)

    async def locked_paused(self, session: AsyncSession) -> bool:
        """Take the writer's own lock, in the caller's transaction, and answer.

        `PaperTradingService` sets the pause after selecting this row
        ``FOR UPDATE``. Taking the same lock here means the two transactions are
        ordered by the database rather than by timing: whichever acquires it
        first wins, and the loser sees the winner's committed state.

        **Lock order: paper account, then trade case.** Safe because nothing in
        this system acquires them the other way round — the workflow locks trade
        cases and never touches the account, and the paper service locks the
        account and never touches a trade case. Establishing this order
        introduces no cycle, and every future caller must keep it.
        """
        paused = await session.scalar(
            select(AccountRow.paused).where(AccountRow.id == self.account_id).with_for_update()
        )
        if paused is None:
            raise SystemPauseUnavailable("PAUSE_STATE_UNAVAILABLE")
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
    # The authoritative evaluator, reused read-only. Never a second engine.
    evaluator: TradeCaseEvaluator = field(default_factory=TradeCaseEvaluator)
    # The deployment's configured trading mode. OBSERVE and PAPER only; live is
    # not representable, and the contract refuses it.
    trading_mode: Literal["OBSERVE", "PAPER"] = "PAPER"
    # A COMMANDER-local stop. Deliberately *not* described as the switch SENTINEL
    # honours: `RiskLimits.kill_switch` is a separate field this one is not wired
    # to, and claiming otherwise would promise a global stop that does not exist.
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

        # The stored status is a snapshot of the last write, and time moves
        # without writes: a case whose authorization or setup aged out keeps a
        # stale row until something touches it. So the status is recomputed here
        # from current state — by the *same* evaluator the workflow persists
        # from, called read-only. That is deliberately reuse rather than a
        # second engine: there is one requirement table, one transition matrix
        # and one freshness rule, and this reads them at the present instant
        # instead of trusting a row written at some earlier one.
        binding_row = await self._binding_row(trade_case_id)
        effective = self.evaluator.evaluate(
            trade_case,
            evidence,
            risk_from_row(binding_row) if binding_row is not None else None,
            now,
        )
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
        shelf_life = _shelf_life(trade_case, current, binding_row, now)
        risk = None if binding_row is None else _risk_state(risk_from_row(binding_row), digest, now)
        controls = await self._controls()
        advisory = _advisory(current, now)

        return CommanderContext(
            trade_case_id=trade_case_id,
            task_id=task_id,
            workflow_version=trade_case.workflow_version,
            policy_version=self.policy.version,
            status=effective.status,
            revision=trade_case.revision,
            reason_code=effective.reason_code,
            blocker_codes=tuple(item.code for item in effective.blockers)[:24],
            evidence=tuple(states),
            tasks=tasks,
            risk=risk,
            advisory=advisory,
            controls=controls,
            current_risk_input_digest=digest,
            observed_at=now,
            valid_until=shelf_life,
            context_digest=context_digest(
                trade_case_id=trade_case_id,
                workflow_version=trade_case.workflow_version,
                policy_version=self.policy.version,
                status=effective.status.value,
                revision=trade_case.revision,
                evidence=tuple(states),
                tasks=tasks,
                risk=risk,
                controls=controls,
                current_risk_input_digest=digest,
            ),
        )

    async def _binding_row(self, trade_case_id: UUID) -> TradeCaseRiskBindingRow | None:
        """The newest binding, selected exactly as the workflow's own read does."""
        async with self.sessions() as session:
            row: TradeCaseRiskBindingRow | None = await session.scalar(
                select(TradeCaseRiskBindingRow)
                .where(TradeCaseRiskBindingRow.trade_case_id == trade_case_id)
                .order_by(
                    TradeCaseRiskBindingRow.case_revision.desc(),
                    TradeCaseRiskBindingRow.recorded_at.desc(),
                    TradeCaseRiskBindingRow.binding_id.desc(),
                )
                .limit(1)
            )
        return row

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
        # Fails closed on every uncertainty. A missing port, a missing account
        # row or a control that cannot be read are all *unknown*, and unknown is
        # not permission. The previous implementation returned False for all
        # three, which turned an unavailable stop into a green light.
        if self.pause is None:
            paused = True
        else:
            try:
                paused = await self.pause.system_paused()
            except SystemPauseUnavailable:
                paused = True
        return SystemControls(
            kill_switch=self.kill_switch,
            account_paused=paused,
            # The configured mode, not a constant. Hard-coding PAPER made the
            # field describe an intention rather than the deployment.
            trading_mode=self.trading_mode,
        )


def _shelf_life(
    trade_case: TradeCase,
    current: dict[EvidenceType, EvidenceEnvelope],
    binding_row: TradeCaseRiskBindingRow | None,
    now: datetime,
) -> datetime | None:
    """The first instant at which this view could stop describing the case.

    The earliest expiry that is still ahead. A view is only as current as the
    soonest thing in it left to lapse, and the alternative — no shelf life — is
    what let a frozen reading claim a validity it had lost.

    Expiries already past are deliberately excluded. They have already changed
    what the case means, and that change is exactly what this view records: an
    authorization that lapsed before the read arrives as `expired`, and the
    recomputed status reflects it. Counting it again would make every fresh
    reading of a case with any lapsed fact instantly unusable.

    `None` means nothing it rests on expires any later than the read, so no
    later instant can invalidate it.

    The setup's own horizon is read alongside the envelope's. That is a temporal
    fact rather than a finding: the control plane still forms no view about what
    the setup says, only about when it stops saying it.
    """
    horizons = [trade_case.expires_at] if trade_case.expires_at is not None else []
    # Canonical evidence only. An advisory synthesis must not shorten the window
    # in which a decision may be made: it is authoritative over nothing, and
    # letting its envelope age the view would hand the advisory layer a way to
    # force re-derivation of decisions it has no say in. Its own freshness is
    # reported separately and inertly, on `advisory`.
    horizons.extend(
        item.valid_until for kind, item in current.items() if kind is not EvidenceType.SYNTHESIS
    )
    if binding_row is not None:
        horizons.append(aware(binding_row.expires_at))
    setup = current.get(EvidenceType.TRADE_SETUP)
    if setup is not None and isinstance(setup.payload, TradeSetupPayload):
        detail = setup.payload.setup
        if detail is not None:
            horizons.append(detail.expires_at)
    ahead = [horizon for horizon in horizons if horizon > now]
    return min(ahead) if ahead else None


def _risk_state(binding: RiskBinding, digest: str, now: datetime) -> RiskState:
    """An authorization, what it covers, and whether it still holds."""
    return RiskState(
        binding_id=binding.binding_id,
        risk_decision_id=binding.risk_decision_id,
        authorization=binding.authorization,
        risk_input_digest=binding.risk_input_digest,
        # Two independent questions, and the first implementation asked only
        # one. Identity: was it granted against the evidence this case currently
        # has? One granted against a different set is not weaker, it is about a
        # case that no longer exists. Time: has it aged out? A decision issued
        # with a two-minute life is not an authorization eight minutes later,
        # however unchanged the evidence is.
        matches_current_inputs=binding.risk_input_digest == digest,
        expired=now >= binding.expires_at,
        expires_at=binding.expires_at,
    )


def _requirement(policy: WorkflowPolicy, evidence_type: EvidenceType) -> tuple[bool, bool]:
    for item in policy.requirements:
        if item.evidence_type == evidence_type:
            return item.required, item.safety_critical
    return False, False


def _advisory(
    current: dict[EvidenceType, EvidenceEnvelope], now: datetime
) -> AdvisorySynthesis | None:
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
        # Two ways to stop applying, and checking only the first was the defect.
        # References stay intact while a reading ages out — and because a
        # synthesis expires with the earliest of its sources *and* the setup it
        # describes, an expired one can outlive an unchanged evidence set.
        expired=item.effective_status(now) != EvidenceStatus.AVAILABLE,
        valid_until=item.valid_until,
    )
