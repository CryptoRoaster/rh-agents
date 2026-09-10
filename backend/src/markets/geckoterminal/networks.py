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


CHAINS = {
    "robinhood": Chain("robinhood", 4663, "robinhood"),
    "bsc": Chain("bsc", 56, "binance-smart-chain"),
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


class NetworkDirectory:
    """Pass-scoped validated network pages shared by both chain adapters."""

    def __init__(self, transport: GeckoTerminalTransport, settings: Settings) -> None:
        self._transport = transport
        self._max_pages = settings.geckoterminal_network_pages
        self._configured = {
            "bsc": settings.geckoterminal_bsc_network_id,
            "robinhood": settings.geckoterminal_robinhood_network_id,
        }
        self._entries: dict[str, NetworkResource] = {}
        self._page = 1
        self._finished = False

    async def resolve(self, chain: Chain) -> str:
        if CHAINS.get(chain.name) != chain:
            raise ConfigurationError()
        network_id = self._configured[chain.name]
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
        return network_id
