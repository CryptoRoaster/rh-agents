"""Which recorded market a reading must come from, stated once.

A pool is not a market. Two providers observing one pool are two sources, a
pair id means nothing across chains or networks, and a reading whose assets,
venue or fixture flag differ is a reading of something else. Everything that
must judge a *held* market — its mark, its liquidity, the sale itself — asks
for that market by its full identity through a `MarketScope`, and never takes
whatever was recorded last under the pool's id.

`describes_market` is the one equality rule. It has exactly one exception, the
one the scout's watch identity has always had: an identity recorded before
pool locators existed has none, and a reading that adds one for the otherwise
identical market completes it rather than contradicting it.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from src.core.models import Position
from src.markets.models import MarketIdentity, MarketSnapshot


def describes_market(observed: MarketIdentity, expected: MarketIdentity) -> bool:
    """Whether `observed` is a reading of exactly the `expected` market."""
    if observed == expected:
        return True
    return expected.pool_locator is None and expected == observed.model_copy(
        update={"pool_locator": None}
    )


@dataclass(frozen=True)
class MarketScope:
    """The market a reading must be of.

    Built from a case's full `MarketIdentity`, or from what a position recorded
    about the market it was bought in. A field a legacy position never recorded
    (None) is not constrained — it was never known — and the pool always is.
    """

    pair_id: str
    provider: str | None = None
    chain: str | None = None
    network: str | None = None
    base_asset_id: str | None = None
    identity: MarketIdentity | None = None

    @classmethod
    def of(cls, identity: MarketIdentity) -> "MarketScope":
        return cls(
            pair_id=identity.pair_id,
            provider=identity.provider,
            chain=identity.chain,
            network=identity.network,
            base_asset_id=identity.base_asset_id,
            identity=identity,
        )

    @classmethod
    def held(cls, position: Position) -> "MarketScope | None":
        if position.market_pair_id is None:
            return None
        return cls(
            pair_id=position.market_pair_id,
            provider=position.market_provider,
            chain=position.market_chain,
            network=position.market_network,
            base_asset_id=position.asset_id,
        )

    def matches_identity(self, identity: MarketIdentity) -> bool:
        """Whether a full identity agrees with every field this scope knows."""
        for recorded, named in (
            (identity.pair_id, self.pair_id),
            (identity.provider, self.provider),
            (identity.chain, self.chain),
            (identity.network, self.network),
            (identity.base_asset_id, self.base_asset_id),
        ):
            if named is not None and recorded != named:
                return False
        return self.identity is None or describes_market(identity, self.identity)

    def matches(self, snapshot: MarketSnapshot) -> bool:
        return self.matches_identity(snapshot.pair.market_identity)


class ScopedMarketInput(Protocol):
    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


async def latest_in(
    markets: Any, scope: MarketScope, *, include_fixtures: bool = False
) -> MarketSnapshot | None:
    """The newest current reading of exactly this market, or None.

    A port that can select by scope is asked to; any other port is asked for
    the pool and its answer accepted only if it is of this market. Never a
    fallback to another source's reading.
    """
    select = getattr(markets, "latest_in", None)
    if select is not None:
        found: MarketSnapshot | None = await select(scope, include_fixtures=include_fixtures)
    else:
        found = await markets.latest(scope.pair_id, include_fixtures=include_fixtures)
    if found is None or not scope.matches(found):
        return None
    return found


@dataclass(frozen=True)
class HeldMarketFeed:
    """A market port on which the held pool's id always means the held market.

    For one exit: every reader that asks the port about the held pool by its
    id — the completeness check included — gets the held market's own reading
    or none, so the basis cannot describe two sources.
    """

    markets: Any
    scope: MarketScope

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        if identity == self.scope.pair_id:
            return await latest_in(self.markets, self.scope, include_fixtures=include_fixtures)
        found: MarketSnapshot | None = await self.markets.latest(
            identity, include_fixtures=include_fixtures
        )
        return found

    async def latest_in(
        self, scope: MarketScope, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        return await latest_in(self.markets, scope, include_fixtures=include_fixtures)


__all__ = ["HeldMarketFeed", "MarketScope", "describes_market", "latest_in"]
