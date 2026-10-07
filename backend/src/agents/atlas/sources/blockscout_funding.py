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
  time. Only V2 history depends on it: there a mined row without a
  timezone-aware one, or a timestamp that increases in the newest-first order,
  makes the *history* unusable (``history_failure``) and leaves V1 untouched.
  A V1-only read does not read it at all.

There is **no** documented block-range filter, so the window is enforced here.
Coverage down to the creation block is proven only by the provider's own order:
either the list ends (``next_page_params`` is ``null``) or a page reaches a
transaction older than the creation block. A read the page budget cuts before
that is ``LOWER_BOUND`` -- what was seen, never "nothing more".

With ``history_until`` (V2) the same single read goes on past the creation
block, keeping the rows before it no older than ``history_until``, until the
list ends, a row older than ``history_until`` proves the history reached it, or
the page budget runs out. The result says which of these happened; the caller
derives every window's coverage from that, never from a second read. A defect
in the history's time data stops only the history: the read then ends exactly
where a V1-only read would, and V1 is answered from the same pages.

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
            history_failure=read.history_failure,
            prelaunch_transactions=() if read.history_failure else tuple(read.prelaunch),
            history_ended=read.ended and read.history_failure is None,
            oldest_observed_at=None if read.history_failure else read.oldest,
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
                row = self._row(item, address_hash, timed=until is not None)
                if row is None:
                    continue  # pending: no block yet, so outside any pinned window
                transaction, position = row
                order = (transaction.block_number, position)
                if last is not None and order > last:
                    # The documented order is newest first; anything else
                    # proves nothing about what older pages hold.
                    raise invalid()
                last = order
                when = (
                    None
                    if until is None or read.history_failure
                    else self._history_time(read, transaction)
                )
                if transaction.block_number >= from_block:
                    read.found.append(transaction)
                elif until is not None and when is not None and when >= until:
                    read.prelaunch.append(transaction)
                    reached_creation = True
                else:
                    reached_creation = True
                    reached_cutoff = reached_cutoff or (
                        when is not None and until is not None and when < until
                    )
                if len(read.found) + len(read.prelaunch) > MAX_NORMALIZED_TRANSACTIONS:
                    raise invalid()
            next_params = payload.get("next_page_params")
            if next_params is None:
                read.ended = True
                read.coverage = FundingCoverage.COMPLETE
                return read
            if reached_creation:
                read.coverage = FundingCoverage.COMPLETE
                if until is None or reached_cutoff or read.history_failure:
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
    def _history_time(read: "_Read", transaction: FundingTransaction) -> datetime | None:
        """The row's time for the history, or None after marking the history unusable.

        Time must not run backwards in a newest-first list, or no time-based
        coverage could rest on it. Neither defect concerns V1's fields.
        """
        when = transaction.observed_at
        if when is None or (read.oldest is not None and when > read.oldest):
            read.history_failure = AtlasSourceFailure.INVALID_RESPONSE
            return None
        read.oldest = when
        return when

    @staticmethod
    def _row(
        item: object, address_hash: str, *, timed: bool = False
    ) -> tuple[FundingTransaction, int] | None:
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
                # Read only for a V2 history, and never fatal: a defective
                # timestamp is None here and fails the history alone.
                observed_at=_timestamp(entry.get("timestamp")) if timed else None,
            ),
            position,
        )


def _timestamp(value: object) -> datetime | None:
    """A mined row's block time in UTC, or None unless it is an exact aware instant."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass
class _Read:
    """What one bounded pagination saw. Coverage starts as cut short."""

    found: list[FundingTransaction] = field(default_factory=list)
    prelaunch: list[FundingTransaction] = field(default_factory=list)
    coverage: FundingCoverage = FundingCoverage.LOWER_BOUND
    ended: bool = False
    oldest: datetime | None = None
    # Set once the history's time data is defective; V1 is unaffected.
    history_failure: AtlasSourceFailure | None = None


__all__ = [
    "MAX_FUNDING_PAGES",
    "MAX_NORMALIZED_TRANSACTIONS",
    "BlockscoutFundingConfig",
    "BlockscoutFundingSource",
]
