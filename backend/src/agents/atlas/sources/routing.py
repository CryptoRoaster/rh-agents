"""Deterministic chain-to-provider routing for ATLAS fact sources.

Which provider answers for which chain is a configuration decision resolved
once, here. There is no base-URL guessing, no "try the other vendor", and no
path where an unconfigured chain silently borrows another chain's source —
address equality means nothing across chains.

A chain with no configured provider returns an explicit unavailability, which is
the same fail-closed answer Phase 2D gave for every chain.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from src.agents.atlas.models import AtlasSourceFailure, HolderFactsSourceResult, OriginFacts
from src.agents.atlas.ports import ContractOriginReadPort, HolderIntelligenceReadPort
from src.markets.models import Availability


@dataclass(frozen=True)
class RoutedHolderSource:
    """One holder provider per chain, chosen by exact chain name."""

    sources: Mapping[str, HolderIntelligenceReadPort]
    source: str = "routed:holder-intelligence"

    async def holder_facts(self, chain: str, token_address: str) -> HolderFactsSourceResult:
        provider = self.sources.get(chain)
        if provider is None:
            return HolderFactsSourceResult(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.NOT_CONFIGURED,
                source=self.source,
            )
        return await provider.holder_facts(chain, token_address)


@dataclass(frozen=True)
class RoutedOriginSource:
    """One creation-history provider per chain, chosen by exact chain name."""

    sources: Mapping[str, ContractOriginReadPort]
    source: str = "routed:contract-origin"

    async def origin_facts(self, chain: str, token_address: str) -> OriginFacts:
        provider = self.sources.get(chain)
        if provider is None:
            return OriginFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.NOT_CONFIGURED,
                source=self.source,
            )
        return await provider.origin_facts(chain, token_address)
