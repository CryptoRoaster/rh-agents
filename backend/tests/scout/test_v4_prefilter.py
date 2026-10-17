"""The V4 prefilter: 20-byte pools are watched, never sent to ORBIT or the early intake.

Only a 32-byte V4 pool id can reach LP_CONTROL=PASS today; a 20-byte pool
address (V2/V3-style) is UNSUPPORTED, so no BUY is possible on it (issue #92).
With `EARLY_SCOUT_V4_PREFILTER_ENABLED` such a pool still becomes a watch with
its observations — discovery data is never thrown away — but it is not given a
paid ORBIT review and the early intake does not read it, so ATLAS never spends
holder or RPC reads on it. Off by default, and off nothing changes.
"""

from datetime import UTC, datetime, timedelta

from src.core.clock import FixedClock
from src.core.config import Settings
from src.markets.models import PoolLocatorKind
from src.markets.reader import MarketReader
from src.scout.candidates import EarlyWatchCandidates
from src.scout.policy import EARLY_SCOUT_V2
from src.scout.repository import WatchRepository
from tests.runner.provider import MarketProvider
from tests.scout.conftest import EchoOrbit, scout, scout_settings
from tests.scout.test_discovery_pages import v3, v4

T0 = datetime(2026, 10, 17, 6, tzinfo=UTC)


def settings(**overrides):
    return scout_settings(
        **{
            "early_scout_max_orbit_reviews_per_run": 5,
            "early_scout_history_max_requests_per_run": 0,
            **overrides,
        }
    )


async def kinds(sessions) -> dict[PoolLocatorKind, int]:
    """The locator kinds of the two watches the discovery created."""
    v3_address = v3(1)["attributes"]["address"]
    v4_address = v4(2)["attributes"]["address"]
    found: dict[PoolLocatorKind, int] = {}
    repository = WatchRepository(sessions)
    for pair in (
        f"robinhood:mainnet:contract_address:{v3_address}",
        f"robinhood:mainnet:bytes32_pool_id:uniswap-v4-robinhood:{v4_address}",
    ):
        watch = await repository.by_pair(pair)
        assert watch is not None, pair
        assert watch.market.pool_locator is not None
        kind = watch.market.pool_locator.kind
        found[kind] = found.get(kind, 0) + 1
    return found


def test_the_new_settings_are_off_or_bounded_by_default():
    defaults = Settings(_env_file=None, database_url="postgresql+asyncpg://x@localhost/db")
    assert defaults.early_scout_v4_prefilter_enabled is False
    assert defaults.early_scout_discovery_page2_enabled is False
    assert defaults.early_scout_max_discovery_page2_per_day == 72
    # The whole-page profile is a runtime change, never a default.
    assert defaults.early_scout_max_discovery_pools == 10


async def test_without_the_prefilter_both_pool_kinds_are_reviewed(db):
    _, sessions = db
    orbit = EchoOrbit()
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[v3(1), v4(2)]),
        orbit=orbit,
        settings=settings(),
    )
    assert len(orbit.calls) == 2


async def test_the_prefilter_watches_a_20_byte_pool_and_never_asks_orbit(db):
    _, sessions = db
    orbit = EchoOrbit()
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[v3(1), v4(2)]),
        orbit=orbit,
        settings=settings(early_scout_v4_prefilter_enabled=True),
    )
    # Both are discovery data: both are watches with their observations.
    assert summary.watches_created == 2
    assert await kinds(sessions) == {
        PoolLocatorKind.CONTRACT_ADDRESS: 1,
        PoolLocatorKind.BYTES32_POOL_ID: 1,
    }
    # Only the V4 pool is reviewed.
    assert len(orbit.calls) == 1
    shown = orbit.calls[0].data["market_observation"]["pair_id"]
    assert ":bytes32_pool_id:" in shown
    assert summary.watches_due_orbit == 1


async def test_the_prefilter_holds_under_the_fresh_first_policy(db):
    _, sessions = db
    orbit = EchoOrbit()
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[v3(1), v4(2)]),
        orbit=orbit,
        settings=settings(early_scout_v4_prefilter_enabled=True),
        policy=EARLY_SCOUT_V2,
    )
    assert len(orbit.calls) == 1
    assert ":bytes32_pool_id:" in orbit.calls[0].data["market_observation"]["pair_id"]


async def test_the_early_intake_reads_only_v4_watches_with_the_prefilter(db):
    _, sessions = db
    await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[v3(1), v4(2)]),
        settings=settings(early_scout_max_orbit_reviews_per_run=0),
    )
    now = T0 + timedelta(minutes=5)
    clock = FixedClock(now)

    def source(v4_only: bool) -> EarlyWatchCandidates:
        return EarlyWatchCandidates(
            sessions=sessions,
            markets=MarketReader(sessions, clock=clock),
            watches=WatchRepository(sessions),
            clock=clock,
            v4_only=v4_only,
        )

    everything = {item.pair_id for item in await source(False).eligible()}
    assert len(everything) == 2
    filtered = await source(True).eligible()
    assert [":bytes32_pool_id:" in item.pair_id for item in filtered] == [True]
