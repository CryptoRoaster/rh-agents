"""Adapters normalize external payloads here; agents never receive transport clients."""

from collections.abc import Mapping
from typing import Protocol

from src.markets.models import (
    LiquiditySnapshot,
    MarketPair,
    MarketSnapshot,
    PriceSnapshot,
    VolumeSnapshot,
)


class DiscoveryProvider(Protocol):
    async def discover(self) -> tuple[MarketPair, ...]: ...


class SnapshotProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def is_fixture(self) -> bool: ...

    async def snapshot(self, pair: MarketPair) -> MarketSnapshot: ...


class MarketDataProvider(Protocol):
    async def price(self, pair: MarketPair) -> PriceSnapshot: ...
    async def liquidity(self, pair: MarketPair) -> LiquiditySnapshot: ...
    async def volume(self, pair: MarketPair) -> VolumeSnapshot: ...


class MarketProvider(DiscoveryProvider, SnapshotProvider, Protocol):
    pass


def normalize_snapshot(
    payload: Mapping[str, object], *, provider: str, is_fixture: bool
) -> MarketSnapshot:
    observation = MarketSnapshot.model_validate(payload)
    if observation.provider != provider or observation.is_fixture != is_fixture:
        raise ValueError("Adapter provenance does not match normalized observation")
    return observation
