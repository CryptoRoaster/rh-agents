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
  older exists -- with 50 items per page; the cursor also echoes the request's
  own documented ``filter`` query parameter, which is accepted only as the
  exact ``"from"`` this adapter sent;
* ``status`` is ``"ok"`` or ``"error"``, ``value`` is an integer string in wei,
  ``to`` is absent for a contract creation, ``block_number`` absent while pending;
* ``timestamp`` is a required, nullable ``date-time`` -- the transaction's block
  time. A mined row without a timezone-aware one is refused, and in the
  newest-first order timestamps never increase.

There is **no** documented block-range filter, so the window is enforced here.
Coverage down to the creation block is proven only by the provider's own order:
either the list ends (``next_page_params`` is ``null``) or a page reaches a
transaction older than the creation block. A read the page budget cuts before
that is ``LOWER_BOUND`` -- what was seen, never "nothing more".

With ``history_until`` (V2) the same single read goes on past the creation
block, keeping the rows before it no older than ``history_until``, until the
list ends, a row older than ``history_until`` proves the history reached it, or
the page budget runs out. The result says which of these happened; the caller
derives every window's coverage from that, never from a second read.

The key travels in the ``Authorization`` header, as everywhere else in this
adapter family. No provider record is persisted, only normalized transactions.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
# The request's own direction filter, which Blockscout echoes in the cursor.
# A documented query parameter of this endpoint, but never the provider's to
# choose: it must repeat exactly what was sent, and is not echoed back again.
FILTER_PARAMETER = "filter"
FILTER_FROM = "from"
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
        self,
        chain: str,
        address_hash: str,
        from_block: int,
        to_block: int,
        history_until: datetime | None = None,
    ) -> FundingSourceResult:
        if chain != self.chain:
            return self._failed(AtlasSourceFailure.UNSUPPORTED_CHAIN)
        if from_block > to_block or (history_until is not None and history_until.tzinfo is None):
            return self._failed(AtlasSourceFailure.INVALID_RESPONSE)
        until = None if history_until is None else history_until.astimezone(UTC)
        transport = self.transport_factory(self.config)
        try:
            read = await self._pages(transport, address_hash, from_block, until)
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
            coverage=read.coverage,
            # The window's upper edge is the caller's to enforce as well; rows
            # above it are kept out of the result so nothing later can leak in.
            transactions=tuple(
                item for item in read.found if from_block <= item.block_number <= to_block
            ),
            requests_made=transport.requests_made,
            history_until=until,
            prelaunch_transactions=tuple(read.prelaunch),
            history_ended=read.ended,
            oldest_observed_at=read.oldest,
        )

    def _failed(self, failure: AtlasSourceFailure, *, requests: int = 0) -> FundingSourceResult:
        return FundingSourceResult(
            status=Availability.UNAVAILABLE,
            failure=failure,
            source=self.source,
            requests_made=requests,
        )

    async def _pages(
        self,
        transport: SourceTransport,
        address_hash: str,
        from_block: int,
        until: datetime | None,
    ) -> "_Read":
        path = f"{self.config.chain_id}/api/v2/addresses/{address_hash}/transactions"
        params: dict[str, str | int] = {FILTER_PARAMETER: FILTER_FROM}
        read = _Read()
        cursors: set[str] = set()
        last: tuple[int, int] | None = None
        reached_creation = reached_cutoff = False
        for _ in range(self.config.max_pages):
            payload = mapping(await transport.get_json(path, params))
            items = sequence(payload.get("items"), limit=PAGE_SIZE)
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
                when = transaction.observed_at
                if when is None or (read.oldest is not None and when > read.oldest):
                    # Time must not run backwards in a newest-first list, or
                    # no time-based coverage could rest on it.
                    raise invalid()
                read.oldest = when
                if transaction.block_number >= from_block:
                    read.found.append(transaction)
                elif until is not None and when >= until:
                    read.prelaunch.append(transaction)
                else:
                    reached_creation = True
                    reached_cutoff = reached_cutoff or (until is not None and when < until)
                    continue
                if transaction.block_number < from_block:
                    reached_creation = True
                if len(read.found) + len(read.prelaunch) > MAX_NORMALIZED_TRANSACTIONS:
                    raise invalid()
            next_params = payload.get("next_page_params")
            if next_params is None:
                read.ended = True
                read.coverage = FundingCoverage.COMPLETE
                return read
            if reached_creation:
                read.coverage = FundingCoverage.COMPLETE
                if until is None or reached_cutoff:
                    return read
            if not items:
                raise invalid()  # promises more, delivers nothing
            params = {FILTER_PARAMETER: FILTER_FROM, **self._page_params(next_params)}
            marker = repr(sorted(params.items()))
            if marker in cursors:
                raise invalid()  # a repeating cursor loops forever
            cursors.add(marker)
        return read

    @staticmethod
    def _page_params(value: object) -> dict[str, str | int]:
        raw = dict(mapping(value))
        if FILTER_PARAMETER in raw and raw.pop(FILTER_PARAMETER) != FILTER_FROM:
            # An echoed direction other than ours is an answer to another query.
            raise invalid()
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
                observed_at=_timestamp(entry.get("timestamp")),
            ),
            position,
        )


def _timestamp(value: object) -> datetime:
    """A mined row's block time: an ISO 8601 date-time with its offset, in UTC."""
    if not isinstance(value, str) or len(value) > 40:
        raise invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise invalid() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise invalid()
    return parsed.astimezone(UTC)


@dataclass
class _Read:
    """What one bounded pagination saw. Coverage starts as cut short."""

    found: list[FundingTransaction] = field(default_factory=list)
    prelaunch: list[FundingTransaction] = field(default_factory=list)
    coverage: FundingCoverage = FundingCoverage.LOWER_BOUND
    ended: bool = False
    oldest: datetime | None = None


__all__ = [
    "MAX_FUNDING_PAGES",
    "MAX_NORMALIZED_TRANSACTIONS",
    "BlockscoutFundingConfig",
    "BlockscoutFundingSource",
]
