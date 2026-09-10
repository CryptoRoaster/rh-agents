"""Recorded market reads only. No ingestion or trading mutation endpoints."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.data.database import connect
from src.markets.models import MarketCandidate, MarketSnapshot
from src.markets.reader import MarketReader

router = APIRouter()


async def market_reader(request: Request) -> AsyncIterator[MarketReader]:
    settings: Settings = request.app.state.settings
    engine, sessions = connect(settings.database_url)
    try:
        yield MarketReader(sessions, max_age=timedelta(seconds=settings.market_max_age_seconds))
    except (SQLAlchemyError, OSError) as error:
        raise HTTPException(status_code=503, detail="Recorded market data unavailable") from error
    finally:
        await engine.dispose()


Reader = Annotated[MarketReader, Depends(market_reader)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/api/markets", response_model=tuple[MarketSnapshot, ...])
async def markets(
    reader: Reader,
    provider: str | None = None,
    chain: str | None = None,
    network: str | None = None,
    include_fixtures: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
) -> tuple[MarketSnapshot, ...]:
    return await reader.markets(
        provider=provider,
        chain=chain,
        network=network,
        include_fixtures=include_fixtures,
        limit=limit,
        offset=offset,
    )


@router.get("/api/markets/{identity:path}", response_model=MarketSnapshot)
async def market(identity: str, reader: Reader, include_fixtures: bool = False) -> MarketSnapshot:
    snapshot = await reader.latest(identity, include_fixtures=include_fixtures)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No fresh available recorded snapshot")
    return snapshot


@router.get("/api/market-candidates", response_model=tuple[MarketCandidate, ...])
async def candidates(
    reader: Reader,
    include_fixtures: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
) -> tuple[MarketCandidate, ...]:
    return await reader.candidates(include_fixtures=include_fixtures, limit=limit, offset=offset)
