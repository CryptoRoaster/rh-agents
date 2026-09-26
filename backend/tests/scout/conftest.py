"""Fixtures for the early-discovery scout.

The scout under test is the real one: the real transport, network directory,
`GeckoTerminalAdapter`, `MarketRecorder`, watch repository, ORBIT evaluator and
VECTOR `assess`. Three outside edges are replaced, each in code where it is
visible:

* the market provider, as the HTTP responses it would send (`MarketProvider`);
* the model, as an `EchoOrbit` that answers coherently about whatever market it
  is shown and counts every call;
* the OHLCV series, as a `ScriptedHistory` returning prepared series.

Nothing here reaches a network, a model or a wallet.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from src.agents.orbit.models import (
    OrbitAssessment,
    OrbitClassification,
    OrbitReasonCode,
    OrbitStrength,
)
from src.core.clock import FixedClock
from src.core.config import Settings
from src.data.tables import TradeCaseRow
from src.markets.fake import fixture_history
from src.markets.history import MarketHistory, MarketHistoryUnavailable
from src.markets.models import MarketIdentity
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningRequest,
    ReasoningResult,
    ReasoningUsage,
)
from src.scout.service import EarlyScoutCycle, ScoutPorts
from tests.atlas.conftest import QUOTE
from tests.riskrequest.conftest import risk_db as risk_db  # noqa: F401
from tests.runner.provider import MarketProvider, pool

TEST_DATABASE = "postgresql+asyncpg://scout@localhost:5432/rh_agents_scout"
CHAIN = "robinhood"

# Three unrelated young pools. Addresses sort c1 < c2 < c3, so the pair
# identifiers do too — which is what lets a test tell "ordered by identity" from
# "ordered by liquidity".
POOLS = ("0x" + "c1" * 20, "0x" + "c2" * 20, "0x" + "c3" * 20)
TOKENS = ("0x" + "a3" * 20, "0x" + "a4" * 20, "0x" + "a5" * 20)


def pair_id(address: str) -> str:
    return f"{CHAIN}:mainnet:contract_address:{address}"


def young(index: int, *, liquidity: str = "5000", volume: str = "100", price="0.001"):
    """One freshly listed pool, deliberately small: nothing here filters on size."""
    return pool(
        POOLS[index],
        base=TOKENS[index],
        quote=QUOTE,
        price=price,
        liquidity=liquidity,
        volume=volume,
    )


def scout_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": TEST_DATABASE,
        "market_provider": "geckoterminal",
        "market_chains": CHAIN,
        "early_scout_enabled": True,
        "early_scout_max_discovery_pools": 10,
        "early_scout_max_new_watches_per_run": 10,
        "early_scout_max_orbit_reviews_per_run": 5,
        "early_scout_max_history_checks_per_run": 5,
        "early_scout_max_refresh_markets_per_run": 5,
    }
    return Settings(_env_file=None, **{**base, **overrides})


class EchoOrbit:
    """A model that answers coherently about the market it was shown.

    It never invents a value: every reason code is derived from the statuses in
    the document it received, so the real validator accepts it. `lie=True`
    names another market, which the real validator must refuse.
    """

    name = "fake"

    def __init__(
        self,
        classification: OrbitClassification = OrbitClassification.NOT_INTERESTING,
        *,
        lie: bool = False,
        failure: ReasoningErrorCategory | None = None,
    ) -> None:
        self.classification = classification
        self.lie = lie
        self.failure = failure
        self.calls: list[ReasoningRequest[Any]] = []

    async def generate_structured(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise ReasoningFailure(self.failure, "SCRIPTED_FAILURE")
        shown = request.data["market_observation"]
        codes = []
        gaps = []
        for field, present, absent in (
            ("price", OrbitReasonCode.PRICE_AVAILABLE, OrbitReasonCode.PRICE_UNKNOWN),
            ("liquidity", OrbitReasonCode.LIQUIDITY_PRESENT, OrbitReasonCode.LIQUIDITY_UNKNOWN),
            ("volume", OrbitReasonCode.VOLUME_PRESENT, OrbitReasonCode.VOLUME_UNKNOWN),
        ):
            if shown[field]["status"] == "AVAILABLE":
                codes.append(present)
            else:
                codes.append(absent)
                gaps.append(absent)
        classification = self.classification
        if classification is OrbitClassification.INSUFFICIENT_DATA and not gaps:
            # Insufficient data must name a gap. Only possible where one exists,
            # so a fully observed market is answered as NOT_INTERESTING instead.
            classification = OrbitClassification.NOT_INTERESTING
        output = OrbitAssessment(
            classification=classification,
            strength=OrbitStrength.WEAK,
            reason_codes=tuple(codes),
            data_gaps=tuple(gaps),
            cited_observation_ids=(shown["snapshot_id"],),
            pair_id="robinhood:mainnet:somewhere-else" if self.lie else shown["pair_id"],
            chain=shown["chain"],
            summary="Observed values only, as supplied.",
        )
        return ReasoningResult(
            output=output,
            model=ReasoningModel(provider="fake", model="echo-orbit"),
            usage=ReasoningUsage(input_tokens=100, output_tokens=40, latency_ms=5),
        )


class ScriptedHistory:
    """Prepared series by pair, and a count of every read."""

    def __init__(self, bars: int | Exception = 0) -> None:
        self.bars = bars
        self.reads: list[str] = []

    async def history(
        self, identity: MarketIdentity, *, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory:
        self.reads.append(identity.pair_id)
        if isinstance(self.bars, Exception):
            raise self.bars
        now = self.now
        return fixture_history(
            identity,
            newest_close=now.replace(minute=0, second=0, microsecond=0),
            bars=self.bars,
            requested_bars=bars,
            price=Decimal("0.001"),
            fetched_at=now,
        )

    now = None  # set by `scout` before each cycle


async def scout(
    sessions,
    now,
    *,
    provider: MarketProvider,
    orbit: EchoOrbit | None = None,
    history: ScriptedHistory | None = None,
    settings: Settings | None = None,
):
    """One real scout cycle at `now`, with the outside edges replaced."""
    history = history if history is not None else ScriptedHistory()
    history.now = now
    cycle = EarlyScoutCycle(
        settings if settings is not None else scout_settings(),
        sessions,
        ports=ScoutPorts(
            reasoning=orbit if orbit is not None else EchoOrbit(),
            market_http=provider.transport(),
            history=history,
        ),
        clock=FixedClock(now),
    )
    return await cycle.execute()


async def trade_cases(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TradeCaseRow))


HOUR = timedelta(hours=1)


@pytest.fixture
def db(risk_db):  # noqa: F811
    return risk_db


__all__ = [
    "HOUR",
    "MarketHistoryUnavailable",
    "MarketProvider",
    "POOLS",
    "ReasoningErrorCategory",
]
