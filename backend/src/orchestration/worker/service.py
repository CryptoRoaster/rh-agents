"""PostgreSQL-authoritative worker runtime: registration, leases, results, recovery.

Processing is AT-LEAST-ONCE. A worker may run the same logical task again after a
crash, a lost process, a lost network, an expired lease or an ambiguous
acknowledgement. Duplicate execution never produces duplicate authoritative
effects, because every effect is guarded by a database-enforced single active
lease plus durable idempotency. Nothing here promises exactly-once execution.

Lock ordering is always TradeCase first, then task, matching the Phase 2A command
services, so worker claims can never deadlock against workflow commands.
"""

from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import AgentRole
from src.data.repository import aware
from src.data.tables import (
    TradeCaseEventRow,
    TradeCaseRow,
    TradeCaseTaskRow,
    WorkerInstanceRow,
    WorkerTaskAttemptRow,
)
from src.orchestration.worker.models import (
    FAILURE_OUTCOMES,
    EvidenceTaskResult,
    TaskAttempt,
    TaskAttemptOutcome,
    TaskDisposition,
    TaskFailureReport,
    TaskLease,
    TaskWaitReport,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerInstance,
    WorkerInstanceStatus,
    WorkerRegistration,
)
from src.orchestration.worker.policy import (
    WORKER_RUNTIME_V1,
    WorkerRuntimePolicy,
    authorized_evidence_type,
    authorized_task_type,
)
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    EvidenceSubmission,
    SpecialistTaskStatus,
    TradeCaseStatus,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.policy import WaitPolicy
from src.orchestration.workflow.service import TradeCaseService

CLAIMABLE_TASK_STATUSES = (
    SpecialistTaskStatus.PENDING.value,
    SpecialistTaskStatus.BLOCKED.value,
)


def instance_from_row(row: WorkerInstanceRow) -> WorkerInstance:
    return WorkerInstance(
        worker_instance_id=row.worker_instance_id,
        role=AgentRole(row.role),
        runtime_version=row.runtime_version,
        status=WorkerInstanceStatus(row.status),
        started_at=aware(row.started_at),
        last_seen_at=aware(row.last_seen_at),
        registration_key=row.registration_key,
    )


def attempt_from_row(row: WorkerTaskAttemptRow) -> TaskAttempt:
    return TaskAttempt(
        attempt_id=row.attempt_id,
        task_id=row.task_id,
        trade_case_id=row.trade_case_id,
        role=AgentRole(row.role),
        worker_instance_id=row.worker_instance_id,
        lease_id=row.lease_id,
        attempt_number=row.attempt_number,
        started_at=aware(row.started_at),
        lease_expires_at=aware(row.lease_expires_at),
        finished_at=aware(row.finished_at) if row.finished_at is not None else None,
        outcome=TaskAttemptOutcome(row.outcome) if row.outcome is not None else None,
        reason_code=row.reason_code,
        failure_category=(
            WorkerFailureCategory(row.failure_category)
            if row.failure_category is not None
            else None
        ),
        runtime_version=row.runtime_version,
        correlation_id=row.correlation_id,
    )


def lease_from_rows(task: TradeCaseTaskRow, attempt: WorkerTaskAttemptRow) -> TaskLease:
    return TaskLease(
        lease_id=attempt.lease_id,
        task_id=task.task_id,
        trade_case_id=task.trade_case_id,
        role=AgentRole(task.role),
        task_type=task.task_type,
        worker_instance_id=attempt.worker_instance_id,
        attempt_number=attempt.attempt_number,
        lease_started_at=aware(attempt.started_at),
        lease_expires_at=aware(attempt.lease_expires_at),
        renewals=task.lease_renewals,
        correlation_id=task.correlation_id,
    )


class WorkerRuntimeService:
    """The only boundary through which a worker can affect authoritative state."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cases: TradeCaseService,
        *,
        clock: Clock | None = None,
        policy: WorkerRuntimePolicy = WORKER_RUNTIME_V1,
    ) -> None:
        self.sessions = sessions
        self.cases = cases
        self.clock = clock if clock is not None else SystemClock()
        self.policy = policy

    # ---------------------------------------------------------------- registration

    async def register_worker(self, registration: WorkerRegistration) -> WorkerInstance:
        registration = WorkerRegistration.model_validate_json(registration.model_dump_json())
        fingerprint = sha256(registration.model_dump_json().encode()).hexdigest()
        now = self.clock.now()
        try:
            async with self.sessions.begin() as session:
                existing = await session.scalar(
                    select(WorkerInstanceRow).where(
                        WorkerInstanceRow.registration_key == registration.registration_key
                    )
                )
                if existing is not None:
                    return self._verify_registration_replay(existing, fingerprint)
                row = WorkerInstanceRow(
                    worker_instance_id=uuid5(
                        NAMESPACE_URL, f"rh-agents:worker:{registration.registration_key}"
                    ),
                    role=registration.role.value,
                    runtime_version=registration.runtime_version,
                    status=WorkerInstanceStatus.ACTIVE.value,
                    started_at=now,
                    last_seen_at=now,
                    registration_key=registration.registration_key,
                    registration_fingerprint=fingerprint,
                )
                session.add(row)
                await session.flush()
                return instance_from_row(row)
        except IntegrityError:
            async with self.sessions() as session:
                existing = await session.scalar(
                    select(WorkerInstanceRow).where(
                        WorkerInstanceRow.registration_key == registration.registration_key
                    )
                )
                if existing is None:
                    raise
                return self._verify_registration_replay(existing, fingerprint)

    @staticmethod
    def _verify_registration_replay(row: WorkerInstanceRow, fingerprint: str) -> WorkerInstance:
        if row.registration_fingerprint != fingerprint:
            raise WorkerFailure(WorkerErrorCode.WORKER_IDENTITY_CONFLICT)
        return instance_from_row(row)

    async def set_worker_status(
        self, worker_instance_id: UUID, status: WorkerInstanceStatus
    ) -> WorkerInstance:
        """Drain or stop a runtime instance. Leases are never silently rewritten:
        a stopped worker simply stops claiming, and its lease expires naturally."""
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(WorkerInstanceRow)
                .where(WorkerInstanceRow.worker_instance_id == worker_instance_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise WorkerFailure(WorkerErrorCode.WORKER_NOT_FOUND)
            row.status = status.value
            row.last_seen_at = self.clock.now()
            await session.flush()
            return instance_from_row(row)

    async def _active_worker(
        self, session: AsyncSession, worker_instance_id: UUID
    ) -> WorkerInstanceRow:
        row = await session.scalar(
            select(WorkerInstanceRow).where(
                WorkerInstanceRow.worker_instance_id == worker_instance_id
            )
        )
        if row is None:
            raise WorkerFailure(WorkerErrorCode.WORKER_NOT_FOUND)
        if row.status != WorkerInstanceStatus.ACTIVE.value:
            raise WorkerFailure(WorkerErrorCode.WORKER_NOT_ACTIVE)
        return row

    # ---------------------------------------------------------------------- claim

    async def claim_next_task(self, worker_instance_id: UUID) -> TaskLease | None:
        """Atomically take exclusive, time-bounded authority over one task.

        Candidates are read without a lock, then each is confirmed under the
        canonical TradeCase-then-task lock order and revalidated. The case lock uses
        SKIP LOCKED so independent workers proceed in parallel while two claimers of
        the same case serialize. PostgreSQL is authoritative here; SQLite cannot
        reproduce SKIP LOCKED and those tests are skipped.
        """
        async with self.sessions.begin() as session:
            worker = await self._active_worker(session, worker_instance_id)
            role = AgentRole(worker.role)
            task_type = authorized_task_type(role)
            if task_type is None:
                # FUSE and COMMANDER have no evidence requirement, so they are not
                # claimable through the evidence-submission runtime in Phase 2B.
                raise WorkerFailure(WorkerErrorCode.ROLE_NOT_AUTHORIZED)
            now = self.clock.now()
            candidates = (
                await session.scalars(
                    select(TradeCaseTaskRow)
                    .where(
                        TradeCaseTaskRow.role == role.value,
                        TradeCaseTaskRow.task_type == task_type,
                        TradeCaseTaskRow.status.in_(CLAIMABLE_TASK_STATUSES),
                    )
                    .order_by(TradeCaseTaskRow.created_at, TradeCaseTaskRow.task_id)
                    .limit(self.policy.claim_batch)
                )
            ).all()
            for candidate in candidates:
                lease = await self._try_claim(session, candidate.task_id, worker, now)
                if lease is not None:
                    return lease
            return None

    async def _try_claim(
        self,
        session: AsyncSession,
        task_id: UUID,
        worker: WorkerInstanceRow,
        now: datetime,
    ) -> TaskLease | None:
        task_case = await session.scalar(
            select(TradeCaseTaskRow.trade_case_id).where(TradeCaseTaskRow.task_id == task_id)
        )
        if task_case is None:
            return None
        case_row = await session.scalar(
            select(TradeCaseRow)
            .where(TradeCaseRow.id == task_case)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if case_row is None:
            return None
        task = await session.scalar(
            select(TradeCaseTaskRow)
            .where(TradeCaseTaskRow.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or not self._claimable(task, case_row, now):
            return None
        attempt_number = task.attempt
        lease_id = uuid4()
        expires_at = now + self.policy.lease_duration
        session.add(
            WorkerTaskAttemptRow(
                attempt_id=uuid4(),
                trade_case_id=task.trade_case_id,
                task_id=task.task_id,
                role=task.role,
                worker_instance_id=worker.worker_instance_id,
                lease_id=lease_id,
                attempt_number=attempt_number,
                started_at=now,
                lease_expires_at=expires_at,
                reason_code="TASK_CLAIMED",
                runtime_version=worker.runtime_version,
                correlation_id=task.correlation_id,
            )
        )
        task.status = SpecialistTaskStatus.RUNNING.value
        task.started_at = task.started_at or now
        task.reason_code = "TASK_CLAIMED"
        task.lease_id = lease_id
        task.worker_instance_id = worker.worker_instance_id
        task.lease_started_at = now
        task.lease_expires_at = expires_at
        task.last_heartbeat_at = now
        task.lease_renewals = 0
        task.next_eligible_at = None
        task.failure_category = None
        worker.last_seen_at = now
        self._event(
            session,
            case_row,
            "TASK_CLAIMED",
            "TASK_CLAIMED",
            {
                "task_id": str(task.task_id),
                "role": task.role,
                "attempt_number": attempt_number,
                "worker_instance_id": str(worker.worker_instance_id),
            },
        )
        await session.flush()
        attempt = await session.scalar(
            select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == lease_id)
        )
        assert attempt is not None
        return lease_from_rows(task, attempt)

    def _claimable(self, task: TradeCaseTaskRow, case_row: TradeCaseRow, now: datetime) -> bool:
        if task.status not in CLAIMABLE_TASK_STATUSES:
            return False
        if task.lease_id is not None and task.lease_expires_at is not None:
            if now < aware(task.lease_expires_at):
                return False
        if task.next_eligible_at is not None and now < aware(task.next_eligible_at):
            return False
        if task.attempt > task.max_attempts:
            return False
        if task.expires_at is not None and now >= aware(task.expires_at):
            return False
        return self._case_workable(case_row, now)

    @staticmethod
    def _case_workable(case_row: TradeCaseRow, now: datetime) -> bool:
        if TradeCaseStatus(case_row.status) in TERMINAL_CASE_STATUSES:
            return False
        if case_row.expires_at is not None and now >= aware(case_row.expires_at):
            return False
        return True

    # ------------------------------------------------------------------ heartbeat

    async def renew_lease(self, lease: TaskLease) -> TaskLease:
        """Extend only the caller's own currently active lease, within a budget.

        Heartbeats update current task state and never write attempt history, which
        is why that history is genuinely immutable once finished.
        """
        async with self.sessions.begin() as session:
            now = self.clock.now()
            task = await session.scalar(
                select(TradeCaseTaskRow)
                .where(TradeCaseTaskRow.task_id == lease.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            attempt = await self._validate_lease(session, task, lease, now)
            assert task is not None
            if task.lease_renewals >= self.policy.max_lease_renewals:
                raise WorkerFailure(WorkerErrorCode.LEASE_RENEWAL_EXHAUSTED)
            case_row = await session.get(TradeCaseRow, task.trade_case_id)
            if case_row is None or not self._case_workable(case_row, now):
                raise WorkerFailure(WorkerErrorCode.TRADE_CASE_NOT_WORKABLE)
            expires_at = now + self.policy.lease_duration
            if task.expires_at is not None:
                # A hard task deadline caps renewal; leases cannot outlive the work.
                deadline = aware(task.expires_at)
                if now >= deadline:
                    raise WorkerFailure(WorkerErrorCode.LEASE_RENEWAL_EXHAUSTED)
                expires_at = min(expires_at, deadline)
            task.lease_expires_at = expires_at
            task.last_heartbeat_at = now
            task.lease_renewals += 1
            await session.flush()
            renewed = lease_from_rows(task, attempt)
            return renewed.model_copy(update={"lease_expires_at": aware(expires_at)})

    async def _validate_lease(
        self,
        session: AsyncSession,
        task: TradeCaseTaskRow | None,
        lease: TaskLease,
        now: datetime,
    ) -> WorkerTaskAttemptRow:
        if task is None:
            raise WorkerFailure(WorkerErrorCode.TASK_NOT_FOUND)
        if task.trade_case_id != lease.trade_case_id:
            raise WorkerFailure(WorkerErrorCode.TRADE_CASE_MISMATCH)
        attempt = await session.scalar(
            select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == lease.lease_id)
        )
        if attempt is None or attempt.task_id != lease.task_id:
            raise WorkerFailure(WorkerErrorCode.LEASE_NOT_FOUND)
        if attempt.worker_instance_id != lease.worker_instance_id:
            raise WorkerFailure(WorkerErrorCode.LEASE_OWNER_MISMATCH)
        # Ownership on the task aggregate is authoritative. A reclaimed task has
        # moved on, so the previous holder's token no longer grants anything.
        if task.lease_id != lease.lease_id:
            raise WorkerFailure(WorkerErrorCode.LEASE_EXPIRED)
        if task.worker_instance_id != lease.worker_instance_id:
            raise WorkerFailure(WorkerErrorCode.LEASE_OWNER_MISMATCH)
        if attempt.finished_at is not None:
            raise WorkerFailure(WorkerErrorCode.LEASE_EXPIRED)
        if task.status != SpecialistTaskStatus.RUNNING.value:
            raise WorkerFailure(WorkerErrorCode.TASK_NOT_CLAIMABLE)
        if task.lease_expires_at is None or now >= aware(task.lease_expires_at):
            raise WorkerFailure(WorkerErrorCode.LEASE_EXPIRED)
        return attempt

    def _event(
        self,
        session: AsyncSession,
        case_row: TradeCaseRow,
        event_type: str,
        reason_code: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            TradeCaseEventRow(
                event_id=uuid4(),
                trade_case_id=case_row.id,
                event_type=event_type,
                reason_code=reason_code,
                recorded_at=self.clock.now(),
                correlation_id=case_row.correlation_id,
                payload=payload,
            )
        )

    # ------------------------------------------------------------------- results

    async def submit_task_result(
        self, lease: TaskLease, result: EvidenceTaskResult
    ) -> TaskDisposition:
        """Record typed evidence and complete the task in one atomic transaction.

        Evidence and task completion commit together or not at all, so a crash can
        never leave a task marked successful without its evidence, nor evidence
        recorded against a task left RUNNING forever.
        """
        result = EvidenceTaskResult.model_validate_json(result.model_dump_json())
        async with self.sessions.begin() as session:
            now = self.clock.now()
            case_row = await self._locked_case(session, lease.trade_case_id)
            task = await session.scalar(
                select(TradeCaseTaskRow)
                .where(TradeCaseTaskRow.task_id == lease.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise WorkerFailure(WorkerErrorCode.TASK_NOT_FOUND)
            attempt = await session.scalar(
                select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == lease.lease_id)
            )
            if attempt is None or attempt.task_id != lease.task_id:
                raise WorkerFailure(WorkerErrorCode.LEASE_NOT_FOUND)
            submission = self._bind_submission(task, attempt, result)
            replay = await self._replayed(session, task, attempt, submission)
            if replay is not None:
                return replay
            await self._validate_lease(session, task, lease, now)
            self._authorize_submission(task, submission)
            await self._require_current_references(session, task, submission)
            if not self._case_workable(case_row, now):
                raise WorkerFailure(WorkerErrorCode.TRADE_CASE_NOT_WORKABLE)
            try:
                # Reuses the Phase 2A boundary inside this transaction rather than
                # restating any workflow rule, so evaluation and blockers stay in
                # exactly one place.
                await self.cases.record_evidence_in_session(session, task.trade_case_id, submission)
            except WorkflowFailure as error:
                raise self._translate(error) from None
            attempt.finished_at = now
            attempt.outcome = TaskAttemptOutcome.SUCCEEDED.value
            attempt.reason_code = "RESULT_ACCEPTED"
            task.failure_category = None
            task.next_eligible_at = None
            self._event(
                session,
                case_row,
                "TASK_RESULT_ACCEPTED",
                "RESULT_ACCEPTED",
                {
                    "task_id": str(task.task_id),
                    "role": task.role,
                    "attempt_number": attempt.attempt_number,
                    "worker_instance_id": str(attempt.worker_instance_id),
                    "evidence_type": submission.evidence_type.value,
                },
            )
            await session.flush()
            return TaskDisposition(
                task_id=task.task_id,
                trade_case_id=task.trade_case_id,
                attempt_number=attempt.attempt_number,
                outcome=TaskAttemptOutcome.SUCCEEDED,
                reason_code="RESULT_ACCEPTED",
                retry_scheduled=False,
            )

    async def report_task_wait(self, lease: TaskLease, report: TaskWaitReport) -> TaskDisposition:
        """Record that an attempt did its work and the world is not yet ready.

        Structurally this is the retry path without the failure: the lease is
        released, the attempt closes, and the task returns to PENDING with a
        future eligibility time. What differs is what the record says. The
        attempt carries WAITING and no failure category, so an operator reading
        task history sees a monitor doing its job rather than a run of incidents,
        and nothing downstream can mistake ordinary patience for an error.

        **The schedule is the runtime's.** A worker reports a fact and this
        derives the next eligibility time from the task's own server-side policy,
        because a worker able to name its own cadence could postpone a task
        indefinitely or poll a provider at will. Only tasks whose policy declares
        a wait may wait at all, and only for the reasons it allows: the rest of
        the roles compute an answer once and have nothing to be patient about.
        """
        report = TaskWaitReport.model_validate_json(report.model_dump_json())
        async with self.sessions.begin() as session:
            now = self.clock.now()
            case_row = await self._locked_case(session, lease.trade_case_id)
            task = await session.scalar(
                select(TradeCaseTaskRow)
                .where(TradeCaseTaskRow.task_id == lease.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise WorkerFailure(WorkerErrorCode.TASK_NOT_FOUND)
            definition = self.cases.policy.task(AgentRole(task.role), task.task_type)
            policy = None if definition is None else definition.wait
            if policy is None:
                # A one-shot specialist has no business postponing itself.
                raise WorkerFailure(WorkerErrorCode.WAIT_NOT_PERMITTED)
            if report.reason_code not in policy.reasons:
                # A worker cannot invent a category to wait under.
                raise WorkerFailure(WorkerErrorCode.WAIT_REASON_NOT_PERMITTED)
            attempt = await session.scalar(
                select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == lease.lease_id)
            )
            if attempt is None or attempt.task_id != lease.task_id:
                raise WorkerFailure(WorkerErrorCode.LEASE_NOT_FOUND)
            if attempt.finished_at is not None:
                # The attempt already reached a durable outcome. Waiting is not a
                # second answer to a question that has been answered.
                raise WorkerFailure(WorkerErrorCode.RESULT_CONFLICT)
            # Fenced exactly like every other authoritative write: a worker whose
            # lease has gone has no standing to reschedule anything.
            await self._validate_lease(session, task, lease, now)

            attempt.finished_at = now
            attempt.outcome = TaskAttemptOutcome.WAITING.value
            attempt.reason_code = report.reason_code
            attempt.failure_category = None
            task.lease_id = None
            task.worker_instance_id = None
            task.lease_started_at = None
            task.lease_expires_at = None
            task.lease_renewals = 0
            task.failure_category = None
            task.attempt += 1

            # Waiting spends its own budget. Failures have theirs, so a monitor
            # that rechecked two hundred times still has a full allowance for
            # things actually going wrong, and a handful of provider outages
            # cannot consume the watch.
            waits = await self._count_outcomes(
                session, task.task_id, frozenset({TaskAttemptOutcome.WAITING})
            )
            ends = report.reason_code in policy.terminal_reasons
            exhausted = waits >= policy.max_waits
            next_eligible_at = self._next_wait(now, policy, report, task)
            if ends or exhausted or next_eligible_at is None:
                # A watch is bounded like everything else. Running out of checks,
                # or reaching the end of what is worth watching, is not a failure
                # of the market to cooperate; it is this system declining to
                # watch on.
                reason = "TASK_WATCH_EXHAUSTED" if exhausted and not ends else "TASK_WATCH_ENDED"
                task.status = SpecialistTaskStatus.FAILED.value
                task.completed_at = now
                task.next_eligible_at = None
                task.reason_code = reason
                self._event(
                    session,
                    case_row,
                    reason,
                    reason,
                    {
                        "task_id": str(task.task_id),
                        "role": task.role,
                        "attempt_number": attempt.attempt_number,
                        "waits": waits,
                    },
                )
                return TaskDisposition(
                    task_id=task.task_id,
                    trade_case_id=task.trade_case_id,
                    attempt_number=attempt.attempt_number,
                    outcome=TaskAttemptOutcome.WAITING,
                    reason_code=reason,
                    retry_scheduled=False,
                )
            task.status = SpecialistTaskStatus.PENDING.value
            task.reason_code = report.reason_code
            task.next_eligible_at = next_eligible_at
            self._event(
                session,
                case_row,
                "TASK_WAITING",
                report.reason_code,
                {
                    "task_id": str(task.task_id),
                    "role": task.role,
                    "attempt_number": attempt.attempt_number,
                    "next_eligible_at": next_eligible_at.isoformat(),
                    "waits": waits,
                },
            )
            return TaskDisposition(
                task_id=task.task_id,
                trade_case_id=task.trade_case_id,
                attempt_number=attempt.attempt_number,
                outcome=TaskAttemptOutcome.WAITING,
                reason_code=report.reason_code,
                retry_scheduled=True,
                next_eligible_at=next_eligible_at,
            )

    @staticmethod
    def _next_wait(
        now: datetime,
        policy: WaitPolicy,
        report: TaskWaitReport,
        task: TradeCaseTaskRow,
    ) -> datetime | None:
        """When to look again, or None when there is no point looking again.

        The interval is policy's. ``not_after`` and the task's own expiry can
        only bring the moment forward, never push it out — a worker may say when
        the thing it watches stops being watchable, and may not say when it would
        prefer to be asked.
        """
        scheduled = now + policy.interval
        for bound in (report.not_after, aware(task.expires_at) if task.expires_at else None):
            if bound is not None and bound < scheduled:
                scheduled = bound
        return None if scheduled <= now else scheduled

    @staticmethod
    async def _count_outcomes(
        session: AsyncSession, task_id: UUID, outcomes: frozenset[TaskAttemptOutcome]
    ) -> int:
        """How many finished attempts of this task ended each way.

        Read from immutable attempt history rather than kept in a counter column,
        so waits and failures are counted separately without a schema change and
        without either budget being able to drift from what actually happened.
        """
        total = await session.scalar(
            select(func.count())
            .select_from(WorkerTaskAttemptRow)
            .where(
                WorkerTaskAttemptRow.task_id == task_id,
                WorkerTaskAttemptRow.outcome.in_([item.value for item in outcomes]),
            )
        )
        return int(total or 0)

    async def report_task_failure(
        self, lease: TaskLease, report: TaskFailureReport
    ) -> TaskDisposition:
        report = TaskFailureReport.model_validate_json(report.model_dump_json())
        async with self.sessions.begin() as session:
            now = self.clock.now()
            case_row = await self._locked_case(session, lease.trade_case_id)
            task = await session.scalar(
                select(TradeCaseTaskRow)
                .where(TradeCaseTaskRow.task_id == lease.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise WorkerFailure(WorkerErrorCode.TASK_NOT_FOUND)
            attempt = await session.scalar(
                select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == lease.lease_id)
            )
            if attempt is None or attempt.task_id != lease.task_id:
                raise WorkerFailure(WorkerErrorCode.LEASE_NOT_FOUND)
            if attempt.finished_at is not None:
                return self._finished_disposition(task, attempt)
            await self._validate_lease(session, task, lease, now)
            return await self._apply_failure(
                session, case_row, task, attempt, report.category, report.reason_code, now
            )

    async def _apply_failure(
        self,
        session: AsyncSession,
        case_row: TradeCaseRow,
        task: TradeCaseTaskRow,
        attempt: WorkerTaskAttemptRow,
        category: WorkerFailureCategory,
        reason_code: str,
        now: datetime,
        recorded_outcome: TaskAttemptOutcome | None = None,
    ) -> TaskDisposition:
        """Single deterministic place where a failed attempt becomes a task decision.

        ``recorded_outcome`` names what the attempt history should say when that
        differs from what the task decision is. Lease recovery is the only caller
        that needs it: the task is retried exactly as a transient failure, while
        the attempt truthfully records that its lease expired. It is written here
        rather than afterwards because an attempt is immutable once finished, and
        the database enforces that.
        """
        if category == WorkerFailureCategory.TASK_INVALIDATED:
            outcome = TaskAttemptOutcome.SUPERSEDED
        elif self.policy.is_retryable(category):
            outcome = TaskAttemptOutcome.FAILED_RETRYABLE
        else:
            outcome = TaskAttemptOutcome.FAILED_PERMANENT
        attempt.finished_at = now
        attempt.outcome = (recorded_outcome or outcome).value
        attempt.reason_code = reason_code
        attempt.failure_category = category.value
        # The lease is released in every failure path; a new attempt needs a new one.
        task.lease_id = None
        task.worker_instance_id = None
        task.lease_started_at = None
        task.lease_expires_at = None
        task.lease_renewals = 0
        task.failure_category = category.value
        retryable = outcome != TaskAttemptOutcome.FAILED_PERMANENT
        # Counted from history rather than from the claim counter, so a monitor's
        # ordinary waiting never spends the budget meant for things going wrong.
        # For every one-shot role there are no waits and the count is identical
        # to what the claim counter said.
        failures = await self._count_outcomes(session, task.task_id, FAILURE_OUTCOMES)
        exhausted = failures >= task.max_attempts
        if retryable and not exhausted:
            delay = self.policy.retry_delay(task.attempt)
            task.attempt += 1
            task.status = SpecialistTaskStatus.PENDING.value
            task.reason_code = reason_code
            task.next_eligible_at = now + delay
            event_type = "TASK_FAILED_RETRYABLE"
            disposition_reason = reason_code
        else:
            task.status = SpecialistTaskStatus.FAILED.value
            task.completed_at = now
            task.next_eligible_at = None
            disposition_reason = (
                "TASK_RETRY_EXHAUSTED" if retryable and exhausted else "TASK_FAILED_PERMANENT"
            )
            task.reason_code = disposition_reason
            event_type = (
                "TASK_RETRY_EXHAUSTED" if retryable and exhausted else "TASK_FAILED_PERMANENT"
            )
        self._event(
            session,
            case_row,
            event_type,
            disposition_reason,
            {
                "task_id": str(task.task_id),
                "role": task.role,
                "attempt_number": attempt.attempt_number,
                "failure_category": category.value,
            },
        )
        return TaskDisposition(
            task_id=task.task_id,
            trade_case_id=task.trade_case_id,
            attempt_number=attempt.attempt_number,
            outcome=outcome,
            reason_code=disposition_reason,
            retry_scheduled=retryable and not exhausted,
            next_eligible_at=(
                aware(task.next_eligible_at) if task.next_eligible_at is not None else None
            ),
        )

    # ------------------------------------------------------------------ recovery

    async def recover_expired_leases(self, *, limit: int = 20) -> tuple[TaskDisposition, ...]:
        """Reclaim work abandoned by lost workers. Safe to run from many processes.

        No always-running background thread is required; a runtime calls this in
        bounded sweeps.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Recovery limit must be between 1 and 100")
        recovered: list[TaskDisposition] = []
        async with self.sessions.begin() as session:
            now = self.clock.now()
            candidates = (
                await session.scalars(
                    select(TradeCaseTaskRow.task_id)
                    .where(
                        TradeCaseTaskRow.status == SpecialistTaskStatus.RUNNING.value,
                        TradeCaseTaskRow.lease_expires_at.is_not(None),
                        TradeCaseTaskRow.lease_expires_at <= now,
                    )
                    .order_by(TradeCaseTaskRow.lease_expires_at, TradeCaseTaskRow.task_id)
                    .limit(limit)
                )
            ).all()
            for task_id in candidates:
                disposition = await self._recover_one(session, task_id, now)
                if disposition is not None:
                    recovered.append(disposition)
        return tuple(recovered)

    async def _recover_one(
        self, session: AsyncSession, task_id: UUID, now: datetime
    ) -> TaskDisposition | None:
        task_case = await session.scalar(
            select(TradeCaseTaskRow.trade_case_id).where(TradeCaseTaskRow.task_id == task_id)
        )
        if task_case is None:
            return None
        case_row = await session.scalar(
            select(TradeCaseRow)
            .where(TradeCaseRow.id == task_case)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if case_row is None:
            return None
        task = await session.scalar(
            select(TradeCaseTaskRow)
            .where(TradeCaseTaskRow.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != SpecialistTaskStatus.RUNNING.value:
            return None
        if task.lease_expires_at is None or now < aware(task.lease_expires_at):
            # A heartbeat won the race; the lease is alive and must not be taken.
            return None
        attempt = await session.scalar(
            select(WorkerTaskAttemptRow).where(WorkerTaskAttemptRow.lease_id == task.lease_id)
        )
        if attempt is None or attempt.finished_at is not None:
            return None
        disposition = await self._apply_failure(
            session,
            case_row,
            task,
            attempt,
            WorkerFailureCategory.TRANSIENT,
            "LEASE_EXPIRED",
            now,
            recorded_outcome=TaskAttemptOutcome.LEASE_EXPIRED,
        )
        self._event(
            session,
            case_row,
            "LEASE_EXPIRED",
            "LEASE_EXPIRED",
            {"task_id": str(task.task_id), "attempt_number": attempt.attempt_number},
        )
        await session.flush()
        return disposition.model_copy(update={"outcome": TaskAttemptOutcome.LEASE_EXPIRED})

    # ------------------------------------------------------------- authorization

    @staticmethod
    async def _locked_case(session: AsyncSession, trade_case_id: UUID) -> TradeCaseRow:
        row = await session.scalar(
            select(TradeCaseRow)
            .where(TradeCaseRow.id == trade_case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise WorkerFailure(WorkerErrorCode.TASK_NOT_FOUND)
        return row

    @staticmethod
    def _derived_key(task: TradeCaseTaskRow, attempt_number: int, result_key: str) -> str:
        # The runtime owns the evidence idempotency identity. A worker supplies only
        # its own result key, so it cannot address another task's evidence slot.
        return f"worker:{task.task_id}:{attempt_number}:{result_key}"

    def _bind_submission(
        self,
        task: TradeCaseTaskRow,
        attempt: WorkerTaskAttemptRow,
        result: EvidenceTaskResult,
    ) -> EvidenceSubmission:
        bound: EvidenceSubmission = result.submission.model_copy(
            update={
                "idempotency_key": self._derived_key(
                    task, attempt.attempt_number, result.result_key
                )
            }
        )
        return bound

    @staticmethod
    def _authorize_submission(task: TradeCaseTaskRow, submission: EvidenceSubmission) -> None:
        """Server-side capability check, independent of which Python object the
        worker was handed."""
        role = AgentRole(task.role)
        if submission.producer_role != role:
            raise WorkerFailure(WorkerErrorCode.ROLE_NOT_AUTHORIZED)
        permitted = authorized_evidence_type(role)
        if permitted is None or submission.evidence_type != permitted:
            raise WorkerFailure(WorkerErrorCode.EVIDENCE_TYPE_NOT_AUTHORIZED)

    async def _require_current_references(
        self, session: AsyncSession, task: TradeCaseTaskRow, submission: EvidenceSubmission
    ) -> None:
        """Reject work built on evidence that has since been superseded.

        The evaluator would also refuse to act on a stale setup later, but the
        authorization layer refuses to record it at all, so a superseded trigger
        never enters history.
        """
        singular = [
            getattr(submission.payload, name, None)
            for name in ("setup_evidence_id", "trigger_evidence_id")
        ]
        # A synthesis names every envelope it read, so all of them are checked.
        # Without this a FUSE reading of setup A could be recorded after VECTOR
        # replaced it with B, and the record would show a current-looking
        # synthesis of a setup nobody is trading any more.
        plural = list(getattr(submission.payload, "source_evidence_ids", ()) or ())
        references = [value for value in [*singular, *plural] if value is not None]
        if not references:
            return
        evidence = await self.cases.evidence(task.trade_case_id)
        superseded = {item.supersedes_id for item in evidence if item.supersedes_id is not None}
        for value in references:
            if value in superseded:
                raise WorkerFailure(WorkerErrorCode.TASK_SUPERSEDED)

        # Identity is not freshness, and a derived result needs both.
        #
        # The references above can all still be current while the work was slow
        # enough that one of them aged out during it — no supersession happened,
        # so nothing above notices, and the result would be recorded claiming a
        # currency its inputs no longer have. A submission that has already
        # expired by the time it arrives is therefore refused rather than stored
        # and left for a reader to catch.
        if submission.valid_until <= self.clock.now():
            raise WorkerFailure(WorkerErrorCode.EVIDENCE_STALE)

    async def _replayed(
        self,
        session: AsyncSession,
        task: TradeCaseTaskRow,
        attempt: WorkerTaskAttemptRow,
        submission: EvidenceSubmission,
    ) -> TaskDisposition | None:
        """Answer a duplicate submission from durable state, not in-memory dedupe."""
        if attempt.finished_at is None:
            return None
        if attempt.outcome != TaskAttemptOutcome.SUCCEEDED.value:
            # The attempt already ended some other way, so this worker is stale.
            raise WorkerFailure(WorkerErrorCode.LEASE_EXPIRED)
        stored = await self.cases.evidence(task.trade_case_id)
        match = next(
            (item for item in stored if item.idempotency_key == submission.idempotency_key), None
        )
        if match is None:
            raise WorkerFailure(WorkerErrorCode.LEASE_EXPIRED)
        if match.submission_fingerprint != submission.fingerprint():
            raise WorkerFailure(WorkerErrorCode.RESULT_CONFLICT)
        return TaskDisposition(
            task_id=task.task_id,
            trade_case_id=task.trade_case_id,
            attempt_number=attempt.attempt_number,
            outcome=TaskAttemptOutcome.SUCCEEDED,
            reason_code="RESULT_REPLAYED",
            retry_scheduled=False,
            replayed=True,
        )

    @staticmethod
    def _finished_disposition(
        task: TradeCaseTaskRow, attempt: WorkerTaskAttemptRow
    ) -> TaskDisposition:
        assert attempt.outcome is not None
        return TaskDisposition(
            task_id=task.task_id,
            trade_case_id=task.trade_case_id,
            attempt_number=attempt.attempt_number,
            outcome=TaskAttemptOutcome(attempt.outcome),
            reason_code=attempt.reason_code,
            retry_scheduled=False,
            replayed=True,
        )

    @staticmethod
    def _translate(error: WorkflowFailure) -> WorkerFailure:
        """Map workflow refusals onto the worker error vocabulary without leaking
        internal detail back to a worker."""
        mapping = {
            WorkflowErrorCode.IDEMPOTENCY_CONFLICT: WorkerErrorCode.RESULT_CONFLICT,
            WorkflowErrorCode.EVIDENCE_BINDING: WorkerErrorCode.EVIDENCE_TYPE_NOT_AUTHORIZED,
            WorkflowErrorCode.EVIDENCE_SUPERSESSION: WorkerErrorCode.TASK_SUPERSEDED,
            WorkflowErrorCode.EVIDENCE_INTEGRITY: WorkerErrorCode.TASK_SUPERSEDED,
            WorkflowErrorCode.TERMINAL_CASE: WorkerErrorCode.TRADE_CASE_NOT_WORKABLE,
            WorkflowErrorCode.NOT_FOUND: WorkerErrorCode.TASK_NOT_FOUND,
        }
        return WorkerFailure(mapping.get(error.code, WorkerErrorCode.RESULT_CONFLICT))

    # ---------------------------------------------------------------------- reads

    async def workers(
        self, *, role: AgentRole | None = None, limit: int = 100
    ) -> tuple[WorkerInstance, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Worker list limit must be between 1 and 100")
        statement = select(WorkerInstanceRow)
        if role is not None:
            statement = statement.where(WorkerInstanceRow.role == role.value)
        statement = statement.order_by(
            WorkerInstanceRow.started_at.desc(), WorkerInstanceRow.worker_instance_id.desc()
        ).limit(limit)
        async with self.sessions() as session:
            return tuple(instance_from_row(row) for row in (await session.scalars(statement)).all())

    async def worker(self, worker_instance_id: UUID) -> WorkerInstance:
        async with self.sessions() as session:
            row = await session.get(WorkerInstanceRow, worker_instance_id)
            if row is None:
                raise WorkerFailure(WorkerErrorCode.WORKER_NOT_FOUND)
            return instance_from_row(row)

    async def attempts(
        self,
        *,
        trade_case_id: UUID | None = None,
        task_id: UUID | None = None,
        worker_instance_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[TaskAttempt, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Attempt list limit must be between 1 and 100")
        statement = select(WorkerTaskAttemptRow)
        for column, value in (
            (WorkerTaskAttemptRow.trade_case_id, trade_case_id),
            (WorkerTaskAttemptRow.task_id, task_id),
            (WorkerTaskAttemptRow.worker_instance_id, worker_instance_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        statement = statement.order_by(
            WorkerTaskAttemptRow.started_at.desc(), WorkerTaskAttemptRow.attempt_id.desc()
        ).limit(limit)
        async with self.sessions() as session:
            return tuple(attempt_from_row(row) for row in (await session.scalars(statement)).all())
