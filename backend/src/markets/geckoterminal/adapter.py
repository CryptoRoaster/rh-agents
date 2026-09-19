"""One normalization pipeline for both configured EVM chains; no trading logic."""

import re
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.markets.geckoterminal.dto import Pool, PoolsResponse, Provenance, Token
from src.markets.geckoterminal.errors import ConfigurationError, ContractError, IdentityError
from src.markets.geckoterminal.networks import Chain, NetworkDirectory, validate
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import (
    MarketIdentity,
    MarketPair,
    MarketSnapshot,
    PoolLocator,
    PoolLocatorIdentity,
    PoolLocatorKind,
)

# One JSON:API document may carry at most this many pools, so a request that
# asked for more than fit could never be answered in full. The bound is the DTO's
# own, stated once here rather than guessed at the call site.
MAX_POOLS_PER_REQUEST = 20


def address(value: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None or int(value[2:], 16) == 0:
        raise IdentityError()
    return value.lower()


def provenance(value: Provenance, chain: Chain) -> None:
    if value.chain not in (None, chain.name) or value.chain_id not in (None, chain.chain_id):
        raise IdentityError()


def bind_resource(resource_id: str, actual_address: str, network_id: str) -> None:
    prefix = network_id + "_"
    if (
        not resource_id.startswith(prefix)
        or resource_id[len(prefix) :].lower() != actual_address.lower()
    ):
        raise IdentityError()


def index_included(response: PoolsResponse) -> dict[str, object]:
    """The response's side-loaded resources, by id, with conflicts refused.

    Shared by both reads for one reason: a token resource that appears twice with
    different content is a contract breach whichever request surfaced it, and two
    copies of that rule could drift apart.
    """
    included: dict[str, object] = {}
    for raw in response.included:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ContractError()
        resource_id = raw["id"]
        if resource_id in included and included[resource_id] != raw:
            raise ContractError()
        included[resource_id] = raw
    return included


def normalize(
    pool: Pool,
    included: dict[str, object],
    chain: Chain,
    network_id: str,
    at: datetime,
    trace: UUID,
) -> MarketSnapshot:
    provenance(pool, chain)
    provenance(pool.attributes, chain)
    pool_address = pool.attributes.address.lower()
    bind_resource(pool.id, pool_address, network_id)
    relationships = pool.relationships
    if relationships.network is not None and (
        relationships.network.data.type != "network" or relationships.network.data.id != network_id
    ):
        raise IdentityError()
    if relationships.dex.data.type != "dex":
        raise IdentityError()
    venue = relationships.dex.data.id
    try:
        locator = PoolLocator(
            kind=PoolLocatorKind.CONTRACT_ADDRESS
            if len(pool_address) == 42
            else PoolLocatorKind.BYTES32_POOL_ID,
            value=pool_address,
            venue=venue,
        )
    except ValidationError:
        raise IdentityError() from None
    meta: dict[str, object] = {
        "observed_at": at,
        "provider": "geckoterminal",
        "chain": chain.name,
        "network": "mainnet",
        "correlation_id": trace,
        "is_fixture": False,
    }
    assets = []
    for relation in (relationships.base_token, relationships.quote_token):
        if relation.data.type != "token" or relation.data.id not in included:
            raise IdentityError()
        token = validate(Token, included[relation.data.id])
        provenance(token, chain)
        provenance(token.attributes, chain)
        token_address = address(token.attributes.address)
        bind_resource(token.id, token_address, network_id)
        assets.append(
            {
                **meta,
                "id": uuid4(),
                "asset_id": f"{chain.name}:mainnet:{token_address}",
                "symbol": token.attributes.symbol,
                "decimals": token.attributes.decimals,
            }
        )
    if assets[0]["asset_id"] == assets[1]["asset_id"]:
        raise IdentityError()
    meta["asset_id"] = assets[0]["asset_id"]
    values = pool.attributes
    price = values.base_token_price_usd
    if price is not None and price == 0:
        # Existing canonical prices must be positive. Preserve zero reserve/volume below.
        price = None
    try:

        def measurement(value: object) -> dict[str, object]:
            return {
                **meta,
                "id": uuid4(),
                "status": "UNKNOWN" if value is None else "AVAILABLE",
                "value_usd": value,
            }

        return MarketSnapshot.model_validate(
            {
                **meta,
                "id": uuid4(),
                "schema_version": 2,
                "pair": {
                    **meta,
                    "id": uuid4(),
                    "pair_id": locator.pair_id(chain.name, "mainnet"),
                    "pool_locator": locator,
                    "base": assets[0],
                    "quote": assets[1],
                    "venue": venue,
                },
                "price": measurement(price),
                "liquidity": measurement(values.reserve_in_usd),
                "volume": {
                    **measurement(values.volume_usd.h24 if values.volume_usd else None),
                    "window_seconds": 86400,
                },
            }
        )
    except ValidationError:
        raise ContractError() from None


class GeckoTerminalAdapter:
    provider = "geckoterminal"
    is_fixture = False

    def __init__(
        self,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        chain: Chain,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._directory = directory
        self.chain = chain
        self._limit = settings.geckoterminal_pools_per_chain
        self._clock = clock if clock is not None else SystemClock()
        self._snapshots: dict[MarketIdentity, MarketSnapshot] = {}
        self.discovered = 0
        self.failed = 0
        self.rejections: dict[str, int] = {}

    async def discover(self) -> tuple[MarketPair, ...]:
        self._snapshots = {}
        self.discovered = self.failed = 0
        self.rejections = {}
        network_id = await self._directory.resolve(self.chain)
        payload = await self._transport.get(
            f"networks/{network_id}/new_pools",
            {"include": "base_token,quote_token,dex", "page": 1},
        )
        at = self._clock.now()  # Successful fetch completion, never pool creation time.
        response = validate(PoolsResponse, payload)
        provenance(response, self.chain)
        included = index_included(response)
        seen: dict[tuple[str, str], object] = {}
        snapshots: dict[MarketIdentity, MarketSnapshot] = {}
        pool_addresses: set[str] = set()
        trace = uuid4()
        for raw in response.data[: self._limit]:
            self.discovered += 1
            try:
                pool = validate(Pool, raw)
            except ContractError as error:
                self.failed += 1
                self.rejections[error.code] = self.rejections.get(error.code, 0) + 1
                continue
            resource_key = (pool.id, pool.relationships.dex.data.id)
            if resource_key in seen:
                self.discovered -= 1
                if seen[resource_key] != raw:
                    raise IdentityError()
                continue
            seen[resource_key] = raw
            try:
                snapshot = normalize(pool, included, self.chain, network_id, at, trace)
            except ContractError as error:
                self.failed += 1
                self.rejections[error.code] = self.rejections.get(error.code, 0) + 1
                continue
            if snapshot.pair.pair_id in pool_addresses:
                raise IdentityError()
            pool_addresses.add(snapshot.pair.pair_id)
            snapshots[snapshot.pair.market_identity] = snapshot
        self._snapshots = snapshots
        return tuple(item.pair for item in snapshots.values())

    async def snapshot(self, pair: MarketPair) -> MarketSnapshot:
        # Discovery already contains all evidence. No detail requests or stale fallback.
        try:
            pair = MarketPair.model_validate(pair.model_dump())
        except ValidationError:
            raise IdentityError() from None
        snapshot = self._snapshots.get(pair.market_identity)
        if snapshot is None:
            raise IdentityError()
        return snapshot

    async def observe(self, locators: tuple[PoolLocatorIdentity, ...]) -> tuple[MarketPair, ...]:
        """Observe these exact pools again, and return only what was confirmed.

        The one read that is *not* discovery. Discovery answers "what is new on
        this chain", which is the wrong question for a market this system already
        holds a case or a position in: such a market stops appearing on a new-pool
        list almost immediately, and it is precisely then that its reading needs
        renewing.

        Deliberately narrow.

        *It asks by locator.* The caller supplies stable pool coordinates that
        came from a recorded observation, never a symbol, a token address or a
        name. Nothing here searches, ranks or chooses a pool, so there is no path
        by which a different market could answer for the one that was asked
        about.

        *It confirms rather than trusts.* Every returned pool is normalized
        through the same pipeline discovery uses and is then required to carry
        the pair identity that was requested. A pool that was not asked for is a
        contract breach and ends the read; one that was asked for and did not come
        back is simply absent from the result, and the caller reports it as
        unanswered rather than filling the gap with something older.

        *It stays bounded, and it does not reset the pass.* One request for up to
        `MAX_POOLS_PER_REQUEST` pools, under the transport's own budget. Results
        are added to what this adapter has already observed rather than replacing
        it, so a market seen by discovery and a market observed here are recorded
        through one and the same binding.
        """
        wanted: dict[str, PoolLocatorIdentity] = {}
        for locator in locators:
            wanted.setdefault(locator.pair_id(self.chain.name, "mainnet"), locator)
        if not wanted:
            return ()
        if len(wanted) > MAX_POOLS_PER_REQUEST:
            # Chunking here would turn one caller's budget into several requests
            # it never authorised. The caller decides how much to ask for.
            raise ConfigurationError()
        network_id = await self._directory.resolve(self.chain)
        addresses = [item.value for item in wanted.values()]
        if any(re.fullmatch(r"0x[0-9a-f]{40}|0x[0-9a-f]{64}", item) is None for item in addresses):
            # Locators are validated on construction, so this cannot normally
            # fail. It is checked anyway because the values are about to become
            # part of a URL path, and that is not a place to rely on an invariant
            # established somewhere else.
            raise IdentityError()
        payload = await self._transport.get(
            f"networks/{network_id}/pools/multi/{','.join(addresses)}",
            {"include": "base_token,quote_token,dex"},
        )
        at = self._clock.now()  # Successful fetch completion, never pool creation time.
        response = validate(PoolsResponse, payload)
        provenance(response, self.chain)
        included = index_included(response)
        trace = uuid4()
        confirmed: dict[str, MarketPair] = {}
        for raw in response.data:
            pool = validate(Pool, raw)
            snapshot = normalize(pool, included, self.chain, network_id, at, trace)
            pair_id = snapshot.pair.pair_id
            if pair_id not in wanted:
                # Something that was never asked about. Recording it would let a
                # provider decide which markets this system observes.
                raise IdentityError()
            if pair_id in confirmed:
                raise IdentityError()
            self._snapshots[snapshot.pair.market_identity] = snapshot
            confirmed[pair_id] = snapshot.pair
        return tuple(confirmed[key] for key in wanted if key in confirmed)
