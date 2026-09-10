"""Explicit fixtures only. No network calls or claims of live market connectivity."""

from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from src.markets.models import (
    AssetIdentity,
    Availability,
    LiquiditySnapshot,
    MarketPair,
    MarketSnapshot,
    PriceSnapshot,
    VolumeSnapshot,
)


def fixture_snapshot(observed_at: datetime, correlation_id: UUID) -> MarketSnapshot:
    def identity(label: str) -> UUID:
        return uuid5(
            NAMESPACE_URL, f"rh-agents:fixture:{observed_at.isoformat()}:{correlation_id}:{label}"
        )

    base_id = "ethereum:mainnet:0xfixture-weth"
    quote_id = "ethereum:mainnet:0xfixture-usdc"
    base = AssetIdentity(
        id=identity("base"),
        observed_at=observed_at,
        provider="fixture:memory",
        chain="ethereum",
        network="mainnet",
        asset_id=base_id,
        correlation_id=correlation_id,
        is_fixture=True,
        symbol="WETH",
        decimals=18,
    )
    quote = base.model_copy(
        update={"id": identity("quote"), "asset_id": quote_id, "symbol": "USDC", "decimals": 6}
    )
    meta = base.model_dump(exclude={"symbol", "decimals", "id"})
    pair = MarketPair(
        **meta,
        id=identity("pair"),
        pair_id="ethereum:mainnet:fixture-weth-usdc",
        base=base,
        quote=quote,
        venue="fixture:spot",
    )
    return MarketSnapshot(
        **meta,
        id=identity("snapshot"),
        pair=pair,
        price=PriceSnapshot(
            **meta,
            id=identity("price"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("2345.123456789012345678"),
        ),
        liquidity=LiquiditySnapshot(
            **meta,
            id=identity("liquidity"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("12500000.25"),
        ),
        volume=VolumeSnapshot(
            **meta,
            id=identity("volume"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("3400000.50"),
            window_seconds=86400,
        ),
    )


class InMemoryProvider:
    provider = "fixture:memory"
    is_fixture = True

    def __init__(self, observations: tuple[MarketSnapshot, ...]) -> None:
        self._observations = tuple(
            MarketSnapshot.model_validate(o.model_dump()) for o in observations
        )
        if any(o.provider != self.provider or not o.is_fixture for o in self._observations):
            raise ValueError("InMemoryProvider accepts explicitly labeled fixtures only")

    async def discover(self) -> tuple[MarketPair, ...]:
        pairs = {o.pair.pair_id: o.pair for o in self._observations}
        return tuple(pairs[key] for key in sorted(pairs))

    async def snapshot(self, pair: MarketPair) -> MarketSnapshot:
        matches = [o for o in self._observations if o.pair.pair_id == pair.pair_id]
        if not matches:
            raise LookupError("Pair not found in fixture provider")
        return max(matches, key=lambda o: (o.observed_at, str(o.id)))

    async def price(self, pair: MarketPair) -> PriceSnapshot:
        return (await self.snapshot(pair)).price

    async def liquidity(self, pair: MarketPair) -> LiquiditySnapshot:
        return (await self.snapshot(pair)).liquidity

    async def volume(self, pair: MarketPair) -> VolumeSnapshot:
        return (await self.snapshot(pair)).volume
