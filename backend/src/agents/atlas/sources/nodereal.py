"""NodeReal adapter: BNB Smart Chain holder facts over NodeReal's JSON-RPC.

Two documented methods and nothing else, both on the BSC mainnet endpoint:

- ``nr_getTokenHolders(token, pageSize, pageKey, topN)`` — one page of holders.
  ``topN`` is sent explicitly on every call: NodeReal documents it as returning
  the top N holders ordered by balance, so the balance-ordered prefix is a
  requested contract rather than an observed accident. Balances arrive as
  hex-encoded raw integers and are used as such.
- ``nr_getTokenHolderCount(token)`` — the holder count, established by its own
  call rather than inferred from a page.

**What is checked, not trusted.** The observed order is verified (non-increasing
balances); duplicates, malformed addresses, non-hex balances, a count smaller
than the rows it covers, and a JSON-RPC envelope that is not the answer to this
request are all failures. At least ten rows must be present unless the count
proves the list is the whole holder set. No provider percentage exists in
either response, and the provider supplies no total supply: the denominator is
always the on-chain ``totalSupply`` ATLAS reads over its own RPC.

**Reduced assurance, stated plainly.** Neither method names a block, a block
hash or an indexer snapshot time. The observation can only be anchored to the
moment the last response was received, recorded as ``RESPONSE_TIME`` —
acceptable to the PAPER policy and to nothing stricter — and no block or
timestamp is invented to fill the gap.

**The credential is part of the URL path**, as NodeReal documents
(``/v1/{API-key}``). It is appended only inside the transport, never enters an
exception, a result or a record, and every HTTP log line from the client is
redacted by the shared transport.
"""

import re
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
from src.agents.atlas.sources.parsing import address, invalid, mapping, sequence
from src.core.clock import Clock, SystemClock
from src.markets.models import Availability

# NodeReal's documented maximum page size, and the one chain this adapter reads.
MAX_PAGE_SIZE = 100
SUPPORTED_CHAIN = "bsc"
# Hex quantities: a uint256 fits in 64 hex digits; NodeReal pads with leading
# zeros, so a longer string is accepted only if its value still fits.
HEX = re.compile(r"^0x[0-9a-fA-F]{1,80}$")
UINT256_MAX = 2**256 - 1
# One holder page plus one count per read.
REQUESTS_PER_READ = 2


@dataclass(frozen=True)
class NodeRealConfig:
    base_url: str
    api_key: str
    timeout_seconds: int = 10
    top_n: int = 50

    def __post_init__(self) -> None:
        if not TOP_N <= self.top_n <= MAX_PAGE_SIZE:
            raise ValueError("NodeReal topN must be between 10 and 100")

    @property
    def max_requests(self) -> int:
        return REQUESTS_PER_READ


def transport_for(config: NodeRealConfig) -> SourceTransport:
    return SourceTransport(
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests,
    )


def quantity(value: object) -> int:
    """A hex-encoded unsigned integer that fits a uint256, or a failure."""
    if not isinstance(value, str) or HEX.fullmatch(value) is None:
        raise invalid()
    parsed = int(value, 16)
    if parsed > UINT256_MAX:
        raise invalid()
    return parsed


def hex_quantity(value: int) -> str:
    return hex(value)


@dataclass(frozen=True)
class NodeRealHolderSource:
    config: NodeRealConfig
    chain: str = SUPPORTED_CHAIN
    clock: Clock = field(default_factory=SystemClock)
    transport_factory: Callable[[NodeRealConfig], SourceTransport] = field(default=transport_for)
    source_name: str = "nodereal"

    @property
    def source(self) -> str:
        return f"{self.source_name}:{self.chain}"

    async def holder_facts(self, chain: str, token_address: str) -> HolderFactsSourceResult:
        if chain != self.chain or chain != SUPPORTED_CHAIN:
            return self._failed(AtlasSourceFailure.UNSUPPORTED_CHAIN)
        try:
            token = address(token_address)
        except SourceRequestError:
            return self._failed(AtlasSourceFailure.TOKEN_MISMATCH)
        transport = self.transport_factory(self.config)
        try:
            return await self._collect(transport, token)
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

    async def _call(
        self, transport: SourceTransport, request_id: int, method: str, params: list[object]
    ) -> object:
        """One JSON-RPC call, answered by exactly this request or refused."""
        envelope = mapping(
            await transport.post_json(
                self.config.api_key,
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
        )
        if envelope.get("jsonrpc") != "2.0" or str(envelope.get("id")) != str(request_id):
            raise invalid()
        if "error" in envelope:
            # A provider-side refusal, whatever its message says. Nothing from
            # the message is kept: it is not a fact and could carry anything.
            raise SourceRequestError(AtlasSourceFailure.UNAVAILABLE)
        if "result" not in envelope:
            raise invalid()
        return envelope["result"]

    async def _collect(self, transport: SourceTransport, token: str) -> HolderFactsSourceResult:
        top_n = self.config.top_n
        page = mapping(
            await self._call(
                transport,
                1,
                "nr_getTokenHolders",
                [token, hex_quantity(top_n), "", hex_quantity(top_n)],
            )
        )
        rows = tuple(self._row(item) for item in sequence(page.get("details"), limit=top_n))
        seen: set[str] = set()
        for row in rows:
            if row.address in seen:
                raise invalid()
            seen.add(row.address)
        if not is_descending(rows):
            # topN was requested ordered by balance; a list that is not is not
            # the documented answer and proves no prefix.
            raise invalid()
        count = quantity(await self._call(transport, 2, "nr_getTokenHolderCount", [token]))
        if count < len(rows):
            # More rows than holders: the two answers describe different states.
            raise invalid()
        # Received after both answers: the latest moment this representation is
        # known to have existed, and nothing more.
        received = self.clock.now()
        complete = count == len(rows)
        if not complete and len(rows) < TOP_N:
            raise SourceRequestError(AtlasSourceFailure.INCOMPLETE_RESULT)
        return HolderFactsSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=self.chain,
            token_address=token,
            rows=ordered_rows(rows),
            # Complete only when the separately established count says the page
            # is every holder; otherwise a proven top-N prefix.
            completeness=HolderCompleteness.COMPLETE if complete else HolderCompleteness.TOP_N_ONLY,
            observation_basis=HolderObservationBasis.RESPONSE_TIME,
            snapshot_block=None,
            snapshot_timestamp=received,
            holder_count=count,
            # NodeReal reports no supply; the on-chain figure is the only one.
            provider_total_supply_raw=None,
            requests_made=transport.requests_made,
        )

    @staticmethod
    def _row(item: object) -> HolderSourceRow:
        entry = mapping(item)
        return HolderSourceRow(
            address=address(entry.get("accountAddress")),
            balance_raw=quantity(entry.get("tokenBalance")),
        )
