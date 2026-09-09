from typing import Literal
from urllib.parse import unquote

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from src.core.models import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")
    database_url: str = Field(min_length=1)
    trading_mode: Literal[TradingMode.OBSERVE, TradingMode.PAPER] = TradingMode.OBSERVE

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
