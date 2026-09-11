"""Narrow fact-source ports for ATLAS.

Each port answers one question and always returns a typed availability plus
provenance. No port exposes a raw client, a session, or a way to ask something
that was not designed for. A worker never holds one of these directly: the
deterministic collector does.
"""

from typing import Protocol

from src.agents.atlas.models import (
    ChainSnapshot,
    ContractFacts,
    HolderFactsSourceResult,
    OriginFacts,
)


class TokenContractReadPort(Protocol):
    """Deterministic token contract state at a chosen block."""

    async def chain_snapshot(self) -> ChainSnapshot: ...

    async def contract_facts(self, token_address: str, block: int) -> ContractFacts: ...


class HolderIntelligenceReadPort(Protocol):
    """Holder distribution from a source that can genuinely provide it.

    Plain EVM RPC cannot enumerate holders, so an implementation must be backed
    by a verified indexer. An unimplemented chain returns UNAVAILABLE rather than
    a reconstruction that looks complete and is not.

    A provider returns raw rows and provenance, never a concentration. The
    denominator is on-chain total supply, which the collector owns, so no vendor
    percentage can become a safety metric.
    """

    async def holder_facts(self, chain: str, token_address: str) -> HolderFactsSourceResult: ...


class CreationVerificationPort(Protocol):
    """Independent chain-side checks for a claimed contract creation.

    A provider returning JSON is not proof. These reads confirm the claim against
    the chain itself, and both answer ``None`` when the check could not be made —
    never a convenient default.
    """

    async def creation_receipt_contract(self, tx_hash: str) -> str | None: ...

    async def is_contract(self, address: str, block: int) -> bool | None: ...


class ContractOriginReadPort(Protocol):
    """Contract creation provenance, which current-state RPC cannot answer.

    Creator identity needs creation-transaction history, an indexer or archive
    access. Where none is configured the answer is UNAVAILABLE.
    """

    async def origin_facts(self, chain: str, token_address: str) -> OriginFacts: ...
