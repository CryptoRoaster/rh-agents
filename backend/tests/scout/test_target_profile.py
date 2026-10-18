"""The recommended early discovery profile, all of it at once, offline.

Shadow run B's recommendation (docs/early-discovery-shadow-b.md): every 20 pools
of page 1, page 2 only on proven saturation, 20-byte pools watched but kept away
from ORBIT and ATLAS, within the run and daily budgets, across a restart. This
runs the scout with exactly those settings against a scripted provider; nothing
is called and no scheduler is touched.
"""

from typing import Any

from tests.runner.provider import MarketProvider
from tests.scout.conftest import EchoOrbit, scout, scout_settings
from tests.scout.test_discovery_pages import FIVE, T0, v3, v4, watches

TARGET: dict[str, Any] = {
    "early_scout_max_discovery_pools": 20,
    "early_scout_max_new_watches_per_run": 40,
    "early_scout_discovery_page2_enabled": True,
    "early_scout_max_discovery_page2_per_day": 72,
    "early_scout_v4_prefilter_enabled": True,
    "early_scout_max_orbit_reviews_per_run": 10,
    "early_scout_history_max_requests_per_run": 0,
    "early_scout_max_refresh_markets_per_run": 0,
    "geckoterminal_retries": 0,
}


def mixed(start: int) -> list[dict[str, Any]]:
    """A full page: ten V2/V3-style and ten V4-style pools."""
    return [v3(start + index) for index in range(10)] + [v4(start + index) for index in range(10)]


async def test_the_target_profile_end_to_end(db):
    _, sessions = db
    settings = scout_settings(**TARGET)

    # First poll: no reference yet, so no second page.
    first_orbit = EchoOrbit()
    first = MarketProvider(discovery_pages={1: mixed(1)})
    summary = await scout(sessions, T0, provider=first, orbit=first_orbit, settings=settings)
    assert first.discovery_page_numbers == [1]
    assert summary.watches_created == 20
    # Every pool is a watch; only the ten V4 pools are reviewed.
    assert len(first_orbit.calls) == 10
    assert all(
        ":bytes32_pool_id:" in call.data["market_observation"]["pair_id"]
        for call in first_orbit.calls
    )

    # Five minutes later page 1 is entirely new: saturated, so page 2 once,
    # and page 2 reaches the previous poll -- no gap.
    second = MarketProvider(discovery_pages={1: mixed(101), 2: mixed(5)})
    summary = await scout(sessions, T0 + FIVE, provider=second, settings=settings)
    assert second.discovery_page_numbers == [1, 2]
    assert summary.discovery_no_overlap_polls == 1
    assert summary.discovery_page2_requests == 1
    assert summary.discovery_possible_gaps == 0

    # A restart repeats the same answer: nothing is created twice.
    before = await watches(sessions)
    again = MarketProvider(discovery_pages={1: mixed(101), 2: mixed(5)})
    summary = await scout(sessions, T0 + 2 * FIVE, provider=again, settings=settings)
    assert summary.watches_created == 0
    assert await watches(sessions) == before
