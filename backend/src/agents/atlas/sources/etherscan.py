"""Etherscan V2 adapter: BNB Smart Chain contract creation provenance.

Etherscan V2 addresses BNB Smart Chain by ``chainid=56`` and serves the same
``getcontractcreation`` response shape as Blockscout, so the strict parser is
shared and the two chains cannot drift apart in how a creator is read.

Only contract creation is taken from here. Etherscan's holder list is a paid
tier endpoint with no documented ordering guarantee, which would leave a top-N
prefix unprovable, so holder intelligence deliberately does not come from it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from src.agents.atlas.models import AtlasSourceFailure, OriginFacts
from src.agents.atlas.sources.creation import parse_creation
from src.agents.atlas.sources.http import SourceRequestError, SourceTransport
from src.markets.models import Availability


@dataclass(frozen=True)
class EtherscanConfig:
    base_url: str
    chain_id: int
    api_key: str
    timeout_seconds: int = 10
    max_requests: int = 2


def transport_for(config: EtherscanConfig) -> SourceTransport:
    # Etherscan requires the key as a query parameter. The transport never logs a
    # URL, httpx and httpcore loggers are filtered at construction, and the typed
    # failure carries a category only — so the key stays out of logs, tracebacks,
    # evidence and API responses.
    return SourceTransport(
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests,
    )


@dataclass(frozen=True)
class EtherscanContractOriginSource:
    config: EtherscanConfig
    chain: str = "bsc"
    transport_factory: Callable[[EtherscanConfig], SourceTransport] = field(default=transport_for)
    source_name: str = "etherscan-v2"

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
                    "chainid": self.config.chain_id,
                    "module": "contract",
                    "action": "getcontractcreation",
                    "contractaddresses": token_address,
                    "apikey": self.config.api_key,
                },
            )
            return parse_creation(payload, token_address, self.source)
        except SourceRequestError as error:
            return OriginFacts(
                status=Availability.UNAVAILABLE, failure=error.failure, source=self.source
            )
        finally:
            await transport.aclose()
