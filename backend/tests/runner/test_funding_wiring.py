"""The creator funding source is composed only when switched on, and only for Robinhood."""

from datetime import UTC, datetime

from src.agents.atlas.sources.blockscout_funding import BlockscoutFundingSource
from src.core.clock import FixedClock
from src.runner.composition import (
    ChainContractSources,
    _exit_read,
    atlas_builder,
    ports_from_settings,
)
from tests.runner.conftest import runner_settings
from tests.runner.test_multichain import BOTH_RPC
from tests.runner.test_multichain import sessions as sessions  # noqa: F401

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
BLOCKSCOUT = {
    "atlas_rh_holder_provider": "blockscout",
    "atlas_rh_origin_provider": "blockscout",
    "blockscout_api_key": "proapi_testkey",
}


async def built(settings, sessions, *, exit_read: bool = False):
    ports = ports_from_settings(settings, sessions, clock=FixedClock(NOW))
    try:
        assert isinstance(ports.onchain, ChainContractSources)
        if exit_read:
            return _exit_read(settings, ports, FixedClock(NOW)).builder.builders  # type: ignore[attr-defined]
        return atlas_builder(settings, ports, FixedClock(NOW)).builders  # type: ignore[attr-defined]
    finally:
        for close in ports.closers:
            await close()


async def test_the_funding_graph_is_off_by_default(sessions):
    settings = runner_settings(market_provider="geckoterminal", **BOTH_RPC, **BLOCKSCOUT)
    assert settings.atlas_funding_graph_enabled is False
    for builder in (await built(settings, sessions)).values():
        assert builder.funding is None


async def test_switched_on_only_robinhood_gets_a_funding_source(sessions):
    settings = runner_settings(
        market_provider="geckoterminal", atlas_funding_graph_enabled=True, **BOTH_RPC, **BLOCKSCOUT
    )
    builders = await built(settings, sessions)
    for (chain, _), builder in builders.items():
        if chain == "robinhood":
            assert isinstance(builder.funding, BlockscoutFundingSource)
            assert builder.funding.chain == "robinhood"
            assert builder.funding.config.chain_id == settings.rh_chain_id
        else:
            assert builder.funding is None  # BSC is deferred


async def test_an_exit_read_never_reads_funding(sessions):
    settings = runner_settings(
        market_provider="geckoterminal", atlas_funding_graph_enabled=True, **BOTH_RPC, **BLOCKSCOUT
    )
    for builder in (await built(settings, sessions, exit_read=True)).values():
        assert builder.funding is None
