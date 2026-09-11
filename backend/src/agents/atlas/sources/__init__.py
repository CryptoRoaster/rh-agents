"""Verified external fact providers for ATLAS. Infrastructure, never a worker capability.

Each adapter turns one provider's response into typed, provenance-bound facts.
No adapter decides anything: completeness, concentration and freshness are
computed by the deterministic collector and judged by the versioned policy.
"""

from src.agents.atlas.sources.blockscout import (
    BlockscoutConfig,
    BlockscoutContractOriginSource,
    BlockscoutHolderSource,
)
from src.agents.atlas.sources.creation import parse_creation
from src.agents.atlas.sources.etherscan import EtherscanConfig, EtherscanContractOriginSource
from src.agents.atlas.sources.factory import holder_sources, origin_sources
from src.agents.atlas.sources.http import SourceRequestError, SourceTransport
from src.agents.atlas.sources.moralis import MoralisConfig, MoralisHolderSource
from src.agents.atlas.sources.normalize import (
    HolderConcentration,
    HolderNormalizationError,
    concentration,
    is_descending,
    ordered_rows,
)
from src.agents.atlas.sources.routing import RoutedHolderSource, RoutedOriginSource

__all__ = [
    "BlockscoutConfig",
    "BlockscoutContractOriginSource",
    "BlockscoutHolderSource",
    "EtherscanConfig",
    "EtherscanContractOriginSource",
    "HolderConcentration",
    "HolderNormalizationError",
    "MoralisConfig",
    "MoralisHolderSource",
    "RoutedHolderSource",
    "RoutedOriginSource",
    "SourceRequestError",
    "SourceTransport",
    "concentration",
    "holder_sources",
    "is_descending",
    "ordered_rows",
    "origin_sources",
    "parse_creation",
]
