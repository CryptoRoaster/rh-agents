"""Capability-restricted worker runtime. No model provider, signer or executor.

Phase 2B provides execution control for specialist workers, not strategy
authority. Workers can produce typed evidence only within their role-scoped
capabilities. TradeCase transitions remain deterministic, SENTINEL remains
non-overridable, and no worker can execute trades.
"""

from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskAttemptOutcome,
    TaskDisposition,
    TaskFailureReport,
    TaskLease,
    WorkerErrorCode,
    WorkerFailure,
    WorkerFailureCategory,
    WorkerInstance,
    WorkerInstanceStatus,
    WorkerRegistration,
)
from src.orchestration.worker.runner import CapabilityProvider, WorkerHandler, WorkerRunner
from src.orchestration.worker.service import WorkerRuntimeService

__all__ = [
    "CapabilityProvider",
    "EvidenceTaskResult",
    "TaskAttemptOutcome",
    "TaskDisposition",
    "TaskFailureReport",
    "TaskLease",
    "WorkerErrorCode",
    "WorkerFailure",
    "WorkerFailureCategory",
    "WorkerHandler",
    "WorkerInstance",
    "WorkerInstanceStatus",
    "WorkerRegistration",
    "WorkerRunner",
    "WorkerRuntimeService",
]
