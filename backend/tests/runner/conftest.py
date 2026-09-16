"""Fixtures for the bounded PAPER run.

The run under test is the real one: the real intake, the real workflow, the real
worker runtime, the real risk request, `src.risk.engine.evaluate`, the real
`PaperExecutor` and the real ledger, composed by the real `build_stack`.

What is supplied rather than configured: the market observations are recorded
fixture-shaped rows written through the real `MarketRecorder`, the case evidence
is fixture evidence submitted through the real workflow, and any external port a
role would need arrives through `RunnerPorts` — in this code, visibly, because a
configuration can never produce one.

No provider is called, no model is called, no launcher exists and nothing here
runs longer than the one pass it asks for.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.config import Settings
from src.data.tables import ExecutionRow, TradeCaseRow
from src.markets.recorder import MarketRecorder
from src.runner.composition import RunnerPorts, build_stack
from src.runner.service import BoundedPaperRun
from tests.riskdata.conftest import (  # noqa: F401
    IDENTITY,
    PAIR_ID,
    recorded_snapshot,
)
from tests.riskrequest.conftest import (  # noqa: F401
    FRESH,
    read_account,
    ready_case,
    risk_db,
    seed_account,
    set_account,
)

TEST_DATABASE = "postgresql+asyncpg://runner@localhost:5432/rh_agents_runner"


def runner_settings(**overrides) -> Settings:
    """A configuration that permits a run, stated in full.

    Every value a run needs is set here explicitly. Nothing is defaulted into
    existence: an unset entry size or cost basis is a refusal, which is the
    behaviour the rest of this system already has.
    """
    base = {
        "database_url": TEST_DATABASE,
        "trading_mode": "PAPER",
        "paper_runner_enabled": True,
        "paper_requested_notional_usd": "500",
        "paper_fee_bps": "30",
        "paper_slippage_bps": "50",
        "paper_runner_max_seconds": 60,
        "paper_runner_step_timeout_seconds": 10,
    }
    return Settings.model_validate({**base, **overrides})


async def record_market(sessions, now, *, age=FRESH, price=None):
    """One real recorded observation, written through the real recorder."""
    extra = {} if price is None else {"price": price}
    snapshot = recorded_snapshot(now, age=age, metadata_age=age, **extra)
    await MarketRecorder(sessions, clock=FixedClock(now)).record(snapshot)
    return snapshot


def stack_for(sessions, settings, now, *, ports=None):
    """The real composition, on a fixed clock so time is a test input."""
    return build_stack(
        settings,
        sessions,
        ports=ports if ports is not None else RunnerPorts(),
        clock=FixedClock(now),
    )


async def run(sessions, settings, now, *, ports=None, run_id=None):
    """One bounded pass, through the real run object."""
    stack = stack_for(sessions, settings, now, ports=ports)
    return await BoundedPaperRun(stack, run_id=run_id).execute()


async def cases_in(sessions):
    async with sessions() as session:
        return (await session.scalars(select(TradeCaseRow))).all()


async def executions(sessions):
    async with sessions() as session:
        return (await session.scalars(select(ExecutionRow))).all()


@pytest.fixture
def notional():
    return Decimal("500")


@pytest.fixture
def lifetime():
    return timedelta(hours=1)


def fresh_trace():
    return uuid4()
