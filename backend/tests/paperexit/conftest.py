"""Fixtures for the explicit paper exit.

The whole chain runs: a case is built from evidence, carried through the real
risk request into a stored approval, filled through the real paper service and
booked by the real ledger — and only then is the resulting position sold.

What is stubbed is the market feed's read and the stop source: two ports,
supplied as values. The evidence is fixture evidence. No provider is called, no
specialist worker runs and there is no launcher.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits, TradingMode
from src.data.tables import PositionRow, TradeCaseExitRow
from src.orchestration.paper import PaperTradingService
from src.orchestration.paperexit.service import PaperExitService
from src.orchestration.workflow.service import TradeCaseService
from tests.casefill.conftest import (  # noqa: F401
    MultiMarkets,
    approved_case,
    build_fill_service,
)
from tests.riskdata.conftest import (  # noqa: F401
    IDENTITY,
    PAIR_ID,
    RunningSystem,
    configured_costs,
    market_for,
    recorded_snapshot,
)
from tests.riskrequest.conftest import (  # noqa: F401
    FRESH,
    build_service,
    read_account,
    ready_case,
    risk_db,
    seed_account,
    set_account,
)


def build_exit_service(sessions, now, *, feed, limits=None, pause="running", **overrides):
    """The real workflow service, paper service and ledger behind one call.

    `limits` configures the paper service and nothing else: there is exactly one
    place SENTINEL's limits live, and a test that could set a second copy would
    be testing a configuration the service cannot be given.
    """
    clock = FixedClock(now)
    bounds = limits if limits is not None else RiskLimits()
    arguments = {
        "sessions": sessions,
        "cases": TradeCaseService(sessions, clock=clock),
        "paper": PaperTradingService(sessions, bounds, TradingMode.PAPER, clock=clock),
        "markets": feed,
        "costs": configured_costs(),
        "trading_mode": TradingMode.PAPER,
        "clock": clock,
        "pause": RunningSystem() if pause == "running" else pause,
        "include_fixtures": False,
    }
    return PaperExitService(**{**arguments, **overrides})


def market_feed(now, *, price=None, age=FRESH):
    """One recorded market: the one every case here runs in."""
    extra = {} if price is None else {"price": price}
    return MultiMarkets(recorded_snapshot(now, age=age, metadata_age=age, **extra))


async def entered(sessions, now, trace, *, key="entry", feed=None, limits=None):
    """One case carried all the way to a booked PAPER entry, and its position."""
    feed = feed if feed is not None else market_feed(now)
    case, approval, _ = await approved_case(sessions, now, trace, key=key, feed=feed, limits=limits)
    assert approval.kind == "risk_request_evaluated", getattr(approval, "reason", None)
    fill = await build_fill_service(sessions, now, feed=feed, limits=limits).execute_case_fill(
        case.id, request_key=f"{key}-req"
    )
    assert fill.kind == "paper_fill_recorded", getattr(fill, "detail", None)
    return case, fill, await position_of(sessions)


async def position_of(sessions):
    async with sessions() as session:
        return await session.scalar(select(PositionRow))


async def exits(sessions):
    async with sessions() as session:
        return (await session.scalars(select(TradeCaseExitRow))).all()


def money(value):
    """One comparable form for an amount that has been through the database.

    SQLite stores `Numeric(38, 18)` through a float, so a value read back there
    differs from the exact arithmetic in the last few places. The PostgreSQL
    suite carries the exact figures; the light suite compares them to the
    repository's six-place convention rather than pretending the round trip is
    lossless.
    """
    return Decimal(value).quantize(Decimal("0.000001"))


@pytest.fixture
def entry_price():
    """The price every fixture market quotes, before slippage."""
    return Decimal("1.25")
