"""Narrow fact-source ports for ATLAS.

Each port answers one question and always returns a typed availability plus
provenance. No port exposes a raw client, a session, or a way to ask something
that was not designed for. A worker never holds one of these directly: the
deterministic collector does.
"""

from datetime import datetime
from typing import Protocol

from src.agents.atlas.funding.models import FundingSourceResult
from src.agents.atlas.models import (
    ChainSnapshot,
    ContractFacts,
    HolderFactsSourceResult,
    OriginFacts,
)
from src.agents.atlas.v4.models import V4Census


class TokenContractReadPort(Protocol):
    """Deterministic token contract state at a chosen block."""

    async def chain_snapshot(self) -> ChainSnapshot: ...

    async def contract_facts(self, token_address: str, block: int) -> ContractFacts: ...

    async def balance_of(self, token_address: str, holder: str, block: int) -> int | None:
        """ERC-20 balance of one address at an explicit block, or None if unread."""
        ...

    async def block_timestamp(self, block: int) -> datetime | None:
        """The chain's own UTC timestamp of an explicit block, or None if unread."""
        ...


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


class PoolControlReadPort(Protocol):
    """A token's Uniswap V4 pools and positions, read from the chain itself.

    Answers for exactly the snapshot's chain and pinned block, scanning from
    ``from_block`` — a creation block it must verify, never a guess. Raises
    only when the source belongs to another chain, which is a hard stop; every
    other failure is an unavailable census with its reason.
    """

    async def census(
        self, snapshot: ChainSnapshot, token: str, from_block: int | None
    ) -> V4Census: ...


class FundingReadPort(Protocol):
    """A root address's normal transactions over one block window, read-only.

    Answers for exactly ``chain``, ``address`` and ``[from_block, to_block]``.
    ``coverage`` says whether every transaction of the window was read; a read
    cut short by a bound is a lower bound, never an empty answer. With
    ``history_until`` the same read also keeps the rows before ``from_block``
    no older than it, and says how far back the history provably reached.
    """

    @property
    def source(self) -> str: ...

    async def funding_transactions(
        self,
        chain: str,
        address: str,
        from_block: int,
        to_block: int,
        history_until: datetime | None = None,
    ) -> FundingSourceResult: ...
