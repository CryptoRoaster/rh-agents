"""Read-only TradeCase observability; commands remain internal capabilities."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.data.database import connect
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    SpecialistTask,
    TimelineEvent,
    TradeCase,
    TradeCaseStatus,
    WorkflowErrorCode,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService

router = APIRouter(prefix="/api/trade-cases")


async def trade_case_service(request: Request) -> AsyncIterator[TradeCaseService]:
    engine, sessions = connect(request.app.state.settings.database_url)
    try:
        yield TradeCaseService(sessions)
    finally:
        await engine.dispose()


Service = Annotated[TradeCaseService, Depends(trade_case_service)]


def not_found(error: WorkflowFailure) -> HTTPException:
    if error.code == WorkflowErrorCode.NOT_FOUND:
        return HTTPException(404, "TradeCase not found")
    return HTTPException(503, "TradeCase data unavailable")


@router.get("")
async def list_trade_cases(
    service: Service,
    status: TradeCaseStatus | None = None,
    chain: Literal["robinhood", "bsc"] | None = None,
    market: Annotated[str | None, Query(max_length=1200)] = None,
    updated_since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> tuple[TradeCase, ...]:
    try:
        return await service.list_trade_cases(
            status=status,
            chain=chain,
            market=market,
            updated_since=updated_since,
            limit=limit,
        )
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "TradeCase data unavailable") from None


@router.get("/{trade_case_id}")
async def trade_case(trade_case_id: UUID, service: Service) -> TradeCase:
    try:
        return await service.get_trade_case(trade_case_id)
    except WorkflowFailure as error:
        raise not_found(error) from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "TradeCase data unavailable") from None


@router.get("/{trade_case_id}/timeline")
async def timeline(trade_case_id: UUID, service: Service) -> tuple[TimelineEvent, ...]:
    try:
        return await service.timeline(trade_case_id)
    except WorkflowFailure as error:
        raise not_found(error) from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "TradeCase data unavailable") from None


@router.get("/{trade_case_id}/evidence")
async def evidence(trade_case_id: UUID, service: Service) -> tuple[EvidenceEnvelope, ...]:
    try:
        return await service.evidence(trade_case_id)
    except WorkflowFailure as error:
        raise not_found(error) from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "TradeCase data unavailable") from None


@router.get("/{trade_case_id}/tasks")
async def tasks(trade_case_id: UUID, service: Service) -> tuple[SpecialistTask, ...]:
    try:
        return await service.tasks(trade_case_id)
    except WorkflowFailure as error:
        raise not_found(error) from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "TradeCase data unavailable") from None
