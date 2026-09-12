"""Explicit fixtures only. No network calls or claims of live market connectivity."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from src.markets.history import (
    MarketBar,
    MarketHistory,
    coverage_for,
    empty_history,
    interval_seconds,
)
from src.markets.models import (
    AssetIdentity,
    Availability,
    LiquiditySnapshot,
    MarketIdentity,
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


def fixture_history(
    identity: MarketIdentity,
    *,
    newest_close: datetime,
    bars: int,
    timeframe: str = "hour",
    aggregate: int = 1,
    requested_bars: int | None = None,
    price: Decimal = Decimal("1.00"),
    fetched_at: datetime | None = None,
    skip: frozenset[int] = frozenset(),
) -> MarketHistory:
    """A deterministic closed series, shaped exactly like a provider's.

    Every bar is derived from its own index, so the same arguments always produce
    the same numbers and a test can point at one bar and change only that. The
    gentle sawtooth gives the window a real high and a real low to be grounded
    against rather than a flat line that would make every level equidistant.

    ``skip`` omits intervals by index, reproducing what the provider does when
    nobody traded: the bar is absent, not flat.
    """
    step = interval_seconds(timeframe, aggregate)
    requested = requested_bars if requested_bars is not None else bars
    built: list[MarketBar] = []
    for index in range(bars):
        if index in skip:
            continue
        # Oldest first; the newest bar closes at newest_close.
        opened_at = newest_close - timedelta(seconds=step * (bars - index))
        drift = (Decimal(index % 5) - Decimal(2)) / Decimal(100)
        close = price * (Decimal(1) + drift)
        open_price = price * (Decimal(1) + drift / Decimal(2))
        built.append(
            MarketBar(
                opened_at=opened_at,
                interval_seconds=step,
                open=open_price,
                high=max(open_price, close) * Decimal("1.01"),
                low=min(open_price, close) * Decimal("0.99"),
                close=close,
                volume=Decimal(1000 + index),
            )
        )
    if not built:
        return empty_history(
            pair_id=identity.pair_id,
            provider="fixture:memory",
            chain=identity.chain,
            network=identity.network,
            venue=identity.venue,
            base_asset_id=identity.base_asset_id,
            quote_asset_id=identity.quote_asset_id,
            timeframe=timeframe,
            aggregate=aggregate,
            requested_bars=requested,
            fetched_at=fetched_at or newest_close,
            is_fixture=True,
        )
    return MarketHistory(
        pair_id=identity.pair_id,
        provider="fixture:memory",
        chain=identity.chain,
        network=identity.network,
        venue=identity.venue,
        base_asset_id=identity.base_asset_id,
        quote_asset_id=identity.quote_asset_id,
        timeframe=timeframe,  # type: ignore[arg-type]
        aggregate=aggregate,
        bars=tuple(built),
        requested_bars=requested,
        coverage=coverage_for(built, requested, step),
        observed_at=built[-1].closed_at,
        fetched_at=fetched_at or built[-1].closed_at,
        is_fixture=True,
    )


class InMemoryHistorySource:
    """Returns one prepared series. Holds no transport of any kind."""

    def __init__(self, history: MarketHistory | Exception) -> None:
        self._history = history

    async def history(
        self, identity: object, *, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory:
        if isinstance(self._history, Exception):
            raise self._history
        return self._history
