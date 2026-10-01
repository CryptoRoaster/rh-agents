"""Pool control is composed only when switched on, over the chain's own client."""

from datetime import UTC, datetime

from src.agents.atlas.context import AtlasSnapshotBuilder
from src.agents.atlas.rpc_source import RpcTokenContractSource
from src.agents.atlas.v4.census import V4PoolCensus
from src.core.clock import FixedClock
from src.runner.composition import ChainContractSources, atlas_builder, ports_from_settings
from tests.runner.conftest import runner_settings
from tests.runner.test_multichain import BOTH_RPC
from tests.runner.test_multichain import sessions as sessions  # noqa: F401

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


async def built(settings, sessions):
    ports = ports_from_settings(settings, sessions, clock=FixedClock(NOW))
    try:
        assert isinstance(ports.onchain, ChainContractSources)
        return atlas_builder(settings, ports, FixedClock(NOW)).builders  # type: ignore[attr-defined]
    finally:
        for close in ports.closers:
            await close()


async def test_pool_control_is_off_by_default(sessions):
    settings = runner_settings(market_provider="geckoterminal", **BOTH_RPC)
    assert settings.atlas_v4_pool_control_enabled is False
    for builder in (await built(settings, sessions)).values():
        assert isinstance(builder, AtlasSnapshotBuilder)
        assert builder.pool_census is None and builder.verifier is None


async def test_switched_on_each_chain_gets_its_own_census(sessions):
    settings = runner_settings(
        market_provider="geckoterminal", atlas_v4_pool_control_enabled=True, **BOTH_RPC
    )
    builders = await built(settings, sessions)
    for (chain, _), builder in builders.items():
        assert isinstance(builder.pool_census, V4PoolCensus)
        assert builder.pool_census.chain == chain
        assert isinstance(builder.contracts, RpcTokenContractSource)
        # The census reads through the very client the contract source uses.
        assert builder.pool_census.reads.client is builder.contracts.client  # type: ignore[attr-defined]
        assert builder.verifier is builder.contracts


async def test_an_exit_read_never_runs_the_census(sessions):
    from src.runner.composition import _exit_read

    settings = runner_settings(
        market_provider="geckoterminal", atlas_v4_pool_control_enabled=True, **BOTH_RPC
    )
    ports = ports_from_settings(settings, sessions, clock=FixedClock(NOW))
    try:
        read = _exit_read(settings, ports, FixedClock(NOW))
        for builder in read.builder.builders.values():  # type: ignore[attr-defined]
            assert builder.pool_census is None
    finally:
        for close in ports.closers:
            await close()
