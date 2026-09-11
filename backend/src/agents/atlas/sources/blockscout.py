"""Blockscout PRO API adapter: Robinhood Chain holder and origin facts.

Blockscout is the official Robinhood Chain explorer and the only holder source
this project has verified for chain 4663. It is an **indexed** source, not chain
truth: contract facts stay independently verifiable over RPC, while holder facts
depend on Blockscout's indexer and are recorded as such.

Only the credentialed ``api.blockscout.com`` host is used. The public explorer
host sits behind an interactive challenge that answers ordinary server-side
clients with an HTML page, and passing it would mean impersonating a browser —
a fragile, unauthorised dependency for a safety-critical fact.

Holder rows carry no block of their own, so provenance is the indexer head read
immediately **before** the holder pages. That under-claims freshness rather than
over-claiming it, and the collector turns the gap between that head and the
pinned chain block into an explicit lag.

The holder list is ordered and filtered by the provider, and both properties are
stated explicitly below: descending balance order is proven from the endpoint's
own implementation, and the zero address is filtered out of it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.agents.atlas.models import (
    ZERO_ADDRESS,
    AtlasSourceFailure,
    HolderCompleteness,
    HolderFactsSourceResult,
    HolderObservationBasis,
    HolderSourceRow,
    OriginFacts,
)
from src.agents.atlas.sources.creation import parse_creation
from src.agents.atlas.sources.http import SourceRequestError, SourceTransport
from src.agents.atlas.sources.normalize import (
    TOP_N,
    HolderNormalizationError,
    is_descending,
    ordered_rows,
)
from src.agents.atlas.sources.parsing import (
    address,
    flag,
    invalid,
    mapping,
    optional_unsigned,
    sequence,
    unsigned,
)
from src.markets.models import Availability

# Keyset pagination parameters Blockscout echoes back.
#
# Descending global order is a *proven* property of this endpoint, not an
# inference from the cursor shape. Blockscout's own implementation of
# ``GET /api/v2/tokens/{hash}/holders`` orders with
# ``order_by([tb], desc: :value, desc: :address_hash)`` and pages with the keyset
# predicate ``tb.value < ^value or (tb.value == ^value and tb.address_hash <
# ^address_hash)``. A later page can therefore only contain rows strictly below
# the last row of the page before it, so the first page holds the globally
# largest balances and no unseen page can hide a larger holder. Its OpenAPI
# operation states the same contract: "List addresses holding a specific token
# sorted by balance".
#
# The provider's tie-break is *descending* address_hash. Ours is ascending, and
# is a canonical normalization only — never claimed as Blockscout's guarantee.
# It cannot move a metric: tied rows hold equal balances, so which of them lands
# in the top-N leaves every top-N sum identical.
PAGE_PARAMETERS = frozenset({"value", "address_hash", "items_count", "token_id"})
MAX_ITEMS_PER_PAGE = 200

# Blockscout's holder query filters ``address_hash != burn_address_hash`` — the
# zero address, and only that one. The list is therefore never the full holder
# universe, so the exclusion is declared rather than left for a reader to infer
# from a burn total that silently reads zero.
EXCLUDED_HOLDER_ADDRESSES: tuple[str, ...] = (ZERO_ADDRESS,)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise invalid() from None
    if parsed.tzinfo is None:
        raise invalid()
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class BlockscoutConfig:
    """Blockscout page size is fixed by the API at 50 rows and is not a parameter."""

    base_url: str
    chain_id: int
    api_key: str
    timeout_seconds: int = 10
    max_pages: int = 2

    @property
    def max_requests(self) -> int:
        # Token metadata, the indexer head, and one request per allowed page.
        return self.max_pages + 2


def transport_for(config: BlockscoutConfig) -> SourceTransport:
    # The key travels in a header, never in a query string, so it cannot leak
    # through a URL in a log line, a proxy access record or an exception.
    return SourceTransport(
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests,
    )


@dataclass(frozen=True)
class BlockscoutHolderSource:
    """Holder intelligence for one chain served by a Blockscout instance."""

    config: BlockscoutConfig
    chain: str
    transport_factory: Callable[[BlockscoutConfig], SourceTransport] = field(default=transport_for)
    source_name: str = "blockscout-pro"

    @property
    def source(self) -> str:
        return f"{self.source_name}:{self.config.chain_id}"

    async def holder_facts(self, chain: str, token_address: str) -> HolderFactsSourceResult:
        if chain != self.chain:
            return self._failed(AtlasSourceFailure.UNSUPPORTED_CHAIN)
        transport = self.transport_factory(self.config)
        try:
            return await self._collect(transport, token_address)
        except (SourceRequestError, HolderNormalizationError) as error:
            return self._failed(error.failure, requests=transport.requests_made)
        finally:
            await transport.aclose()

    def _failed(self, failure: AtlasSourceFailure, *, requests: int = 0) -> HolderFactsSourceResult:
        return HolderFactsSourceResult(
            status=Availability.UNAVAILABLE,
            failure=failure,
            source=self.source,
            requests_made=requests,
        )

    async def _collect(
        self, transport: SourceTransport, token_address: str
    ) -> HolderFactsSourceResult:
        prefix = f"{self.config.chain_id}/api/v2"
        metadata = mapping(await transport.get_json(f"{prefix}/tokens/{token_address}", {}))
        if address(metadata.get("address_hash")) != token_address:
            raise SourceRequestError(AtlasSourceFailure.TOKEN_MISMATCH)
        if metadata.get("type") != "ERC-20":
            # Holder semantics differ for NFTs; a non-ERC-20 answer is not the
            # fact this domain is about.
            raise SourceRequestError(AtlasSourceFailure.UNSUPPORTED_ENDPOINT)
        # Read the indexer head first, so the recorded provenance can only be
        # older than the holder rows that follow it.
        head_block, head_time = await self._indexer_head(transport, prefix)
        rows, complete = await self._holder_pages(transport, prefix, token_address)
        ordered = ordered_rows(tuple(rows))
        if not is_descending(tuple(rows)):
            # The provider order guarantee is what makes a prefix a *global* top-N.
            # Checking every row, across page boundaries, is the observed-order
            # validation that sits beside that guarantee — it can catch a broken
            # deployment, and it is never mistaken for the guarantee itself.
            raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
        completeness = HolderCompleteness.COMPLETE if complete else HolderCompleteness.TOP_N_ONLY
        if not complete and len(ordered) < TOP_N:
            raise SourceRequestError(AtlasSourceFailure.INCOMPLETE_RESULT)
        return HolderFactsSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=self.chain,
            token_address=token_address,
            rows=ordered,
            completeness=completeness,
            excluded_addresses=EXCLUDED_HOLDER_ADDRESSES,
            observation_basis=HolderObservationBasis.SOURCE_BLOCK,
            snapshot_block=head_block,
            snapshot_timestamp=head_time,
            holder_count=optional_unsigned(metadata.get("holders_count")),
            provider_total_supply_raw=optional_unsigned(metadata.get("total_supply")),
            requests_made=transport.requests_made,
        )

    async def _indexer_head(self, transport: SourceTransport, prefix: str) -> tuple[int, datetime]:
        blocks = sequence(await transport.get_json(f"{prefix}/main-page/blocks", {}), limit=20)
        if not blocks:
            raise SourceRequestError(AtlasSourceFailure.UNAVAILABLE)
        heads = [mapping(item) for item in blocks]
        latest = max(heads, key=lambda item: unsigned(item.get("height")))
        return unsigned(latest.get("height")), _timestamp(latest.get("timestamp"))

    async def _holder_pages(
        self, transport: SourceTransport, prefix: str, token_address: str
    ) -> tuple[list[HolderSourceRow], bool]:
        """Bounded keyset paging. Returns the rows and whether the set is complete."""
        rows: list[HolderSourceRow] = []
        seen: set[str] = set()
        cursors: set[str] = set()
        params: dict[str, str | int] = {}
        for _ in range(self.config.max_pages):
            payload = mapping(
                await transport.get_json(f"{prefix}/tokens/{token_address}/holders", params)
            )
            items = sequence(payload.get("items"), limit=MAX_ITEMS_PER_PAGE)
            page = [self._row(item) for item in items]
            for row in page:
                if row.address in seen:
                    # The same holder on two pages would double-count a balance.
                    raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
                seen.add(row.address)
            rows.extend(page)
            next_params = payload.get("next_page_params")
            if next_params is None:
                return rows, True
            if not page:
                # A page with no items that still promises more is incoherent.
                raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
            params = self._page_params(next_params)
            marker = repr(sorted(params.items()))
            if marker in cursors:
                # A repeating cursor is how a paginator loops forever.
                raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
            cursors.add(marker)
        return rows, False

    @staticmethod
    def _page_params(value: object) -> dict[str, str | int]:
        raw: Mapping[str, object] = mapping(value)
        if not raw or not set(raw) <= PAGE_PARAMETERS:
            # Only the documented cursor keys are echoed back, so a response
            # cannot inject an arbitrary query parameter into the next request.
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
    def _row(item: object) -> HolderSourceRow:
        entry = mapping(item)
        if entry.get("token_id") is not None:
            # An ERC-20 holder row has no token id; one that does is NFT data.
            raise invalid()
        holder = mapping(entry.get("address"))
        return HolderSourceRow(
            address=address(holder.get("hash")),
            balance_raw=unsigned(entry.get("value")),
            is_contract=flag(holder.get("is_contract")),
        )


@dataclass(frozen=True)
class BlockscoutContractOriginSource:
    """Contract creation provenance from the same Blockscout account."""

    config: BlockscoutConfig
    chain: str
    transport_factory: Callable[[BlockscoutConfig], SourceTransport] = field(default=transport_for)
    source_name: str = "blockscout-pro"

    @property
    def source(self) -> str:
        return f"{self.source_name}:{self.config.chain_id}"

    async def origin_facts(self, chain: str, token_address: str) -> OriginFacts:
        if chain != self.chain:
            return OriginFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNSUPPORTED_CHAIN,
                source=self.source,
            )
        transport = self.transport_factory(self.config)
        try:
            payload = await transport.get_json(
                "v2/api",
                {
                    "chain_id": self.config.chain_id,
                    "module": "contract",
                    "action": "getcontractcreation",
                    "contractaddresses": token_address,
                },
            )
            return parse_creation(payload, token_address, self.source)
        except SourceRequestError as error:
            return OriginFacts(
                status=Availability.UNAVAILABLE, failure=error.failure, source=self.source
            )
        finally:
            await transport.aclose()
