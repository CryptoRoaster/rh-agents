"""PRE_VECTOR_EARLY_ENTRY_V1: a bounded PAPER-only entry before VECTOR has history.

A normal case needs VECTOR, and VECTOR needs 24 closed hourly bars, so a pool
younger than a day can never be entered. This strategy is the one, explicit
exception — and it is an exception to *that* requirement only:

* it runs in its own workflow (``trade-case-early-v1``) and every case it opens
  carries ``strategy_policy_id = PRE_VECTOR_EARLY_ENTRY_V1``, so no early case is
  ever inferred from missing VECTOR evidence and no normal case is ever judged
  by these rules;
* a deterministic EARLY producer replaces the VECTOR setup with a fixed
  geometry around a fresh recorded price, and only after VECTOR's own
  ``assess`` has said the history is too young — not broken, not stale;
* ORBIT, ATLAS, PULSE, ANCHOR and SENTINEL all still run, unchanged;
* SENTINEL is the same engine with one changed limit (minimum liquidity), and
  the small strategy caps below sit *in addition* to every account limit.

Everything here is a constant of the strategy version. Nothing is configured,
because a different number would be a different strategy.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import RiskLimits

PRE_VECTOR_EARLY_ENTRY_V1 = "PRE_VECTOR_EARLY_ENTRY_V1"
EARLY_WORKFLOW_VERSION = "trade-case-early-v1"
# The setup kind the EARLY producer writes. Deliberately not a VECTOR
# `SetupKind`: VECTOR can never propose it, and nothing reads it as a breakout
# or a pullback.
EARLY_SETUP_KIND = "PRE_VECTOR_EARLY_ENTRY"
EARLY_SETUP_POLICY_VERSION = "pre-vector-early-setup-v1"
EARLY_RISK_LIMITS_VERSION = "EARLY_RISK_LIMITS_V1"


@dataclass(frozen=True)
class EarlyEntryPolicy:
    version: str
    # Measured from the chain-side creation timestamp, never from discovery.
    max_age: timedelta
    # The VECTOR sufficiency verdicts that mean "too young", and nothing else.
    allowed_history: frozenset[str]
    # Setup geometry around the fresh reference price P.
    zone_fraction: Decimal
    invalidation_multiple: Decimal
    target_multiple: Decimal
    lifetime: timedelta
    # How old the recorded price may be when the setup is built.
    max_input_age: timedelta
    # Fixed notional: exactly this, or no entry. Never downsized.
    notional_usd: Decimal
    # Strategy caps, over early positions only, in addition to SENTINEL.
    max_open_positions: int
    max_exposure_usd: Decimal
    daily_loss_cap_usd: Decimal
    max_entries_per_run: int
    reentry: bool
    # The single SENTINEL limit this strategy changes.
    min_liquidity_usd: Decimal

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0) or self.lifetime <= timedelta(0):
            raise ValueError("Age and lifetime bounds must be positive")
        if not Decimal(0) < self.zone_fraction < Decimal(1):
            raise ValueError("The entry zone must be a fraction of the reference price")
        if not Decimal(0) < self.invalidation_multiple < Decimal(1) - self.zone_fraction:
            raise ValueError("Invalidation must sit below the entry zone")
        if self.target_multiple <= Decimal(1) + self.zone_fraction:
            raise ValueError("The informative target must sit above the entry zone")
        if self.notional_usd <= 0 or self.max_exposure_usd < self.notional_usd:
            raise ValueError("The exposure cap must admit at least one entry")
        if self.max_open_positions < 1 or self.max_entries_per_run < 1:
            raise ValueError("Caps must admit at least one entry")
        if self.daily_loss_cap_usd <= 0 or self.min_liquidity_usd <= 0:
            raise ValueError("Loss and liquidity bounds must be positive")


EARLY_ENTRY_V1 = EarlyEntryPolicy(
    version=PRE_VECTOR_EARLY_ENTRY_V1,
    max_age=timedelta(hours=6),
    allowed_history=frozenset({"MARKET_HISTORY_TOO_SHORT", "MARKET_HISTORY_EMPTY"}),
    zone_fraction=Decimal("0.05"),
    invalidation_multiple=Decimal("0.40"),
    target_multiple=Decimal(2),
    lifetime=timedelta(minutes=10),
    max_input_age=timedelta(minutes=5),
    notional_usd=Decimal(10),
    max_open_positions=5,
    max_exposure_usd=Decimal(50),
    daily_loss_cap_usd=Decimal(30),
    max_entries_per_run=1,
    reentry=False,
    min_liquidity_usd=Decimal(10_000),
)


def is_early(strategy_policy_id: str | None) -> bool:
    return strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1


def early_risk_limits(base: RiskLimits, policy: EarlyEntryPolicy = EARLY_ENTRY_V1) -> RiskLimits:
    """`EARLY_RISK_LIMITS_V1`: the account's limits with one change, minimum liquidity.

    Everything else — position size, exposure, slippage, daily loss, holder
    concentration, snapshot age, approval window, kill switch — is the account's
    own value, read from the same object every other case is judged against.
    The position limit is deliberately *not* reduced to the early notional: a
    $10 order's worst-case cost includes fees and slippage and would refuse
    itself against a $10 ceiling.
    """
    return base.model_copy(update={"min_liquidity_usd": policy.min_liquidity_usd})


def limits_for(
    strategy_policy_id: str | None, base: RiskLimits, policy: EarlyEntryPolicy = EARLY_ENTRY_V1
) -> RiskLimits:
    """The SENTINEL limits a case is judged against, by its own strategy."""
    return early_risk_limits(base, policy) if is_early(strategy_policy_id) else base


@dataclass(frozen=True)
class EarlyBook:
    """The early strategy's own open book, read from the ledger and the cases."""

    open_positions: int
    exposure_usd: Decimal
    realized_loss_today_usd: Decimal


def utc_day_start(now: datetime) -> datetime:
    day = now.astimezone(UTC).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True)
class EarlyLedger:
    """What the ledger holds for the early strategy, before any instant is applied.

    Read in one place and judged at an instant the caller chooses, so a caller
    that must not await between its clock read and its decision can read this
    first and still decide on the exact instant it decides at.
    """

    open_positions: int
    exposure_usd: Decimal
    exits: tuple[tuple[datetime, Decimal], ...]

    def at(self, now: datetime) -> EarlyBook:
        start = utc_day_start(now)
        return EarlyBook(
            open_positions=self.open_positions,
            exposure_usd=self.exposure_usd,
            realized_loss_today_usd=sum(
                (-pnl for filled_at, pnl in self.exits if filled_at >= start and pnl < 0),
                Decimal(0),
            ),
        )


async def early_ledger(session: AsyncSession) -> EarlyLedger:
    """Open early positions, their cost basis, and every early exit's realised result.

    Only positions whose current cycle was entered by an early case count, and
    only exits of early cases count toward the loss. A loss elsewhere in the
    account is SENTINEL's business, not this cap's.
    """
    from src.data.repository import aware
    from src.data.tables import PositionRow, TradeCaseExecutionRow, TradeCaseExitRow, TradeCaseRow

    opened = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(PositionRow.cost_basis_usd), 0))
            .select_from(PositionRow)
            .join(TradeCaseExecutionRow, TradeCaseExecutionRow.cycle_id == PositionRow.cycle_id)
            .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExecutionRow.trade_case_id)
            .where(
                TradeCaseRow.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1,
                PositionRow.quantity > 0,
            )
        )
    ).one()
    exits = (
        await session.execute(
            select(TradeCaseExitRow.filled_at, TradeCaseExitRow.realized_pnl_usd)
            .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExitRow.trade_case_id)
            .where(TradeCaseRow.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1)
        )
    ).all()
    return EarlyLedger(
        open_positions=int(opened[0]),
        exposure_usd=Decimal(opened[1]),
        exits=tuple((aware(filled_at), Decimal(pnl)) for filled_at, pnl in exits),
    )


async def early_book(session: AsyncSession, now: datetime) -> EarlyBook:
    return (await early_ledger(session)).at(now)


def cap_refusal(book: EarlyBook, policy: EarlyEntryPolicy = EARLY_ENTRY_V1) -> str | None:
    """Which strategy cap a further early entry would breach, if any."""
    if book.realized_loss_today_usd >= policy.daily_loss_cap_usd:
        return "EARLY_DAILY_LOSS_CAP_REACHED"
    if book.open_positions >= policy.max_open_positions:
        return "EARLY_MAX_OPEN_POSITIONS_REACHED"
    if book.exposure_usd + policy.notional_usd > policy.max_exposure_usd:
        return "EARLY_MAX_EXPOSURE_REACHED"
    return None


def capacity_refusal(
    largest_tested_acceptable_notional_usd: Decimal | None,
    policy: EarlyEntryPolicy = EARLY_ENTRY_V1,
) -> str | None:
    """Whether ANCHOR proved the fixed early notional executable. No downsizing."""
    if largest_tested_acceptable_notional_usd is None:
        return "EARLY_EXECUTABLE_CAPACITY_UNKNOWN"
    if largest_tested_acceptable_notional_usd < policy.notional_usd:
        return "EARLY_EXECUTABLE_CAPACITY_BELOW_NOTIONAL"
    return None
