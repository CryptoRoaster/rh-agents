from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.models import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")
    database_url: str = "postgresql+asyncpg://rh_agents@localhost:5432/rh_agents"
    trading_mode: Literal[TradingMode.OBSERVE, TradingMode.PAPER] = TradingMode.OBSERVE
