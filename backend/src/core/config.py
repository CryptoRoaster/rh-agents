from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from src.core.models import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore", hide_input_in_errors=True)
    database_url: str = Field(min_length=1)
    market_max_age_seconds: int = Field(default=60, gt=0, le=3600)
    market_provider: Literal["fixture", "geckoterminal"] = "fixture"
    market_chains: str = "robinhood,bsc"
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_api_version: Literal["20230203"] = "20230203"
    geckoterminal_robinhood_network_id: str = "robinhood"
    geckoterminal_bsc_network_id: str = "bsc"
    geckoterminal_max_chains: int = Field(default=2, ge=1, le=2)
    geckoterminal_pools_per_chain: int = Field(default=3, ge=1, le=20)
    geckoterminal_max_detail_lookups: Literal[0] = 0
    geckoterminal_max_requests: int = Field(default=5, ge=1, le=10)
    geckoterminal_max_http_attempts: int = Field(default=8, ge=1, le=10)
    geckoterminal_max_concurrency: int = Field(default=1, ge=1, le=2)
    geckoterminal_network_pages: int = Field(default=3, ge=1, le=10)
    geckoterminal_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    geckoterminal_read_timeout_seconds: int = Field(default=10, ge=1, le=60)
    geckoterminal_total_timeout_seconds: int = Field(default=30, ge=1, le=120)
    geckoterminal_retries: int = Field(default=1, ge=0, le=2)
    geckoterminal_retry_delay_seconds: int = Field(default=2, ge=0, le=10)
    geckoterminal_max_retry_after_seconds: int = Field(default=5, ge=0, le=30)
    trading_mode: Literal[TradingMode.OBSERVE, TradingMode.PAPER] = TradingMode.OBSERVE

    @field_validator("market_chains")
    @classmethod
    def require_target_chains(cls, value: str) -> str:
        chains = [item.strip() for item in value.split(",")]
        if (
            not chains
            or len(set(chains)) != len(chains)
            or any(item not in ("robinhood", "bsc") for item in chains)
        ):
            raise ValueError("MARKET_CHAINS must contain unique robinhood/bsc chain names")
        return ",".join(chains)

    @field_validator("geckoterminal_base_url")
    @classmethod
    def require_public_origin(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") != "/api/v2"
            or (url.port is not None and not 1 <= url.port <= 65535)
            or any(c.isspace() for c in value)
        ):
            raise ValueError("GeckoTerminal requires an HTTPS /api/v2 URL without credentials")
        return value.rstrip("/")

    @field_validator("geckoterminal_robinhood_network_id", "geckoterminal_bsc_network_id")
    @classmethod
    def require_network_id(cls, value: str) -> str:
        if (
            not value
            or len(value) > 60
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in value)
        ):
            raise ValueError("Invalid provider network identifier")
        return value

    @field_validator("database_url")
    @classmethod
    def require_postgresql_asyncpg(cls, value: str) -> str:
        message = "DATABASE_URL must be a postgresql+asyncpg URL with a database name"
        if any(character.isspace() for character in value) or "#" in value:
            raise ValueError(message)
        try:
            url = make_url(value)
        except (ArgumentError, ValueError) as error:
            raise ValueError(message) from error
        if (
            url.drivername != "postgresql+asyncpg"
            or not url.database
            or not unquote(url.database).strip()
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError(message)
        return value
