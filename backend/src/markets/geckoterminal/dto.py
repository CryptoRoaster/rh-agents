"""Used subset of GeckoTerminal V2 JSON:API, version 20230203.

Additive fields are ignored. Optional provenance guards reject conflicting metadata;
they are local validation rules, not invented provider measurements.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)

from src.markets.models import Amount

# A calendar date and a time of day, as ISO 8601 writes them: a bare number,
# which pydantic would read as Unix seconds, is not a stated creation time.
ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

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
    # The pool's quote token in USD, as the provider states it for this same
    # pool. Read only as given: never derived from the base price, the reserve
    # ratio, a symbol or an assumed peg.
    quote_token_price_usd: Amount | None = None
    reserve_in_usd: Amount | None = None
    volume_usd: Volume | None = None
    # When the provider says the pool was created. Only an ISO 8601 string with
    # an explicit offset is a point in time; anything else — absent, null, a
    # number, a naive or malformed string — is UNKNOWN (None), never a guess,
    # and never a reason to drop the pool.
    pool_created_at: datetime | None = None

    @field_validator("pool_created_at", mode="wrap")
    @classmethod
    def creation_time(cls, value: object, handler: ValidatorFunctionWrapHandler) -> datetime | None:
        if not isinstance(value, str) or len(value) > 64 or ISO_INSTANT.match(value) is None:
            return None
        try:
            parsed = handler(value)
        except ValidationError:
            return None
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            return None
        return parsed


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
