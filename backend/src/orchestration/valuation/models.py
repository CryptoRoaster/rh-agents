"""What it takes to value an open PAPER position, and every way it fails.

A position names an asset. An asset is not a market: the same token can trade in
several pools, and a recorded observation belongs to a pair. So a valuation
needs the market the position was *acquired in*, and a position that does not
carry one cannot be valued — that ambiguity is reported rather than resolved by
picking whichever market happens to mention the asset.

Nothing here substitutes for a missing price. Not the entry price, which is what
was paid rather than what it is worth; not zero, which is a claim that the
holding is worthless; and not a stale observation, which is a claim about a
moment that has passed. Each of those turns an unknown portfolio into a
confident wrong one, and the whole point of valuing it is that SENTINEL is about
to judge exposure against it.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ValuationRefusal(StrEnum):
    """Why a holding could not be valued. None of these has a fallback number."""

    # The position does not name the market it was acquired in, so no
    # observation can be attributed to it without guessing.
    POSITION_MARKET_UNKNOWN = "POSITION_MARKET_UNKNOWN"
    # Nothing has been recorded for that market.
    MARKET_NOT_RECORDED = "MARKET_NOT_RECORDED"
    # A recording exists and carries no usable price.
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    # The recording prices a different asset than the one held. Wrong by the
    # exchange rate, and entirely plausible-looking.
    PRICE_ASSET_MISMATCH = "PRICE_ASSET_MISMATCH"
    # The recording belongs to a different market than the position names.
    MARKET_IDENTITY_MISMATCH = "MARKET_IDENTITY_MISMATCH"
    # Older than the bound the evaluation will be judged under.
    PRICE_STALE = "PRICE_STALE"
    # Recorded as observed after the instant being valued at.
    PRICE_NOT_YET_OBSERVED = "PRICE_NOT_YET_OBSERVED"
    # A holding appeared between the valuation and the account lock, so the
    # portfolio being judged is not the portfolio that was valued.
    PORTFOLIO_CHANGED_DURING_VALUATION = "PORTFOLIO_CHANGED_DURING_VALUATION"


class PositionMark(Immutable):
    """One recorded observation, attributed to one held asset.

    Carries its provenance because a price without one cannot be checked: the
    market it belongs to, the observation it came from, the source that recorded
    it and the instant *that source* says it was true. No assembly time appears
    anywhere here.
    """

    asset_id: Identifier
    pair_id: Identifier
    provider: Identifier
    snapshot_id: UUID
    observation_id: UUID
    price_usd: Price
    # The source's own instant, never when it was read.
    observed_at: AwareDatetime

    def is_current_at(self, instant: datetime, tolerance_seconds: int) -> bool:
        """Whether this mark may still price the holding at `instant`."""
        age = (instant - self.observed_at).total_seconds()
        return 0 <= age <= tolerance_seconds


class UnvaluedPosition(Immutable):
    """One holding that could not be valued, and why."""

    asset_id: Identifier
    reason: ValuationRefusal
    pair_id: Identifier | None = None


class PortfolioValuation(Immutable):
    """Current marks for every open position, or the reason there are none.

    Complete or nothing: a partial valuation is an exposure figure computed from
    some of the portfolio, which is worse than no figure at all because it looks
    like one.
    """

    kind: Literal["portfolio_valuation"] = "portfolio_valuation"
    marks: tuple[PositionMark, ...] = Field(default=(), max_length=64)
    unvalued: tuple[UnvaluedPosition, ...] = Field(default=(), max_length=64)
    # The assets the valuation covers, as read before the account lock. Compared
    # against the holdings found under the lock, because a position created in
    # between would make this valuation describe a different portfolio.
    valued_assets: tuple[Identifier, ...] = Field(default=(), max_length=64)

    @property
    def complete(self) -> bool:
        return not self.unvalued

    @property
    def by_asset(self) -> dict[str, PositionMark]:
        return {item.asset_id: item for item in self.marks}

    def covers(self, held: set[str]) -> bool:
        """Whether every currently held asset was part of what was valued."""
        return held <= set(self.valued_assets)

    def stale_at(self, instant: datetime, tolerance_seconds: int) -> tuple[str, ...]:
        """Assets whose mark no longer prices them at `instant`."""
        return tuple(
            item.asset_id
            for item in self.marks
            if not item.is_current_at(instant, tolerance_seconds)
        )

    def recorded(self) -> list[dict[str, object]]:
        """The marks as an audit record, for the decision basis."""
        return [item.model_dump(mode="json") for item in sorted(self.marks, key=_order)]


def _order(mark: PositionMark) -> str:
    return mark.asset_id


def unvaluable_reason(valuation: "PortfolioValuation", unmarked: tuple[str, ...]) -> str | None:
    """Why the first holding the portfolio could not price could not be priced.

    Every caller that refuses on an unvaluable holding reports the same typed
    reason, so "missing", "stale" and "attributed to another market" stay three
    different answers wherever the refusal is raised.
    """
    reasons = {item.asset_id: item.reason for item in valuation.unvalued}
    for asset_id in unmarked:
        found = reasons.get(asset_id)
        if found is not None:
            return found.value
    # The holding was marked when it was read and the mark aged out before the
    # instant it was judged at; there is no refusal recorded for it.
    return ValuationRefusal.PRICE_STALE.value
