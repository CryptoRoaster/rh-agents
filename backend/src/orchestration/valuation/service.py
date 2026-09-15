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

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from src.core.models import Position
from src.markets.models import Availability, MarketSnapshot
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

    async def value(self, positions: list[Position], now: datetime) -> PortfolioValuation:
        """Mark every non-zero holding, or name the ones that could not be.

        `now` is the instant freshness is judged at. It is passed in rather than
        read here, because the caller owns the one instant everything else in
        its decision is measured against.
        """
        marks: list[PositionMark] = []
        unvalued: list[UnvaluedPosition] = []
        valued: list[str] = []
        for holding in sorted(positions, key=lambda item: item.asset_id):
            if holding.quantity == 0:
                # Nothing held, nothing to value. A closed position contributes
                # no exposure and needs no price.
                continue
            valued.append(holding.asset_id)
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
            marks=tuple(marks), unvalued=tuple(unvalued), valued_assets=tuple(valued)
        )

    async def _mark(self, holding: Position, now: datetime) -> PositionMark | ValuationRefusal:
        pair_id = holding.market_pair_id
        if pair_id is None:
            # The position never recorded which market it came from. Choosing
            # one for it would resolve an ambiguity silently, in the one place
            # where being wrong misprices the whole portfolio.
            return ValuationRefusal.POSITION_MARKET_UNKNOWN
        snapshot = await self.markets.latest(pair_id, include_fixtures=self.include_fixtures)
        if snapshot is None:
            return ValuationRefusal.MARKET_NOT_RECORDED
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
