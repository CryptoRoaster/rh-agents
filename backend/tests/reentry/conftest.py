"""Fixtures for the explicit paper re-entry.

The whole chain runs: a case is built from evidence, carried through the real
risk request into a stored approval, filled through the real paper service,
booked by the real ledger and closed by the real exit service — and only then is
a successor cycle asked for.

What is stubbed is the market feed's read and the stop source: two ports,
supplied as values. The evidence is fixture evidence. No provider is called, no
specialist worker runs and there is no launcher.
"""

from decimal import Decimal

from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits, TradingMode
from src.data.tables import PositionRow, TradeCycleRow
from src.orchestration.paper import PaperTradingService
from src.orchestration.reentry.service import PaperReentryService
from src.orchestration.workflow.service import TradeCaseService
from tests.paperexit.conftest import (  # noqa: F401
    IDENTITY,
    RunningSystem,
    build_exit_service,
    configured_costs,
    entered,
    exits,
    market_feed,
    market_for,
    money,
    position_of,
    read_account,
    recorded_snapshot,
    risk_db,
    set_account,
)


def build_reentry_service(sessions, now, *, limits=None, pause="running", **overrides):
    """The real workflow service behind one narrow call.

    No market port at all: nothing here prices, values or judges anything, so
    there is nothing for this service to read from a provider.
    """
    clock = FixedClock(now)
    bounds = limits if limits is not None else RiskLimits()
    arguments = {
        "sessions": sessions,
        "cases": TradeCaseService(sessions, clock=clock),
        "paper": PaperTradingService(sessions, bounds, TradingMode.PAPER, clock=clock),
        "trading_mode": TradingMode.PAPER,
        "clock": clock,
        "pause": RunningSystem() if pause == "running" else pause,
    }
    return PaperReentryService(**{**arguments, **overrides})


async def closed_cycle(sessions, now, trace, *, key="cycle-one", feed=None):
    """One complete cycle: a real entry and a real exit that closed it."""
    feed = feed if feed is not None else market_feed(now)
    case, entry, position = await entered(sessions, now, trace, key=key, feed=feed)
    sale = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key=f"{key}-exit"
    )
    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    return case, entry, position, sale


async def cycles(sessions):
    async with sessions() as session:
        return (await session.scalars(select(TradeCycleRow).order_by(TradeCycleRow.sequence))).all()


async def holding(sessions, asset):
    async with sessions() as session:
        return await session.scalar(select(PositionRow).where(PositionRow.asset_id == asset))


ZERO = Decimal("0")
