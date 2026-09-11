"""Used subset of GeckoTerminal V2 JSON:API, version 20230203.

Additive fields are ignored. Optional provenance guards reject conflicting metadata;
they are local validation rules, not invented provider measurements.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.markets.models import Amount

Text = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Address = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-fA-F]{40}$")]


class DTO(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


class Provenance(DTO):
    provider: Literal["geckoterminal"] = "geckoterminal"
    network: Literal["mainnet"] = "mainnet"
    chain: Literal["robinhood", "bsc"] | None = None
    chain_id: int | None = Field(default=None, strict=True)
    is_fixture: bool = Field(default=False, strict=True)

    @field_validator("is_fixture")
    @classmethod
    def real_data(cls, value: bool) -> bool:
        if value:
            raise ValueError("Provider data must not claim fixture provenance")
        return value


class NetworkAttributes(DTO):
    name: Text
    coingecko_asset_platform_id: Text | None = None


class NetworkResource(DTO):
    id: Text
    type: Literal["network"]
    attributes: NetworkAttributes


class Links(DTO):
    next: Text | None


class NetworksResponse(DTO):
    data: list[NetworkResource] = Field(max_length=1000)
    links: Links | None = None


class Reference(DTO):
    id: Text
    type: Literal["token", "dex", "network"]


class Relationship(DTO):
    data: Reference


class Relationships(DTO):
    base_token: Relationship
    quote_token: Relationship
    dex: Relationship
    network: Relationship | None = None


class Volume(DTO):
    h24: Amount | None = None


class PoolAttributes(Provenance):
    address: Annotated[str, Field(strict=True, pattern=r"^0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")]
    base_token_price_usd: Amount | None = None
    reserve_in_usd: Amount | None = None
    volume_usd: Volume | None = None


class Pool(Provenance):
    id: Text
    type: Literal["pool"]
    attributes: PoolAttributes
    relationships: Relationships


class TokenAttributes(Provenance):
    address: Address
    symbol: Text
    decimals: int | None = Field(default=None, strict=True, ge=0, le=36)


class Token(Provenance):
    id: Text
    type: Literal["token"]
    attributes: TokenAttributes


class PoolsResponse(Provenance):
    data: list[object] = Field(max_length=20)
    included: list[object] = Field(default_factory=list, max_length=100)
