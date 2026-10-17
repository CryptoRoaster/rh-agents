"""`GeckoTerminalAdapter.discover(page=...)`: one bounded page of new pools.

Page 1 stays the default and the only page any existing caller reads. A second
page is one more request under the same transport budget, never a loop.
"""

from datetime import UTC, datetime

import pytest

from src.core.clock import FixedClock
from src.core.config import Settings
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import BudgetError, ConfigurationError
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from tests.runner.provider import MarketProvider
from tests.scout.test_discovery_pages import page

NOW = datetime(2026, 10, 17, 6, tzinfo=UTC)


def settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x@localhost/db",
        market_provider="geckoterminal",
        market_chains="robinhood",
        geckoterminal_pools_per_chain=20,
        **overrides,
    )


async def read(provider: MarketProvider, *pages: int, **overrides) -> list[int]:
    configured = settings(**overrides)
    async with GeckoTerminalTransport(
        configured, transport=provider.transport(), clock=FixedClock(NOW)
    ) as transport:
        adapter = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, configured),
            CHAINS["robinhood"],
            configured,
            clock=FixedClock(NOW),
        )
        sizes = []
        for number in pages:
            pairs = await adapter.discover(page=number)
            assert adapter.returned == len(pairs)
            sizes.append(len(pairs))
        return sizes


async def test_page_one_is_the_default_and_page_two_is_asked_for_by_number():
    provider = MarketProvider(discovery_pages={1: page(1), 2: page(21, 7)})
    assert await read(provider, 1, 2) == [20, 7]
    assert provider.discovery_page_numbers == [1, 2]


@pytest.mark.parametrize("number", [0, 3, -1])
async def test_only_the_first_two_pages_exist_for_discovery(number):
    provider = MarketProvider(discovery_pages={1: page(1)})
    with pytest.raises(ConfigurationError):
        await read(provider, number)
    assert provider.discovery_page_numbers == []


async def test_page_two_spends_the_same_transport_budget():
    provider = MarketProvider(discovery_pages={1: page(1), 2: page(21)})
    with pytest.raises(BudgetError):
        # One request resolves the network, one reads page 1: nothing is left.
        await read(provider, 1, 2, geckoterminal_max_requests=2)
    assert provider.discovery_page_numbers == [1]
