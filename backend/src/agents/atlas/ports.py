"""Narrow fact-source ports for ATLAS.

Each port answers one question and always returns a typed availability plus
provenance. No port exposes a raw client, a session, or a way to ask something
that was not designed for. A worker never holds one of these directly: the
deterministic collector does.
"""

from typing import Protocol

from src.agents.atlas.models import ChainSnapshot, ContractFacts, HolderFacts, OriginFacts


class TokenContractReadPort(Protocol):
    """Deterministic token contract state at a chosen block."""

    async def chain_snapshot(self) -> ChainSnapshot: ...

    async def contract_facts(self, token_address: str, block: int) -> ContractFacts: ...


class HolderIntelligenceReadPort(Protocol):
    """Holder distribution from a source that can genuinely provide it.

    Plain EVM RPC cannot enumerate holders, so an implementation must be backed
    by a verified indexer. An unimplemented chain returns UNAVAILABLE rather than
    a reconstruction that looks complete and is not.
    """

    async def holder_facts(self, chain: str, token_address: str) -> HolderFacts: ...


class ContractOriginReadPort(Protocol):
    """Contract creation provenance, which current-state RPC cannot answer.

    Creator identity needs creation-transaction history, an indexer or archive
    access. Where none is configured the answer is UNAVAILABLE.
    """

    async def origin_facts(self, chain: str, token_address: str) -> OriginFacts: ...
