"""Blockscout PRO: a creator's outgoing normal transactions, for CREATOR_FUNDING_GRAPH.

Contract: ``GET /{chain_id}/api/v2/addresses/{address_hash}/transactions`` with
``filter=from``, as documented in Blockscout's PRO API OpenAPI specification
(``docs.blockscout.com/openapi-specs/pro-api-v12.json``) and implemented in
``blockscout/blockscout`` (``address_controller.ex`` and
``explorer/chain/transaction.ex``):

* ``filter=from`` restricts the list to transactions whose sender is the
  address; the response is still checked row by row, never trusted for it;
* the default order is ``desc block_number, desc index`` (then
  ``inserted_at``, ``hash``), so a later page only holds older transactions;
* pages are keyset cursors in ``next_page_params`` -- ``null`` when nothing
  older exists -- with 50 items per page;
* ``status`` is ``"ok"`` or ``"error"``, ``value`` is an integer string in wei,
  ``to`` is absent for a contract creation, ``block_number`` absent while pending.

There is **no** documented block-range filter, so the window is enforced here.
Coverage down to the creation block is proven only by the provider's own order:
either the list ends (``next_page_params`` is ``null``) or a page reaches a
transaction older than the creation block. A read the page budget cuts before
that is ``LOWER_BOUND`` -- what was seen, never "nothing more".

The key travels in the ``Authorization`` header, as everywhere else in this
adapter family. No provider record is persisted, only normalized transactions.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from src.agents.atlas.funding.models import (
    FundingCoverage,
    FundingSourceResult,
    FundingTransaction,
)
from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.sources.http import SourceRequestError, SourceTransport
from src.agents.atlas.sources.parsing import address, invalid, mapping, sequence, tx_hash, unsigned
from src.markets.models import Availability

# The documented keyset cursor keys of this endpoint. Only these are echoed
# back, so a response cannot inject an arbitrary parameter into the next call.
PAGE_PARAMETERS = frozenset(
    {"block_number", "index", "inserted_at", "hash", "value", "fee", "items_count"}
)
PAGE_SIZE = 50
# Fixed V1 bounds: at most ten pages, so at most 500 transactions per read.
MAX_FUNDING_PAGES = 10
MAX_NORMALIZED_TRANSACTIONS = MAX_FUNDING_PAGES * PAGE_SIZE


@dataclass(frozen=True)
class BlockscoutFundingConfig:
    base_url: str
    chain_id: int
    api_key: str
    timeout_seconds: int = 10
    max_pages: int = MAX_FUNDING_PAGES

    def __post_init__(self) -> None:
        if not 0 < self.max_pages <= MAX_FUNDING_PAGES:
            raise ValueError("The funding page budget is bounded")

    @property
    def max_requests(self) -> int:
        # One request per page and nothing else.
        return self.max_pages


def transport_for(config: BlockscoutFundingConfig) -> SourceTransport:
    return SourceTransport(
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests,
    )


@dataclass(frozen=True)
class BlockscoutFundingSource:
    """Outgoing normal transactions of one address on one Blockscout chain."""

    config: BlockscoutFundingConfig
    chain: str
    transport_factory: Callable[[BlockscoutFundingConfig], SourceTransport] = field(
        default=transport_for
    )
    source_name: str = "blockscout-pro"

    @property
    def source(self) -> str:
        return f"{self.source_name}:{self.config.chain_id}"

    async def funding_transactions(
        self, chain: str, address_hash: str, from_block: int, to_block: int
    ) -> FundingSourceResult:
        if chain != self.chain:
            return self._failed(AtlasSourceFailure.UNSUPPORTED_CHAIN)
        if from_block > to_block:
            return self._failed(AtlasSourceFailure.INVALID_RESPONSE)
        transport = self.transport_factory(self.config)
        try:
            found, coverage = await self._pages(transport, address_hash, from_block)
        except SourceRequestError as error:
            return self._failed(error.failure, requests=transport.requests_made)
        finally:
            await transport.aclose()
        return FundingSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=self.chain,
            address=address_hash,
            from_block=from_block,
            to_block=to_block,
            coverage=coverage,
            # The window's upper edge is the caller's to enforce as well; rows
            # above it are kept out of the result so nothing later can leak in.
            transactions=tuple(
                item for item in found if from_block <= item.block_number <= to_block
            ),
            requests_made=transport.requests_made,
        )

    def _failed(self, failure: AtlasSourceFailure, *, requests: int = 0) -> FundingSourceResult:
        return FundingSourceResult(
            status=Availability.UNAVAILABLE,
            failure=failure,
            source=self.source,
            requests_made=requests,
        )

    async def _pages(
        self, transport: SourceTransport, address_hash: str, from_block: int
    ) -> tuple[list[FundingTransaction], FundingCoverage]:
        path = f"{self.config.chain_id}/api/v2/addresses/{address_hash}/transactions"
        params: dict[str, str | int] = {"filter": "from"}
        found: list[FundingTransaction] = []
        cursors: set[str] = set()
        last: tuple[int, int] | None = None
        for _ in range(self.config.max_pages):
            payload = mapping(await transport.get_json(path, params))
            items = sequence(payload.get("items"), limit=PAGE_SIZE)
            reached_creation = False
            for item in items:
                row = self._row(item, address_hash)
                if row is None:
                    continue  # pending: no block yet, so outside any pinned window
                transaction, position = row
                order = (transaction.block_number, position)
                if last is not None and order > last:
                    # The documented order is newest first; anything else
                    # proves nothing about what older pages hold.
                    raise invalid()
                last = order
                if transaction.block_number < from_block:
                    reached_creation = True
                    continue
                found.append(transaction)
                if len(found) > MAX_NORMALIZED_TRANSACTIONS:
                    raise invalid()
            next_params = payload.get("next_page_params")
            if next_params is None or reached_creation:
                return found, FundingCoverage.COMPLETE
            if not items:
                raise invalid()  # promises more, delivers nothing
            params = {"filter": "from", **self._page_params(next_params)}
            marker = repr(sorted(params.items()))
            if marker in cursors:
                raise invalid()  # a repeating cursor loops forever
            cursors.add(marker)
        return found, FundingCoverage.LOWER_BOUND

    @staticmethod
    def _page_params(value: object) -> dict[str, str | int]:
        raw = mapping(value)
        if not raw or not set(raw) <= PAGE_PARAMETERS:
            raise invalid()
        params: dict[str, str | int] = {}
        for key, item in raw.items():
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, int | str) or len(str(item)) > 100:
                raise invalid()
            params[key] = item
        if not params:
            raise invalid()
        return params

    @staticmethod
    def _row(item: object, address_hash: str) -> tuple[FundingTransaction, int] | None:
        entry = mapping(item)
        if entry.get("block_number") is None:
            return None
        sender = address(mapping(entry.get("from")).get("hash"))
        if sender != address_hash:
            # ``filter=from`` was asked for; a row from anyone else is not it.
            raise invalid()
        recipient_entry = entry.get("to")
        recipient = (
            None if recipient_entry is None else address(mapping(recipient_entry).get("hash"))
        )
        status = entry.get("status")
        if status not in ("ok", "error"):
            # A mined transaction whose outcome is unknown can be neither
            # counted nor excluded.
            raise invalid()
        value = entry.get("value")
        if not isinstance(value, str):
            raise invalid()
        position = entry.get("position")
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise invalid()
        return (
            FundingTransaction(
                tx_hash=tx_hash(entry.get("hash")),
                block_number=unsigned(entry.get("block_number")),
                sender=sender,
                recipient=recipient,
                native_value_raw=unsigned(value),
                succeeded=status == "ok",
            ),
            position,
        )


__all__ = [
    "MAX_FUNDING_PAGES",
    "MAX_NORMALIZED_TRANSACTIONS",
    "BlockscoutFundingConfig",
    "BlockscoutFundingSource",
]
