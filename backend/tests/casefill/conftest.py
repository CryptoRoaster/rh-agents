"""Fixtures for case-bound PAPER execution.

The whole chain runs here: a case is built from evidence, carried through the
real risk request into a stored approval, and filled through the real paper
service. What is stubbed is the market feed's single read and the stop source —
two ports, supplied as values.
"""

from decimal import Decimal

import pytest

from src.core.clock import FixedClock
from src.core.models import RiskLimits, TradingMode
from src.markets.models import MarketCandidate
from src.orchestration.casefill.service import CaseFillService
from src.orchestration.paper import PaperTradingService
from src.orchestration.workflow.service import TradeCaseService
from tests.riskdata.conftest import (  # noqa: F401
    IDENTITY,
    PAIR_ID,
    RecordedMarkets,
    RunningSystem,
    configured_costs,
    holder_block,
    record,
    record_onchain,
)
from tests.riskrequest.conftest import (  # noqa: F401
    FRESH,
    build_service,
    fresh_onchain,
    fresh_snapshot,
    read_account,
    ready_case,
    risk_db,
    seed_account,
    set_account,
)


def build_fill_service(sessions, now, *, feed, limits=None, pause="running", **overrides):
    """The real workflow service and the real paper service behind one call.

    `limits` configures the paper service and nothing else. There is exactly one
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
    return CaseFillService(**{**arguments, **overrides})


async def approved_case(
    sessions,
    now,
    trace,
    *,
    key="fill",
    notional="500",
    onchain=None,
    lifetime=None,
    limits=None,
    identity=None,
    feed=None,
):
    """A case carried through the real risk request into a stored approval."""
    risk = build_service(sessions, now, notional=notional, limits=limits, feed=feed)
    extra = {} if lifetime is None else {"lifetime": lifetime}
    case = await ready_case(
        risk.cases, now, trace, onchain=onchain, key=f"{key}-case", identity=identity, **extra
    )
    result = await risk.request_risk_evaluation(case.id, request_key=f"{key}-req")
    return case, result, risk.markets


class MultiMarkets:
    """Several recorded markets, answered by pair. Never a provider."""

    def __init__(self, *snapshots) -> None:
        self._by_pair = {item.pair.pair_id: item for item in snapshots}
        self.requested: list[str] = []

    def replace(self, snapshot) -> None:
        self._by_pair[snapshot.pair.pair_id] = snapshot

    def drop(self, pair_id: str) -> None:
        self._by_pair.pop(pair_id, None)

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        self.requested.append(identity)
        return self._by_pair.get(identity)


def candidate_for(snapshot) -> MarketCandidate:
    """A recorded candidate pointing at exactly this market."""
    return MarketCandidate.from_snapshot(snapshot)


@pytest.fixture
def notional():
    return Decimal("500")
