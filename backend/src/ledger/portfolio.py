"""The portfolio facts a risk evaluation reads, computed in exactly one place.

Extracted from `PaperTradingService` rather than reimplemented beside it. Two
paths now ask what the account holds — the paper fill and the case-bound risk
request — and two implementations of "what is our exposure?" would eventually
disagree about money. Which one was right would then be decided by whichever
happened to run.

Nothing here writes. It reads an account row and its positions and returns the
typed view SENTINEL consumes, plus the marks that were actually used and the
holdings that could not be marked, so a caller can record the first and refuse
on the second.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.core.models import Position, RiskContext, SafetyStatus
from src.core.numbers import quantize
from src.markets.models import MarketIdentity
from src.orchestration.valuation.models import PositionMark


def loss_day(now: datetime) -> date:
    """The UTC day a realised loss counts against."""
    return now.astimezone(UTC).date()


def roll_loss_day(account: object, now: datetime) -> None:
    """Reset the day's realised loss when the UTC day has turned.

    Mutates the account row in the caller's transaction, which is where the
    lock is held. Called before the figure is read, because a stale day would
    carry yesterday's loss into today's limit.
    """
    today = loss_day(now)
    if account.loss_day != today:  # type: ignore[attr-defined]
        account.loss_day = today  # type: ignore[attr-defined]
        account.realized_loss_today_usd = Decimal("0")  # type: ignore[attr-defined]


@dataclass(frozen=True)
class PortfolioState:
    """What the account holds, valued, and what could not be valued."""

    context: RiskContext
    position: Position
    prices: dict[str, Decimal]
    # The marks actually relied on, for the decision basis.
    marks_used: tuple[PositionMark, ...]
    # Non-zero holdings with no usable mark. Their presence is why `context`
    # reports unknown exposure rather than a figure computed from a guess.
    unmarked_assets: tuple[str, ...]
    # The market a holding of *this order's* asset was acquired in, when that is
    # not the market this order runs in. Filling would merge inventory bought in
    # one market into a position recorded against another, which is a top-up
    # across markets — a contract this system does not have.
    conflicting_market: str | None = None


def portfolio_state(
    *,
    cash_usd: Decimal,
    realized_loss_today_usd: Decimal,
    positions: list[Position],
    asset_id: str,
    price_usd: Decimal,
    marks: dict[str, PositionMark] | None,
    now: datetime,
    max_snapshot_age_seconds: int,
    correlation_id: UUID,
    market: MarketIdentity | None = None,
) -> PortfolioState:
    """Value the portfolio, or say honestly that it could not be valued.

    A holding with no fresh mark does not become zero and does not become the
    price of something else. It makes exposure and the day's loss *unknown*, and
    `accounting` says so — which is what lets SENTINEL emit
    `PORTFOLIO_DATA_UNKNOWN` instead of judging a number nobody measured.
    """
    # The price the *new order* is measured at. It prices nothing that is
    # already held: a holding is worth what its own market says, and this entry
    # only stands in for the position this order would open.
    prices = {asset_id: price_usd}
    used: list[PositionMark] = []
    unmarked: list[str] = []
    conflicting: str | None = None
    for holding in positions:
        if holding.quantity == 0:
            continue
        if _acquired_here(holding, asset_id, market):
            # The holding's own market is the one being judged, and this
            # reading is that market's — checked for freshness by the caller
            # under the same bound as everything else. Using it is not a
            # substitution; using it for a holding from anywhere else would be.
            prices[holding.asset_id] = price_usd
            continue
        if holding.asset_id == asset_id and holding.market_pair_id is not None:
            # Held already, and bought somewhere else. Reported rather than
            # priced, because no price makes this fill correct.
            conflicting = holding.market_pair_id
        mark = (marks or {}).get(holding.asset_id)
        if (
            mark is None
            or mark.asset_id != holding.asset_id
            or not mark.is_current_at(now, max_snapshot_age_seconds)
        ):
            unmarked.append(holding.asset_id)
            continue
        prices[holding.asset_id] = mark.price_usd
        used.append(mark)

    position = next((item for item in positions if item.asset_id == asset_id), None)
    if position is None:
        # A new holding records the market it is being acquired in, so it can be
        # valued later without anyone having to guess which pool it came from.
        position = Position(
            source="LEDGER",
            correlation_id=correlation_id,
            asset_id=asset_id,
            market_pair_id=None if market is None else market.pair_id,
            market_chain=None if market is None else market.chain,
            market_network=None if market is None else market.network,
            market_provider=None if market is None else market.provider,
            created_at=now,
            updated_at=now,
        )
    valid = not unmarked
    # Quantity and price each carry eighteen places, so their product carries
    # thirty-six. The sum is exact whatever order the holdings arrive in; only
    # the result is brought to the ledger's storage precision, so the same
    # holdings always produce the same figure.
    exposure = quantize(
        sum(
            (item.quantity * prices.get(item.asset_id, Decimal("0")) for item in positions),
            Decimal("0"),
        )
    )
    unrealized_loss = quantize(
        sum(
            (
                max(
                    Decimal("0"),
                    item.cost_basis_usd - item.quantity * prices.get(item.asset_id, Decimal("0")),
                )
                for item in positions
            ),
            Decimal("0"),
        )
    )
    return PortfolioState(
        context=RiskContext(
            cash_usd=cash_usd,
            exposure_usd=exposure if valid else None,
            position_quantity=position.quantity,
            daily_loss_usd=realized_loss_today_usd + unrealized_loss if valid else None,
            accounting=SafetyStatus.PASS if valid else SafetyStatus.UNKNOWN,
        ),
        position=position,
        prices=prices,
        marks_used=tuple(used),
        unmarked_assets=tuple(unmarked),
        conflicting_market=conflicting,
    )


def _acquired_here(holding: Position, asset_id: str, market: MarketIdentity | None) -> bool:
    """Whether this holding was acquired in the market now being judged.

    A holding that records no market is never this one. That absence is the
    ambiguity the identity contract refuses to resolve, and resolving it in
    favour of whichever market happens to be asking is the worst available
    answer: it is silent, and it is wrong by the whole spread.
    """
    if holding.asset_id != asset_id:
        # A market prices one base asset, so a different asset was never
        # acquired in it. Its price says nothing about this holding, and would
        # be wrong by whatever the two assets happen to differ by.
        return False
    if market is None:
        # The caller named no market. The standalone paper path is the only one
        # that does this: it records no market on what it opens either, so its
        # orders and its holdings live in one unnamed market by construction.
        # The moment *either* side names one, both must, and they must agree —
        # which is why a case-bound flow, whose market is never absent, can
        # never take this branch for a holding that recorded nothing.
        return holding.market_pair_id is None
    if holding.market_pair_id is None:
        return False
    return (
        holding.market_pair_id == market.pair_id
        and holding.market_chain == market.chain
        and holding.market_network == market.network
        and holding.market_provider == market.provider
    )


def portfolio_basis(
    state: PortfolioState,
    *,
    cash_usd: Decimal,
    realized_loss_today_usd: Decimal,
    positions: list[Position],
    asset_id: str,
    price_usd: Decimal,
    now: datetime,
    max_snapshot_age_seconds: int,
    correlation_id: UUID,
    market: MarketIdentity | None,
) -> dict[str, Any]:
    """Everything the valuation rested on, as an audit record.

    The figures are recorded beside their inputs rather than instead of them. A
    stored exposure nobody can re-derive is a claim, not evidence: it can only
    be checked by re-reading position rows that have moved since and market data
    that has moved further, which is exactly what an audit record exists to
    avoid.

    So the holdings themselves are kept — quantity, cost basis and the market
    each was acquired in — beside the account values, the instant and the bound
    the valuation was judged under. `replay_portfolio_basis` feeds them back
    through the same `portfolio_state`, so the recomputation is the computation.
    """
    from src.orchestration.riskrequest.models import canonical_amount

    held = [item for item in sorted(positions, key=lambda row: row.asset_id) if item.quantity]
    return {
        # --------------------------------------------------- the inputs
        "cash_usd": canonical_amount(cash_usd),
        "realized_loss_today_usd": canonical_amount(realized_loss_today_usd),
        "asset_id": asset_id,
        "order_price_usd": canonical_amount(price_usd),
        "valued_at": now.isoformat(),
        "max_snapshot_age_seconds": max_snapshot_age_seconds,
        "correlation_id": str(correlation_id),
        "market": None if market is None else market.model_dump(mode="json"),
        "holdings": [item.model_dump(mode="json") for item in held],
        # The marks actually relied on, with the market, source and instant each
        # came from.
        "position_marks": [item.model_dump(mode="json") for item in state.marks_used],
        # --------------------------------------------------- what came out
        "exposure_usd": canonical_amount(state.context.exposure_usd),
        "position_quantity": canonical_amount(state.context.position_quantity),
        "daily_loss_usd": canonical_amount(state.context.daily_loss_usd),
        "accounting": state.context.accounting.value,
        "unmarked_assets": list(state.unmarked_assets),
        "conflicting_market": state.conflicting_market,
    }


def replay_portfolio_basis(stored: dict[str, Any]) -> PortfolioState:
    """Recompute a stored valuation, through the code that produced it.

    Nothing current is consulted: not the position rows, not the account, not
    the market layer. If this disagrees with the figures recorded beside it, the
    record is wrong — which is the only reason to keep one.
    """
    marks = [PositionMark.model_validate(item) for item in stored["position_marks"]]
    market = stored.get("market")
    return portfolio_state(
        cash_usd=Decimal(stored["cash_usd"]),
        realized_loss_today_usd=Decimal(stored["realized_loss_today_usd"]),
        positions=[Position.model_validate(item) for item in stored["holdings"]],
        asset_id=stored["asset_id"],
        price_usd=Decimal(stored["order_price_usd"]),
        marks={item.asset_id: item for item in marks},
        now=datetime.fromisoformat(stored["valued_at"]),
        max_snapshot_age_seconds=stored["max_snapshot_age_seconds"],
        correlation_id=UUID(stored["correlation_id"]),
        market=None if market is None else MarketIdentity.model_validate(market),
    )
