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
from uuid import UUID

from src.core.models import MarketSnapshot, Position, RiskContext, SafetyStatus


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
    # Marks a caller may record as observations it relied on.
    marks_used: tuple[MarketSnapshot, ...]
    # Non-zero holdings with no usable mark. Their presence is why `context`
    # reports unknown exposure rather than a figure computed from a guess.
    unmarked_assets: tuple[str, ...]


def portfolio_state(
    *,
    cash_usd: Decimal,
    realized_loss_today_usd: Decimal,
    positions: list[Position],
    asset_id: str,
    price_usd: Decimal,
    marks: dict[str, MarketSnapshot] | None,
    now: datetime,
    max_snapshot_age_seconds: int,
    correlation_id: UUID,
) -> PortfolioState:
    """Value the portfolio, or say honestly that it could not be valued.

    A holding with no fresh mark does not become zero and does not become the
    price of something else. It makes exposure and the day's loss *unknown*, and
    `accounting` says so — which is what lets SENTINEL emit
    `PORTFOLIO_DATA_UNKNOWN` instead of judging a number nobody measured.
    """
    prices = {asset_id: price_usd}
    used: list[MarketSnapshot] = []
    unmarked: list[str] = []
    for holding in positions:
        if holding.quantity == 0 or holding.asset_id == asset_id:
            continue
        mark = (marks or {}).get(holding.asset_id)
        if (
            mark is None
            or mark.asset_id != holding.asset_id
            or not 0 <= (now - mark.observed_at).total_seconds() <= max_snapshot_age_seconds
        ):
            unmarked.append(holding.asset_id)
            continue
        prices[holding.asset_id] = mark.price_usd
        used.append(mark)

    position = next((item for item in positions if item.asset_id == asset_id), None)
    if position is None:
        position = Position(
            source="LEDGER",
            correlation_id=correlation_id,
            asset_id=asset_id,
            created_at=now,
            updated_at=now,
        )
    valid = not unmarked
    exposure = sum(
        (item.quantity * prices.get(item.asset_id, Decimal("0")) for item in positions),
        Decimal("0"),
    )
    unrealized_loss = sum(
        (
            max(
                Decimal("0"),
                item.cost_basis_usd - item.quantity * prices.get(item.asset_id, Decimal("0")),
            )
            for item in positions
        ),
        Decimal("0"),
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
    )
