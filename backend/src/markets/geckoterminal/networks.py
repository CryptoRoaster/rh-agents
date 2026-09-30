"""Canonical EVM identity is independent from verified provider network IDs."""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from src.core.config import Settings
from src.markets.geckoterminal.dto import NetworkResource, NetworksResponse
from src.markets.geckoterminal.errors import (
    BudgetError,
    ConfigurationError,
    ContractError,
    UnsupportedNetworkError,
)
from src.markets.geckoterminal.transport import GeckoTerminalTransport


@dataclass(frozen=True)
class Chain:
    name: str
    chain_id: int
    platform: str
    # Decimals of the chain's native asset. GeckoTerminal names that asset by the
    # zero address, and these decimals are what it must declare to be accepted
    # as it; see `adapter.token_address`.
    native_decimals: int = 18


CHAINS = {
    "robinhood": Chain("robinhood", 4663, "robinhood", native_decimals=18),
    "bsc": Chain("bsc", 56, "binance-smart-chain", native_decimals=18),
}


def validate[T: BaseModel](schema: type[T], value: object) -> T:
    try:
        return schema.model_validate(value)
    except ValidationError:
        raise ContractError() from None


def selected_chains(settings: Settings) -> tuple[Chain, ...]:
    names = settings.market_chains.split(",")
    if len(names) > settings.geckoterminal_max_chains or any(name not in CHAINS for name in names):
        raise ConfigurationError()
    return tuple(CHAINS[name] for name in names)


class VerifiedNetworkRegistry:
    """Chain → provider network id, for what one run has already validated.

    Holds nothing but the two strings, and only after `NetworkDirectory` has
    validated the binding the usual way — the paginated `/networks` answer, the
    configured id, its platform and the chain it must belong to. A failure is
    never written: an entry exists only because a validation succeeded.

    Scoped by whoever creates it. A PAPER run creates one per pass and hands it
    to each stage's own directory, so a later stage with its own transport and
    its own budget does not pay for the same paginated scan twice. No TTL, no
    persistence and no process-wide instance: when the pass ends, so does it.
    """

    def __init__(self) -> None:
        self._verified: dict[str, str] = {}

    def verified(self, chain: str) -> str | None:
        return self._verified.get(chain)

    def record(self, chain: str, network_id: str) -> None:
        self._verified[chain] = network_id

    def __len__(self) -> int:
        return len(self._verified)


class NetworkDirectory:
    """Pass-scoped validated network pages shared by both chain adapters.

    With a `VerifiedNetworkRegistry`, a chain some earlier directory of the same
    run already validated is answered from it without a request, and a chain
    this one validates is written to it — after, and only after, validation.
    Without one, every resolution is validated here, as before.
    """

    def __init__(
        self,
        transport: GeckoTerminalTransport,
        settings: Settings,
        *,
        registry: VerifiedNetworkRegistry | None = None,
    ) -> None:
        self._transport = transport
        self._max_pages = settings.geckoterminal_network_pages
        self._configured = {
            "bsc": settings.geckoterminal_bsc_network_id,
            "robinhood": settings.geckoterminal_robinhood_network_id,
        }
        self._registry = registry
        self._entries: dict[str, NetworkResource] = {}
        self._page = 1
        self._finished = False
        # How this directory answered: from the provider, or from the run's
        # registry. Counts only.
        self.resolved_by_provider = 0
        self.resolved_from_cache = 0

    async def resolve(self, chain: Chain) -> str:
        if CHAINS.get(chain.name) != chain:
            raise ConfigurationError()
        network_id = self._configured[chain.name]
        if self._registry is not None and self._registry.verified(chain.name) == network_id:
            # Validated earlier in this run, against this same configured id.
            self.resolved_from_cache += 1
            return network_id
        while network_id not in self._entries and not self._finished:
            if self._page > self._max_pages:
                # A bounded scan is not proof that a network does not exist.
                raise BudgetError()
            response = validate(
                NetworksResponse,
                await self._transport.get(
                    "networks",
                    {"page": self._page},
                ),
            )
            for entry in response.data:
                previous = self._entries.get(entry.id)
                if previous is not None and previous != entry:
                    raise ContractError()
                self._entries[entry.id] = entry
            self._page += 1
            # Never follow provider-supplied URLs. Only advance the bounded page number.
            self._finished = not response.data or (
                response.links is not None and response.links.next is None
            )
        resolved = self._entries.get(network_id)
        if resolved is None:
            raise UnsupportedNetworkError()
        if resolved.attributes.coingecko_asset_platform_id != chain.platform:
            raise UnsupportedNetworkError()
        self.resolved_by_provider += 1
        if self._registry is not None:
            self._registry.record(chain.name, network_id)
        return network_id
