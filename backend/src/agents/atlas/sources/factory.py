"""Construct the configured ATLAS fact sources. Construction only — nothing starts.

Building a source is not running one. No worker is launched here, no request is
made, and a chain whose provider is left at ``disabled`` is simply absent from
the routing table, which makes its facts explicitly unavailable rather than
quietly optional.
"""

from src.agents.atlas.ports import ContractOriginReadPort, HolderIntelligenceReadPort
from src.agents.atlas.sources.blockscout import (
    BlockscoutConfig,
    BlockscoutContractOriginSource,
    BlockscoutHolderSource,
)
from src.agents.atlas.sources.etherscan import EtherscanConfig, EtherscanContractOriginSource
from src.agents.atlas.sources.moralis import MoralisConfig, MoralisHolderSource
from src.agents.atlas.sources.routing import RoutedHolderSource, RoutedOriginSource
from src.core.clock import Clock, SystemClock
from src.core.config import Settings


def _blockscout(settings: Settings) -> BlockscoutConfig:
    return BlockscoutConfig(
        base_url=settings.blockscout_base_url,
        chain_id=settings.rh_chain_id,
        api_key=settings.blockscout_api_key.get_secret_value(),
        timeout_seconds=settings.atlas_source_timeout_seconds,
        max_pages=settings.atlas_holder_max_pages,
    )


def holder_sources(settings: Settings, *, clock: Clock | None = None) -> RoutedHolderSource:
    """Holder intelligence routing: Robinhood via Blockscout, BSC via Moralis."""
    sources: dict[str, HolderIntelligenceReadPort] = {}
    if settings.atlas_rh_holder_provider == "blockscout":
        sources["robinhood"] = BlockscoutHolderSource(
            config=_blockscout(settings), chain="robinhood"
        )
    if settings.atlas_bsc_holder_provider == "moralis":
        sources["bsc"] = MoralisHolderSource(
            config=MoralisConfig(
                base_url=settings.moralis_base_url,
                api_key=settings.moralis_api_key.get_secret_value(),
                timeout_seconds=settings.atlas_source_timeout_seconds,
                max_pages=settings.atlas_holder_max_pages,
                page_size=settings.atlas_holder_page_size,
            ),
            chain="bsc",
            clock=clock if clock is not None else SystemClock(),
        )
    return RoutedHolderSource(sources=sources)


def origin_sources(settings: Settings) -> RoutedOriginSource:
    """Creation provenance routing: Robinhood via Blockscout, BSC via Etherscan V2."""
    sources: dict[str, ContractOriginReadPort] = {}
    if settings.atlas_rh_origin_provider == "blockscout":
        sources["robinhood"] = BlockscoutContractOriginSource(
            config=_blockscout(settings), chain="robinhood"
        )
    if settings.atlas_bsc_origin_provider == "etherscan":
        sources["bsc"] = EtherscanContractOriginSource(
            config=EtherscanConfig(
                base_url=settings.etherscan_base_url,
                chain_id=settings.bsc_chain_id,
                api_key=settings.etherscan_api_key.get_secret_value(),
                timeout_seconds=settings.atlas_source_timeout_seconds,
            ),
            chain="bsc",
        )
    return RoutedOriginSource(sources=sources)
