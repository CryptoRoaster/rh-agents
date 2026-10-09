"""The narrow read that prices open PAPER positions.

One port, read-only: the recorded market layer. No provider client, no
transport, no session and no way to ask a different question. Nothing here
writes, and nothing here decides anything about risk — it establishes what the
portfolio is currently worth, or says which holding it could not establish that
for.

Deliberately performed *before* the account lock is taken. The reads are bounded
DB reads against recorded observations, but holding a portfolio-wide lock across
any injected port is a latency somebody else pays for. What that costs is the
possibility of a position appearing in between, and the callers close it by
comparing what was valued against what they find under the lock.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import Position
from src.markets.models import Availability, MarketIdentity, MarketSnapshot
from src.markets.scope import MarketScope, latest_in
from src.orchestration.valuation.models import (
    PortfolioValuation,
    PositionMark,
    UnvaluedPosition,
    ValuationRefusal,
)


class ValuationMarketInput(Protocol):
    """Recorded market observations only: no writes, no provider, no transport."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


@dataclass(frozen=True)
class PositionValuationReader:
    """Prices every open position from the market it was acquired in."""

    markets: ValuationMarketInput
    # The bound the evaluation will judge these under. Read from the same limits
    # object SENTINEL uses, never a second tolerance chosen here.
    max_age_seconds: int
    include_fixtures: bool = False
    # The full identity of each holding's market, by asset, where the case that
    # bought it is known (`held_market_identities`). A position records its
    # pool, chain, network, provider and asset but not its quote asset or
    # venue; with the case's identity the mark is read from exactly that
    # market's stream, without it from the position's partial scope.
    identities: Mapping[str, MarketIdentity] = field(default_factory=dict)

    async def value(self, positions: list[Position], now: datetime) -> PortfolioValuation:
        """Mark every non-zero holding, or name the ones that could not be.

        `now` is the instant freshness is judged at. It is passed in rather than
        read here, because the caller owns the one instant everything else in
        its decision is measured against.
        """
        marks: list[PositionMark] = []
        unvalued: list[UnvaluedPosition] = []
        considered: list[str] = []
        for holding in sorted(positions, key=lambda item: item.asset_id):
            if holding.quantity == 0:
                # Nothing held, nothing to value. A closed position contributes
                # no exposure and needs no price.
                continue
            considered.append(holding.asset_id)
            outcome = await self._mark(holding, now)
            if isinstance(outcome, PositionMark):
                marks.append(outcome)
            else:
                unvalued.append(
                    UnvaluedPosition(
                        asset_id=holding.asset_id,
                        reason=outcome,
                        pair_id=holding.market_pair_id,
                    )
                )
        return PortfolioValuation(
            marks=tuple(marks),
            unvalued=tuple(unvalued),
            considered_assets=tuple(considered),
        )

    async def _mark(self, holding: Position, now: datetime) -> PositionMark | ValuationRefusal:
        pair_id = holding.market_pair_id
        if pair_id is None:
            # The position never recorded which market it came from. Choosing
            # one for it would resolve an ambiguity silently, in the one place
            # where being wrong misprices the whole portfolio.
            return ValuationRefusal.POSITION_MARKET_UNKNOWN
        held = MarketScope.held(holding)
        assert held is not None  # pair_id is known here
        identity = self.identities.get(holding.asset_id)
        if identity is not None and not held.matches_identity(identity):
            # The case that bought this holding names another market than the
            # holding records. One of the two is wrong; neither is guessed.
            return ValuationRefusal.MARKET_IDENTITY_MISMATCH
        scope = held if identity is None else MarketScope.of(identity)
        # The held market's own reading: its provider's stream of its pool,
        # never whichever source recorded the pool last.
        snapshot = await latest_in(self.markets, scope, include_fixtures=self.include_fixtures)
        if snapshot is None:
            # Said precisely, without using it: is there no reading at all, or
            # only another market's?
            other = await self.markets.latest(pair_id, include_fixtures=self.include_fixtures)
            if other is None:
                return ValuationRefusal.MARKET_NOT_RECORDED
            if replace(held, base_asset_id=None).matches(other):
                # This market's own stream, pricing another asset.
                return ValuationRefusal.PRICE_ASSET_MISMATCH
            return ValuationRefusal.MARKET_IDENTITY_MISMATCH
        if snapshot.pair.pair_id != pair_id or not _same_market(snapshot, holding):
            return ValuationRefusal.MARKET_IDENTITY_MISMATCH
        price = snapshot.price
        if price.status is not Availability.AVAILABLE or price.value_usd is None:
            return ValuationRefusal.PRICE_UNAVAILABLE
        if price.value_usd <= 0:
            return ValuationRefusal.PRICE_UNAVAILABLE
        if price.asset_id != holding.asset_id:
            # A price from the wrong side of a pair is wrong by the exchange
            # rate and looks entirely plausible.
            return ValuationRefusal.PRICE_ASSET_MISMATCH
        age = (now - price.observed_at).total_seconds()
        if age < 0:
            return ValuationRefusal.PRICE_NOT_YET_OBSERVED
        if age > self.max_age_seconds:
            return ValuationRefusal.PRICE_STALE
        return PositionMark(
            asset_id=holding.asset_id,
            pair_id=pair_id,
            provider=price.provider,
            snapshot_id=snapshot.id,
            observation_id=price.id,
            price_usd=price.value_usd,
            # The source's own instant, carried unchanged.
            observed_at=price.observed_at,
        )


def _same_market(snapshot: Any, holding: Position) -> bool:
    """Whether the recorded market is the one the position names.

    Chain and network are checked beside the pair because an address means
    nothing across chains, and a position that recorded its provider is held to
    that too — two providers observing one pool are two sources, and a valuation
    that silently swapped them would attribute a price to the wrong one.
    """
    for recorded, named in (
        (snapshot.chain, holding.market_chain),
        (snapshot.network, holding.market_network),
        (snapshot.provider, holding.market_provider),
    ):
        if named is not None and recorded != named:
            return False
    return True


async def held_market_identities(
    session: AsyncSession, positions: Sequence[Position]
) -> dict[str, MarketIdentity]:
    """The full market identity of each holding, from the case that bought it.

    Followed through the holding's own cycle to its case-bound entry and that
    entry's case. A holding without a cycle or an entry has none, and is valued
    from what it recorded.
    """
    from sqlalchemy import select

    from src.data.tables import TradeCaseExecutionRow, TradeCaseRow

    cycles = {item.cycle_id: item.asset_id for item in positions if item.cycle_id is not None}
    if not cycles:
        return {}
    rows = (
        await session.execute(
            select(TradeCaseExecutionRow.cycle_id, TradeCaseRow.market_payload)
            .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExecutionRow.trade_case_id)
            .where(TradeCaseExecutionRow.cycle_id.in_(list(cycles)))
        )
    ).all()
    return {cycles[cycle]: MarketIdentity.model_validate(payload) for cycle, payload in rows}
