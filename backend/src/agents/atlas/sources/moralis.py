"""Moralis adapter: BNB Smart Chain holder facts.

Blockscout does not index BNB Smart Chain, so chain 56 needs its own verified
source. Moralis is used because it documents the two properties the deterministic
math depends on: raw integer balances, and an explicit ``order`` parameter, so a
balance-ordered prefix is a stated contract rather than an observed accident.

**Ordering is documented, not proven.** ``order`` is a documented request
parameter taking ``ASC`` or ``DESC``, and the endpoint is documented as returning
owners sorted by balance. It is sent explicitly on every page rather than relying
on the documented default, but the cursor is opaque, so the global top-prefix
rests on the provider's stated contract rather than on an inspectable keyset
predicate the way Robinhood's does. Observed order is verified across pages
regardless.

**Reduced assurance, stated plainly.** Moralis answers with current indexed state
and names no block, no block hash and no indexer snapshot timestamp, so the
holder observation can only be anchored to the moment the response was received.
That is materially weaker than Robinhood's block-anchored provenance: it proves
when this representation arrived, never that the state behind it is that recent,
so an indexer running behind is invisible to it. It is recorded as
``RESPONSE_TIME`` on the fact, no code pretends the two chains have equal
provenance, and nothing here fabricates a block or a snapshot time to fill the
gap.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from src.agents.atlas.models import (
    AtlasSourceFailure,
    HolderCompleteness,
    HolderFactsSourceResult,
    HolderObservationBasis,
    HolderSourceRow,
)
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
from src.core.clock import Clock, SystemClock
from src.markets.models import Availability

MAX_ITEMS_PER_PAGE = 200
# Hexadecimal chain selectors Moralis documents for the chains ATLAS supports.
CHAIN_SELECTORS = {"bsc": "0x38"}


@dataclass(frozen=True)
class MoralisConfig:
    base_url: str
    api_key: str
    timeout_seconds: int = 10
    max_pages: int = 2
    page_size: int = 100

    @property
    def max_requests(self) -> int:
        return self.max_pages


def transport_for(config: MoralisConfig) -> SourceTransport:
    # Moralis authenticates by header, so no credential ever appears in a URL.
    return SourceTransport(
        base_url=config.base_url,
        headers={"X-API-Key": config.api_key},
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests,
    )


@dataclass(frozen=True)
class MoralisHolderSource:
    config: MoralisConfig
    chain: str = "bsc"
    clock: Clock = field(default_factory=SystemClock)
    transport_factory: Callable[[MoralisConfig], SourceTransport] = field(default=transport_for)
    source_name: str = "moralis"

    @property
    def source(self) -> str:
        return f"{self.source_name}:{self.chain}"

    async def holder_facts(self, chain: str, token_address: str) -> HolderFactsSourceResult:
        if chain != self.chain or chain not in CHAIN_SELECTORS:
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
        rows: list[HolderSourceRow] = []
        seen: set[str] = set()
        cursors: set[str] = set()
        supply: int | None = None
        complete = False
        cursor: str | None = None
        for _ in range(self.config.max_pages):
            params: dict[str, str | int] = {
                "chain": CHAIN_SELECTORS[self.chain],
                "order": "DESC",
                "limit": self.config.page_size,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = mapping(await transport.get_json(f"erc20/{token_address}/owners", params))
            page = [
                self._row(item)
                for item in sequence(payload.get("result"), limit=MAX_ITEMS_PER_PAGE)
            ]
            for row in page:
                if row.address in seen:
                    raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
                seen.add(row.address)
            rows.extend(page)
            # The provider's own supply is kept only for reconciliation; the
            # denominator is always the on-chain figure.
            supply = optional_unsigned(payload.get("total_supply")) if supply is None else supply
            cursor = self._cursor(payload.get("cursor"))
            if cursor is None:
                complete = True
                break
            if not page:
                raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
            if cursor in cursors:
                raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
            cursors.add(cursor)
        ordered = ordered_rows(tuple(rows))
        if not is_descending(tuple(rows)):
            # ``order=DESC`` was requested; a page that is not ordered means the
            # documented contract was not honoured and the prefix proves nothing.
            raise SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)
        if not complete and len(ordered) < TOP_N:
            raise SourceRequestError(AtlasSourceFailure.INCOMPLETE_RESULT)
        return HolderFactsSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=self.chain,
            token_address=token_address,
            rows=ordered,
            completeness=HolderCompleteness.COMPLETE if complete else HolderCompleteness.TOP_N_ONLY,
            # No block is named by this API, so the moment the response was
            # received is the only honest anchor and is labelled as the weaker
            # basis it is. A re-fetch produces a new receipt time and nothing
            # more: it can never turn this into block-anchored provenance.
            observation_basis=HolderObservationBasis.RESPONSE_TIME,
            snapshot_block=None,
            snapshot_timestamp=self.clock.now(),
            holder_count=None,
            provider_total_supply_raw=supply,
            requests_made=transport.requests_made,
        )

    @staticmethod
    def _cursor(value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value) > 4096:
            raise invalid()
        return value

    @staticmethod
    def _row(item: object) -> HolderSourceRow:
        entry = mapping(item)
        return HolderSourceRow(
            address=address(entry.get("owner_address")),
            balance_raw=unsigned(entry.get("balance")),
            is_contract=flag(entry.get("is_contract")),
        )
