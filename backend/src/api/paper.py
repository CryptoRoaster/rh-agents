"""Read-only paper portfolio: what the ledger booked, nothing estimated. GET only."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.data.database import connect
from src.ledger.read import PaperPortfolio, PaperReadService

router = APIRouter(prefix="/api/paper")


async def paper_reader(request: Request) -> AsyncIterator[PaperReadService]:
    engine, sessions = connect(request.app.state.settings.database_url)
    try:
        yield PaperReadService(sessions)
    finally:
        await engine.dispose()


Reader = Annotated[PaperReadService, Depends(paper_reader)]


@router.get("/portfolio")
async def portfolio(
    reader: Reader,
    fills: Annotated[int, Query(ge=1, le=200)] = 50,
    pnl: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaperPortfolio:
    try:
        return await reader.portfolio(fills=fills, pnl=pnl)
    except (SQLAlchemyError, OSError) as error:
        raise HTTPException(503, "Paper ledger unavailable") from error
