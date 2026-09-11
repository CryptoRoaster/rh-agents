"""Honest stand-ins for fact sources that are not connected yet.

These exist so the system states plainly that a fact cannot currently be
established, instead of guessing, reconstructing from partial data, or quietly
treating the domain as optional. Returning UNAVAILABLE here is what makes ATLAS
fail closed rather than reach CLEAR on evidence it never had.
"""

from dataclasses import dataclass

from src.agents.atlas.models import AtlasSourceFailure, HolderFacts, OriginFacts
from src.markets.models import Availability


@dataclass(frozen=True)
class UnconfiguredHolderSource:
    """No verified holder indexer is connected for either supported chain.

    Robinhood Chain (4663) in particular has no holder API this project has
    verified. Implementing one against an unverified endpoint, or rebuilding a
    holder set from a bounded log scan, would produce a number that looks
    authoritative and is not.
    """

    source: str = "unconfigured:holder-intelligence"

    async def holder_facts(self, chain: str, token_address: str) -> HolderFacts:
        return HolderFacts(
            status=Availability.UNAVAILABLE,
            failure=AtlasSourceFailure.NOT_CONFIGURED,
            source=self.source,
        )


@dataclass(frozen=True)
class UnconfiguredOriginSource:
    """No creation-history source is connected.

    Current-state RPC cannot prove who deployed a contract. Inferring it from the
    first holder, the current owner or the pool creator would be a guess wearing
    the clothes of a fact.
    """

    source: str = "unconfigured:contract-origin"

    async def origin_facts(self, chain: str, token_address: str) -> OriginFacts:
        return OriginFacts(
            status=Availability.UNAVAILABLE,
            failure=AtlasSourceFailure.NOT_CONFIGURED,
            source=self.source,
        )
