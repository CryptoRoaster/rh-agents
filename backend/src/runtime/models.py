"""Provider-neutral, bounded EVM read contracts. No monetary float values."""

import re
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, model_validator

from src.core.config import Settings


class ErrorCode(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    CHAIN_ID_MISMATCH = "CHAIN_ID_MISMATCH"
    CONTRACT = "RPC_CONTRACT"
    RPC_ERROR = "RPC_ERROR"
    TIMEOUT = "TIMEOUT"
    CONNECTIVITY = "CONNECTIVITY"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHENTICATION = "AUTHENTICATION"
    CLIENT = "HTTP_CLIENT"
    UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    OWNERSHIP = "OWNERSHIP_UNAVAILABLE"
    CONFLICT = "EVENT_CONFLICT"
    CURSOR_RACE = "CURSOR_RACE"
    REORG = "REORG_DETECTED"
    DEEP_REORG = "REORG_BEYOND_WINDOW"
    OVERFLOW = "BACKPRESSURE_OVERFLOW"
    STOPPED = "STOPPED"


class RuntimeFailure(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class HealthState(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


def quantity(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", value
    ):
        raise RuntimeFailure(ErrorCode.CONTRACT)
    result = int(value, 16)
    if result > 2**63 - 1:
        raise RuntimeFailure(ErrorCode.CONTRACT)
    return result


def hex_data(value: object, size: int | None = None, *, nonzero: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 131074
        or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value)
        or (size is not None and len(value) != 2 + size * 2)
        or (nonzero and not int(value[2:] or "0", 16))
    ):
        raise ValueError("Invalid EVM byte data")
    return value.lower()


Address = Annotated[str, BeforeValidator(lambda v: hex_data(v, 20, nonzero=True))]
Hash = Annotated[str, BeforeValidator(lambda v: hex_data(v, 32))]
Data = Annotated[str, BeforeValidator(hex_data)]
ChainName = Literal["robinhood", "bsc"]


class Immutable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ChainConfig(Immutable):
    chain: ChainName
    network: Literal["mainnet"] = "mainnet"
    chain_id: int
    http_url: SecretStr
    ws_url: SecretStr
    confirmations: int = Field(ge=1)

    @model_validator(mode="after")
    def identity(self) -> "ChainConfig":
        if self.chain_id != {"robinhood": 4663, "bsc": 56}[self.chain]:
            raise ValueError("Invalid canonical chain identity")
        for secret, schemes in ((self.http_url, ("http", "https")), (self.ws_url, ("ws", "wss"))):
            value = secret.get_secret_value()
            if not value:
                continue  # Isolated HTTP/WSS diagnostics may configure just one transport.
            try:
                url = urlsplit(value)
                valid = (
                    url.scheme in schemes
                    and bool(url.hostname)
                    and not url.fragment
                    and not any(c.isspace() for c in value)
                    and (url.port is None or 1 <= url.port <= 65535)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Invalid secret endpoint configuration")
        return self


def chain_configs(settings: Settings) -> tuple[ChainConfig, ...]:
    result = []
    for chain, prefix in (("robinhood", "rh"), ("bsc", "bsc")):
        if settings.evm_runtime_enabled and getattr(settings, f"{prefix}_chain_enabled"):
            result.append(
                ChainConfig.model_validate(
                    {
                        "chain": chain,
                        "chain_id": getattr(settings, f"{prefix}_chain_id"),
                        "http_url": getattr(settings, f"{prefix}_rpc_http_url"),
                        "ws_url": getattr(settings, f"{prefix}_rpc_ws_url"),
                        "confirmations": getattr(settings, f"{prefix}_confirmations_required"),
                    }
                )
            )
    return tuple(result)


class Head(Immutable):
    number: int = Field(strict=True, ge=0, le=2**63 - 1)
    hash: Hash
    parent_hash: Hash
    timestamp: int = Field(strict=True, ge=0)

    @classmethod
    def from_rpc(cls, value: object) -> "Head":
        if not isinstance(value, dict):
            raise RuntimeFailure(ErrorCode.CONTRACT)
        try:
            return cls.model_validate(
                dict(
                    number=quantity(value.get("number")),
                    hash=value.get("hash"),
                    parent_hash=value.get("parentHash"),
                    timestamp=quantity(value.get("timestamp")),
                )
            )
        except ValueError:
            raise RuntimeFailure(ErrorCode.CONTRACT) from None


class SubscriptionSpec(Immutable):
    chain: ChainName
    addresses: tuple[Address, ...] = Field(min_length=1, max_length=20)
    topics: tuple[Hash | None, ...] = Field(min_length=1, max_length=4)
    decoder: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")

    @model_validator(mode="after")
    def bounded_filter(self) -> "SubscriptionSpec":
        if self.topics[0] is None or len(set(self.addresses)) != len(self.addresses):
            raise ValueError("An explicit topic0 and unique address allowlist are required")
        return self

    def rpc_filter(self) -> dict[str, object]:
        return {"address": list(self.addresses), "topics": list(self.topics)}


class Log(Immutable):
    block_number: int = Field(strict=True, ge=0)
    block_hash: Hash
    transaction_hash: Hash
    transaction_index: int = Field(strict=True, ge=0)
    log_index: int = Field(strict=True, ge=0)
    address: Address
    topics: tuple[Hash, ...] = Field(max_length=4)
    data: Data

    @classmethod
    def from_rpc(cls, value: object) -> "Log":
        if not isinstance(value, dict) or value.get("removed", False) is not False:
            raise RuntimeFailure(ErrorCode.CONTRACT)
        try:
            return cls.model_validate(
                dict(
                    block_number=quantity(value.get("blockNumber")),
                    block_hash=value.get("blockHash"),
                    transaction_hash=value.get("transactionHash"),
                    transaction_index=quantity(value.get("transactionIndex")),
                    log_index=quantity(value.get("logIndex")),
                    address=value.get("address"),
                    topics=value.get("topics"),
                    data=value.get("data"),
                )
            )
        except ValueError:
            raise RuntimeFailure(ErrorCode.CONTRACT) from None

    def matches(self, spec: SubscriptionSpec) -> bool:
        return (
            self.address in spec.addresses
            and len(self.topics) >= len(spec.topics)
            and all(
                expected is None or self.topics[index] == expected
                for index, expected in enumerate(spec.topics)
            )
        )
