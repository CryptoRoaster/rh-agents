"""Read-only worker runtime observability.

There is deliberately no public claim, heartbeat, complete or fail route. The
authentication and identity model for external worker processes has not been
designed, so every mutation stays an internal application capability.
"""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.core.models import AgentRole
from src.data.database import connect
from src.orchestration.worker.models import (
    TaskAttempt,
    WorkerErrorCode,
    WorkerFailure,
    WorkerInstance,
)
from src.orchestration.worker.policy import WORKER_RUNTIME_V1, role_evidence_matrix
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.service import TradeCaseService

router = APIRouter(prefix="/api")


async def worker_service(request: Request) -> AsyncIterator[WorkerRuntimeService]:
    engine, sessions = connect(request.app.state.settings.database_url)
    try:
        yield WorkerRuntimeService(sessions, TradeCaseService(sessions))
    finally:
        await engine.dispose()


Service = Annotated[WorkerRuntimeService, Depends(worker_service)]


def unavailable(error: WorkerFailure) -> HTTPException:
    if error.code == WorkerErrorCode.WORKER_NOT_FOUND:
        return HTTPException(404, "Worker instance not found")
    return HTTPException(503, "Worker runtime data unavailable")


@router.get("/worker-runtime")
async def worker_runtime(request: Request) -> dict[str, object]:
    """Static runtime posture. Reports configuration, never a fake agent status."""
    settings = request.app.state.settings
    return {
        "policy_version": WORKER_RUNTIME_V1.version,
        "enabled": settings.worker_runtime_enabled,
        "lease_seconds": int(WORKER_RUNTIME_V1.lease_duration.total_seconds()),
        "max_attempts": WORKER_RUNTIME_V1.max_attempts,
        "max_lease_renewals": WORKER_RUNTIME_V1.max_lease_renewals,
        "reasoning_workers_implemented": False,
        "role_evidence": {
            role.value: evidence.value for role, evidence in role_evidence_matrix().items()
        },
    }


@router.get("/workers")
async def workers(
    service: Service,
    role: AgentRole | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> tuple[WorkerInstance, ...]:
    try:
        return await service.workers(role=role, limit=limit)
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "Worker runtime data unavailable") from None


@router.get("/workers/{worker_instance_id}")
async def worker(worker_instance_id: UUID, service: Service) -> WorkerInstance:
    try:
        return await service.worker(worker_instance_id)
    except WorkerFailure as error:
        raise unavailable(error) from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "Worker runtime data unavailable") from None


@router.get("/worker-attempts")
async def worker_attempts(
    service: Service,
    trade_case_id: UUID | None = None,
    task_id: UUID | None = None,
    worker_instance_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> tuple[TaskAttempt, ...]:
    try:
        return await service.attempts(
            trade_case_id=trade_case_id,
            task_id=task_id,
            worker_instance_id=worker_instance_id,
            limit=limit,
        )
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "Worker runtime data unavailable") from None
