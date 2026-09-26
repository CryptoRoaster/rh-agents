"""Read-only early-discovery scout views for the cockpit. GET only.

No route here writes, starts a scout run, calls a provider or asks a model. A
watch is not a trade recommendation and PROMOTABLE is not an approval: these
routes report what the scout recorded and nothing more.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.data.database import connect
from src.scout.models import WatchAssessment
from src.scout.policy import WatchStatus
from src.scout.read import RunPage, ScoutOverview, ScoutReadService, WatchDetail, WatchPage

router = APIRouter(prefix="/api/scout")


async def scout_reader(request: Request) -> AsyncIterator[ScoutReadService]:
    engine, sessions = connect(request.app.state.settings.database_url)
    try:
        yield ScoutReadService(sessions)
    finally:
        await engine.dispose()


Reader = Annotated[ScoutReadService, Depends(scout_reader)]


def unavailable() -> HTTPException:
    return HTTPException(503, "Scout data unavailable")


@router.get("/overview")
async def overview(reader: Reader) -> ScoutOverview:
    try:
        return await reader.overview(datetime.now(UTC))
    except (SQLAlchemyError, OSError) as error:
        raise unavailable() from error


@router.get("/watches")
async def watches(
    reader: Reader,
    status: WatchStatus | None = None,
    chain: Literal["robinhood", "bsc"] | None = None,
    venue: Annotated[
        str | None, Query(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    ] = None,
    has_assessment: bool | None = None,
    promotable: bool | None = None,
    max_age_seconds: Annotated[int | None, Query(ge=60, le=30 * 86400)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> WatchPage:
    try:
        return await reader.watches(
            datetime.now(UTC),
            status=status,
            chain=chain,
            venue=venue,
            has_assessment=has_assessment,
            promotable=promotable,
            max_age=None if max_age_seconds is None else timedelta(seconds=max_age_seconds),
            limit=limit,
            offset=offset,
        )
    except (SQLAlchemyError, OSError) as error:
        raise unavailable() from error


@router.get("/watches/{watch_id}")
async def watch(reader: Reader, watch_id: UUID) -> WatchDetail:
    try:
        found = await reader.detail(watch_id, datetime.now(UTC))
    except (SQLAlchemyError, OSError) as error:
        raise unavailable() from error
    if found is None:
        raise HTTPException(404, "Watch not found")
    return found


@router.get("/watches/{watch_id}/assessments")
async def assessments(reader: Reader, watch_id: UUID) -> tuple[WatchAssessment, ...]:
    try:
        found = await reader.assessments(watch_id)
    except (SQLAlchemyError, OSError) as error:
        raise unavailable() from error
    if found is None:
        raise HTTPException(404, "Watch not found")
    return found


@router.get("/runs")
async def runs(
    reader: Reader,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> RunPage:
    try:
        return await reader.runs(limit, offset)
    except (SQLAlchemyError, OSError) as error:
        raise unavailable() from error
