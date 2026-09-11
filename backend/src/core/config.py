from decimal import Decimal
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    market_watcher_enabled: bool = False
    market_watch_interval_seconds: int = Field(default=90, ge=60, le=86400)
    evm_runtime_enabled: bool = False
    rh_chain_enabled: bool = False
    bsc_chain_enabled: bool = False
    rh_chain_id: Literal[4663] = 4663
    bsc_chain_id: Literal[56] = 56
    rh_rpc_http_url: SecretStr = SecretStr("")
    rh_rpc_ws_url: SecretStr = SecretStr("")
    bsc_rpc_http_url: SecretStr = SecretStr("")
    bsc_rpc_ws_url: SecretStr = SecretStr("")
    rh_confirmations_required: int = Field(default=12, ge=1, le=1000)
    bsc_confirmations_required: int = Field(default=12, ge=1, le=1000)
    evm_timeout_seconds: int = Field(default=15, ge=1, le=60)
    evm_retries: int = Field(default=2, ge=0, le=3)
    evm_retry_delay_seconds: int = Field(default=2, ge=0, le=10)
    evm_max_retry_delay_seconds: int = Field(default=15, ge=1, le=60)
    evm_reconnect_attempts: int = Field(default=5, ge=0, le=10)
    evm_queue_size: int = Field(default=64, ge=1, le=1024)
    evm_recovery_chunk_size: int = Field(default=100, ge=1, le=1000)
    evm_reorg_window: int = Field(default=64, ge=2, le=1000)
    evm_stale_seconds: int = Field(default=60, ge=5, le=600)
    # Phase 2B worker runtime. Disabled by default: no reasoning worker exists,
    # and the API process must never become a worker host implicitly.
    worker_runtime_enabled: bool = False
    worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_poll_interval_seconds: int = Field(default=5, ge=1, le=300)
    # Phase 2C reasoning. "disabled" is the default: booting the API must never
    # start paid model calls, and no reasoning worker runs implicitly.
    reasoning_provider: Literal["disabled", "fake", "anthropic"] = "disabled"
    reasoning_model: str = Field(default="claude-opus-5", min_length=1, max_length=80)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    reasoning_timeout_seconds: int = Field(default=60, ge=5, le=300)
    reasoning_max_output_tokens: int = Field(default=1024, ge=256, le=8192)
    anthropic_api_key: SecretStr = SecretStr("")
    orbit_worker_enabled: bool = False
    orbit_input_max_age_seconds: int = Field(default=900, ge=30, le=86400)
    orbit_discovery_liquidity_floor_usd: Decimal = Field(
        default=Decimal("25000"), ge=0, allow_inf_nan=False
    )
    # Phase 2D ATLAS. Disabled by default like every other worker; the core
    # safety policy is code-defined and versioned rather than env-mutable.
    atlas_worker_enabled: bool = False
    atlas_snapshot_max_age_seconds: int = Field(default=600, ge=30, le=86400)

    @model_validator(mode="after")
    def reasoning_configuration(self) -> "Settings":
        # A real provider is only usable once it is fully configured; enabling the
        # worker without credentials must fail loudly rather than at call time.
        if self.reasoning_provider == "anthropic" and not self.anthropic_api_key.get_secret_value():
            raise ValueError("The anthropic reasoning provider requires ANTHROPIC_API_KEY")
        if self.orbit_worker_enabled and self.reasoning_provider == "disabled":
            raise ValueError("ORBIT requires a configured reasoning provider")
        if self.atlas_worker_enabled and not self.evm_runtime_enabled:
            # ATLAS reads chain facts; enabling it without the EVM runtime would
            # guarantee an unavailable contract domain rather than fail loudly.
            raise ValueError("ATLAS requires the EVM runtime for on-chain facts")
        return self

    @model_validator(mode="after")
    def runtime_configuration(self) -> "Settings":
        if self.market_watcher_enabled:
            if self.market_provider != "geckoterminal":
                raise ValueError("Watcher requires geckoterminal")
            if self.market_watch_interval_seconds * 8 < self.geckoterminal_max_http_attempts * 60:
                raise ValueError(
                    "Watcher interval exceeds conservative eight-attempt/minute budget"
                )
        if self.evm_runtime_enabled:
            for prefix in ("rh", "bsc"):
                if not getattr(self, f"{prefix}_chain_enabled"):
                    continue
                for suffix, schemes in (("http", ("http", "https")), ("ws", ("ws", "wss"))):
                    secret: SecretStr = getattr(self, f"{prefix}_rpc_{suffix}_url")
                    try:
                        value = secret.get_secret_value()
                        parsed = urlsplit(value)
                        valid = (
                            parsed.scheme in schemes
                            and bool(parsed.hostname)
                            and not parsed.fragment
                            and not any(c.isspace() for c in value)
                            and (parsed.port is None or 1 <= parsed.port <= 65535)
                        )
                    except ValueError:
                        valid = False
                    if not valid:
                        raise ValueError(
                            "Enabled EVM chain requires valid HTTP and WSS configuration"
                        )
        return self

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
