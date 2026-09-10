"""Normalized observations, distinct from SENTINEL's execution evidence contracts."""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BeforeValidator, Field, model_validator

from src.core.models import Contract


def exact_number(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use Decimal or decimal strings, never floating-point values")
    return value


Amount = Annotated[
    Decimal,
    BeforeValidator(exact_number),
    Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18),
]
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]
Namespace = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[a-zA-Z0-9_-]+$")]


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class Observation(Contract):
    id: UUID
    observed_at: AwareDatetime
    provider: Name
    chain: Namespace
    network: Namespace
    asset_id: Name
    correlation_id: UUID
    is_fixture: bool = Field(strict=True)

    @model_validator(mode="after")
    def qualified_identity(self) -> Self:
        prefix = f"{self.chain}:{self.network}:"
        if not self.asset_id.startswith(prefix) or len(self.asset_id) == len(prefix):
            raise ValueError("asset_id must be chain:network:asset")
        return self

    def age(self, now: datetime) -> timedelta:
        if now.utcoffset() is None:
            raise ValueError("Freshness requires timezone-aware time")
        return now - self.observed_at

    def is_fresh(self, now: datetime, max_age: timedelta) -> bool:
        return timedelta(0) <= self.age(now) <= max_age


class AssetIdentity(Observation):
    symbol: Name
    decimals: int | None = Field(default=None, strict=True, ge=0, le=36)


class MarketIdentity(Contract):
    """Stable market coordinates, independent of discovery/snapshot event metadata."""

    provider: Name
    chain: Namespace
    network: Namespace
    pair_id: Name
    base_asset_id: Name
    quote_asset_id: Name
    venue: Name
    is_fixture: bool = Field(strict=True)


class MarketPair(Observation):
    pair_id: Name
    base: AssetIdentity
    quote: AssetIdentity
    venue: Name

    @property
    def market_identity(self) -> MarketIdentity:
        return MarketIdentity(
            provider=self.provider,
            chain=self.chain,
            network=self.network,
            pair_id=self.pair_id,
            base_asset_id=self.base.asset_id,
            quote_asset_id=self.quote.asset_id,
            venue=self.venue,
            is_fixture=self.is_fixture,
        )

    @model_validator(mode="after")
    def pair_identity(self) -> Self:
        prefix = f"{self.chain}:{self.network}:"
        if not self.pair_id.startswith(prefix) or len(self.pair_id) == len(prefix):
            raise ValueError("pair_id must be chain:network:pair")
        if self.asset_id != self.base.asset_id or self.base.asset_id == self.quote.asset_id:
            raise ValueError("Pair must identify distinct base and quote assets")
        for asset in (self.base, self.quote):
            same_provenance(self, asset, same_asset=False)
        return self


class Measurement(Observation):
    status: Availability = Availability.UNKNOWN
    value_usd: Amount | None = None

    @model_validator(mode="after")
    def availability_matches_value(self) -> Self:
        if (self.status == Availability.AVAILABLE) != (self.value_usd is not None):
            raise ValueError("AVAILABLE requires a value; unknown/unavailable values must be null")
        return self


class PriceSnapshot(Measurement):
    @model_validator(mode="after")
    def positive_price(self) -> Self:
        if self.value_usd is not None and self.value_usd <= 0:
            raise ValueError("Available price must be positive")
        return self


class LiquiditySnapshot(Measurement):
    pass


class VolumeSnapshot(Measurement):
    window_seconds: int = Field(strict=True, gt=0, le=604800)


def same_provenance(parent: Observation, child: Observation, *, same_asset: bool = True) -> None:
    fields: tuple[str, ...] = ("provider", "chain", "network", "correlation_id", "is_fixture")
    if same_asset:
        fields += ("asset_id",)
    if any(getattr(parent, field) != getattr(child, field) for field in fields):
        raise ValueError("Nested observations must preserve identity and provenance")
    if child.observed_at > parent.observed_at:
        raise ValueError("Nested observation cannot be newer than its enclosing observation")


class MarketSnapshot(Observation):
    schema_version: Literal[1] = 1
    pair: MarketPair
    price: PriceSnapshot
    liquidity: LiquiditySnapshot
    volume: VolumeSnapshot

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        for child in (self.pair, self.price, self.liquidity, self.volume):
            same_provenance(self, child)
        return self

    @property
    def freshness_at(self) -> datetime:
        return min(
            self.observed_at,
            self.price.observed_at,
            self.liquidity.observed_at,
            self.volume.observed_at,
        )

    @property
    def available(self) -> bool:
        # Volume may be unknown; this is a data candidate, never a risk approval.
        return (
            self.price.status == Availability.AVAILABLE
            and self.liquidity.status == Availability.AVAILABLE
        )

    def is_valid_at(self, now: datetime, max_age: timedelta) -> bool:
        return self.available and all(
            item.is_fresh(now, max_age) for item in (self, self.price, self.liquidity, self.volume)
        )


class MarketCandidate(Observation):
    pair_id: Name
    snapshot_id: UUID

    # A recorded-data reference, not a recommendation or tradability assertion.
    @classmethod
    def from_snapshot(cls, snapshot: MarketSnapshot) -> "MarketCandidate":
        return cls(
            id=uuid5(NAMESPACE_URL, f"rh-agents:candidate:{snapshot.id}"),
            observed_at=snapshot.observed_at,
            provider=snapshot.provider,
            chain=snapshot.chain,
            network=snapshot.network,
            asset_id=snapshot.asset_id,
            correlation_id=snapshot.correlation_id,
            is_fixture=snapshot.is_fixture,
            pair_id=snapshot.pair.pair_id,
            snapshot_id=snapshot.id,
        )

    @model_validator(mode="after")
    def qualified_pair(self) -> Self:
        prefix = f"{self.chain}:{self.network}:"
        if not self.pair_id.startswith(prefix) or len(self.pair_id) == len(prefix):
            raise ValueError("pair_id must be chain:network:pair")
        return self
