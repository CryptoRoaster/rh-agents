"""EARLY_PAPER_EXIT_V1: when an open PRE_VECTOR_EARLY_ENTRY_V1 position is closed.

Deterministic, PAPER only, and always a full exit — the one exit path sells the
whole holding, so a partial sale cannot be expressed. At most one trigger is
named, in a fixed, protective-first order:

1. **STOP_LOSS** — the mark is at or below 40 % of the entry cost per unit
   (−60 %).
2. **LIQUIDITY_INVALIDATION** — the held market's current liquidity is known
   and below the early strategy's own entry requirement (10,000 USD).
3. **TRAILING_STOP** — the highest observed price since entry reached 2× the
   entry cost per unit, and the mark has since fallen to 50 % of that peak or
   below.
4. **TIME_EXIT** — the position has been held 72 hours.

**What "entry" means.** The entry instant is the case fill's `filled_at`. The
entry cost per unit is the ledger's cost basis divided by the quantity — the
execution price *with* slippage, fees and gas, the money actually at risk. No
top-up exists, so the holding is exactly the one fill.

**The peak is never stored.** It is the highest available, non-fixture price
recorded for the held market (same provider, same pair) between the entry
instant and now, together with the current mark. Those observations are
durable, append-only rows, so the peak survives a restart without a second
copy of state that could disagree with them, and every exit's basis names the
observation it rested on. A gap in recording is a gap in the peak, never an
invented price.

**Unknown is not a breach.** An unknown or stale mark fires no price trigger
and an unknown liquidity no invalidation; the time exit needs neither. Whatever
fires, the sale still goes through `PaperExitService` and its own SENTINEL
SELL check, which may refuse it — then nothing is booked and the next sweep
asks again.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import Position, RiskLimits
from src.data.repository import aware
from src.data.tables import MarketObservationRow, TradeCaseExecutionRow, TradeCaseRow
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.exitpolicy.policy import Immutable
from src.orchestration.exitpolicy.service import ExitSweep, exit_request_key
from src.orchestration.paperexit.models import PaperExitRecorded
from src.orchestration.paperexit.service import (
    ExitTriggerRecord,
    PaperExitService,
    PaperExitUnavailable,
)
from src.orchestration.riskrequest.service import OneSnapshot
from src.orchestration.strategy.early import EARLY_ENTRY_V1, PRE_VECTOR_EARLY_ENTRY_V1
from src.orchestration.valuation.service import PositionValuationReader
from src.orchestration.valuation.service import _same_market as same_market

EARLY_EXIT_VERSION: Literal["EARLY_PAPER_EXIT_V1"] = "EARLY_PAPER_EXIT_V1"
BPS = Decimal(10000)

# The peak read loads candidates, never the window: the database orders every
# observation since entry by price and only the top page is read and checked.
# A page whose rows all fail the exact check moves on to the next, up to a bound;
# exhausting it is stated on the basis as an incomplete peak, never hidden.
PEAK_CANDIDATE_PAGE = 20
PEAK_CANDIDATE_PAGES = 5


class EarlyExitTrigger(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    LIQUIDITY_INVALIDATION = "LIQUIDITY_INVALIDATION"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"


class EarlyExitPolicy(Immutable):
    version: Literal["EARLY_PAPER_EXIT_V1"] = EARLY_EXIT_VERSION
    stop_loss_bps: int = Field(default=6000, gt=0, lt=10000)
    trailing_activation_multiple: Decimal = Field(default=Decimal(2), gt=1)
    trailing_drawdown_bps: int = Field(default=5000, gt=0, lt=10000)
    max_holding_seconds: int = Field(default=72 * 3600, gt=0)
    min_liquidity_usd: Decimal = Field(default=EARLY_ENTRY_V1.min_liquidity_usd, gt=0)


EARLY_PAPER_EXIT_V1 = EarlyExitPolicy()


class ObservedPeak(Immutable):
    """The highest recorded price since entry, and where it came from."""

    price_usd: Decimal | None = Field(default=None, gt=0)
    observed_at: AwareDatetime | None = None
    observation_id: UUID | None = None
    observations: int = Field(default=0, ge=0)
    truncated: bool = False


class EarlyExitInputs(Immutable):
    """What was observed about one open early position at one instant."""

    entry_cost_per_unit_usd: Decimal = Field(gt=0)
    entered_at: AwareDatetime
    mark_price_usd: Decimal | None = Field(default=None, gt=0)
    mark_observed_at: AwareDatetime | None = None
    peak: ObservedPeak = ObservedPeak()
    liquidity_usd: Decimal | None = Field(default=None, ge=0)
    now: AwareDatetime


class EarlyExitVerdict(Immutable):
    policy_version: Literal["EARLY_PAPER_EXIT_V1"] = EARLY_EXIT_VERSION
    trigger: EarlyExitTrigger | None = None
    reason: str
    return_bps: Decimal | None = None
    peak_price_usd: Decimal | None = None
    trailing_active: bool = False
    held_seconds: int
    liquidity_usd: Decimal | None = None
    # Every trigger whose condition held, in priority order. The first one is
    # the trigger; the rest are recorded so a simultaneous breach is auditable.
    conditions: tuple[EarlyExitTrigger, ...] = ()


def evaluate_early(policy: EarlyExitPolicy, inputs: EarlyExitInputs) -> EarlyExitVerdict:
    entry = inputs.entry_cost_per_unit_usd
    mark = inputs.mark_price_usd
    held = max(0, int((inputs.now - inputs.entered_at).total_seconds()))
    # The peak includes the current mark: a price we are looking at is a price
    # that was reached.
    candidates = [item for item in (inputs.peak.price_usd, mark) if item is not None]
    peak = max(candidates) if candidates else None
    trailing = peak is not None and peak >= entry * policy.trailing_activation_multiple
    move = None if mark is None else ((mark / entry - 1) * BPS).quantize(Decimal("0.01"))

    met: list[EarlyExitTrigger] = []
    if mark is not None and mark <= entry * (1 - Decimal(policy.stop_loss_bps) / BPS):
        met.append(EarlyExitTrigger.STOP_LOSS)
    if inputs.liquidity_usd is not None and inputs.liquidity_usd < policy.min_liquidity_usd:
        met.append(EarlyExitTrigger.LIQUIDITY_INVALIDATION)
    if (
        trailing
        and mark is not None
        and peak is not None
        and mark <= peak * (1 - Decimal(policy.trailing_drawdown_bps) / BPS)
    ):
        met.append(EarlyExitTrigger.TRAILING_STOP)
    if held >= policy.max_holding_seconds:
        met.append(EarlyExitTrigger.TIME_EXIT)

    reasons = {
        EarlyExitTrigger.STOP_LOSS: "MARK_AT_OR_BELOW_STOP",
        EarlyExitTrigger.LIQUIDITY_INVALIDATION: "LIQUIDITY_BELOW_EARLY_MINIMUM",
        EarlyExitTrigger.TRAILING_STOP: "MARK_AT_OR_BELOW_TRAILING_STOP",
        EarlyExitTrigger.TIME_EXIT: "MAX_HOLDING_TIME_REACHED",
    }
    trigger = met[0] if met else None
    if trigger is not None:
        reason = reasons[trigger]
    elif mark is None:
        reason = "HOLD_MARK_UNKNOWN"
    elif inputs.peak.truncated:
        # The peak could not be established in full. A trailing exit on a lower
        # bound is still a correct exit, but holding on one is not a proof.
        reason = "HOLD_PEAK_INCOMPLETE"
    else:
        reason = "HOLD_TRAILING_ACTIVE" if trailing else "HOLD"
    return EarlyExitVerdict(
        trigger=trigger,
        reason=reason,
        return_bps=move,
        peak_price_usd=peak,
        trailing_active=trailing,
        held_seconds=held,
        liquidity_usd=inputs.liquidity_usd,
        conditions=tuple(met),
    )


def _price_text() -> Any:
    return MarketObservationRow.payload[("price", "value_usd")].as_string()


def _price_status() -> Any:
    return MarketObservationRow.payload[("price", "status")].as_string()


# A stored price the ranking may cast: digits, an optional fraction, an optional
# short exponent — the shapes `Decimal` serialises to, bounded so no cast can
# overflow. Anything else is never cast at all.
_NUMERIC_TEXT = r"^[0-9]{1,40}([.][0-9]{1,40})?([eE][-+]?[0-9]{1,2})?$"
# The ledger's own bound (`Numeric(38, 18)`): a price at or above it is damaged.
MAX_PEAK_PRICE_USD = Decimal(10) ** 20


def _orderable_price(session: AsyncSession) -> Any:
    """The recorded price as a number the database can rank, or NULL.

    PostgreSQL raises on a cast of non-numeric text, so the cast only happens
    for text that matches a bounded number; everything else ranks as NULL and
    is never a candidate. SQLite (lightweight tests only) casts any text
    without raising. The ranking is approximate by design — every candidate is
    re-read and compared exactly.
    """
    text = _price_text()
    if session.get_bind().dialect.name == "postgresql":
        return case((text.op("~")(_NUMERIC_TEXT), cast(text, Float)), else_=None)
    return cast(text, Float)


async def observed_peak(
    session: AsyncSession,
    position: Position,
    *,
    since: datetime,
    until: datetime,
    include_fixtures: bool = False,
    page: int = PEAK_CANDIDATE_PAGE,
    max_pages: int = PEAK_CANDIDATE_PAGES,
) -> ObservedPeak:
    """The highest available price recorded for the held market in [since, until].

    Over the **whole** window: the database orders every matching observation
    by its recorded USD price, highest first, and only the top candidates are
    loaded — never the window itself. Each candidate is then read as the
    recorder's own `MarketSnapshot` and its price checked exactly, so the
    database's ordering only proposes and the stored observation decides.

    Only the position's own market — provider and pair — and only observations
    whose price is stated AVAILABLE and positive. Fixtures never count unless
    explicitly included. If no candidate within the bounded number of pages
    survives that check, the peak is reported as incomplete, never as known.
    """
    if position.market_pair_id is None:
        return ObservedPeak()
    window = [
        MarketObservationRow.pair_id == position.market_pair_id,
        MarketObservationRow.observed_at >= since,
        MarketObservationRow.observed_at <= until,
        MarketObservationRow.available.is_(True),
        _price_status() == Availability.AVAILABLE.value,
    ]
    if position.market_provider is not None:
        window.append(MarketObservationRow.provider == position.market_provider)
    if not include_fixtures:
        window.append(MarketObservationRow.is_fixture.is_(False))
    priced = _orderable_price(session)
    # Every row in the window claims an available price; only those whose
    # stored text is a bounded, positive number can be ranked. Any difference
    # is damaged data, and a peak read past damaged data is a lower bound.
    claimed = (
        await session.scalar(select(func.count()).select_from(MarketObservationRow).where(*window))
    ) or 0
    counted = (
        await session.scalar(
            select(func.count()).select_from(MarketObservationRow).where(*window, priced > 0)
        )
    ) or 0
    damaged = claimed > counted
    if counted == 0:
        return ObservedPeak(observations=0, truncated=damaged)
    statement = (
        select(MarketObservationRow)
        .where(*window, priced > 0)
        .order_by(
            priced.desc(),
            MarketObservationRow.observed_at.asc(),
            MarketObservationRow.id.asc(),
        )
    )
    unreadable = False
    for index in range(max_pages):
        rows = (await session.scalars(statement.limit(page).offset(index * page))).all()
        best: tuple[Decimal, datetime, UUID] | None = None
        for row in rows:
            try:
                snapshot = MarketSnapshot.model_validate(row.payload)
            except ValidationError:
                # A stored row that is not a snapshot proves no price. Skipped,
                # never allowed to stop the sweep and never read as a peak.
                unreadable = True
                continue
            price = snapshot.price
            if price.status != Availability.AVAILABLE or price.value_usd is None:
                continue
            if price.value_usd <= 0 or price.value_usd >= MAX_PEAK_PRICE_USD:
                # Not a price the ledger could hold: damaged, never a peak.
                unreadable = True
                continue
            # Exact comparison within the page: the ordering only proposes.
            if best is None or price.value_usd > best[0]:
                best = (price.value_usd, aware(row.observed_at), row.id)
        if best is not None:
            # Rows ranked above the winner were skipped as unreadable: the true
            # maximum may be among them, so the peak is a lower bound and says so.
            return ObservedPeak(
                price_usd=best[0],
                observed_at=best[1],
                observation_id=best[2],
                observations=counted,
                truncated=unreadable or damaged,
            )
        if len(rows) < page:
            break
    # Rows claimed a price and none could be read as one. Not a peak.
    return ObservedPeak(observations=counted, truncated=True)


@dataclass(frozen=True)
class EarlyExitService:
    """The early exit sweep: every open early position, through the one exit path."""

    sessions: async_sessionmaker[AsyncSession]
    exits: PaperExitService
    markets: Any
    limits: RiskLimits
    policy: EarlyExitPolicy = EARLY_PAPER_EXIT_V1
    max_exits: int = 5
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def sweep(self) -> ExitSweep:
        result = ExitSweep()
        async with self.sessions() as session:
            positions = [
                item
                for item in await self.exits.paper.positions_in_session(session)
                if item.quantity > 0 and item.cycle_id is not None
            ]
            entries = await early_entries(session, positions)
        for position in sorted(positions, key=lambda item: str(item.id)):
            entry = entries.get(position.cycle_id)
            if entry is None:
                # Not an early cycle: another policy's position, never this one's.
                continue
            now = self.clock.now()
            inputs = await self._inputs(position, entry, now)
            verdict = evaluate_early(self.policy, inputs)
            result.evaluated += 1
            if verdict.trigger is None:
                result.held += 1
                if verdict.reason == "HOLD_MARK_UNKNOWN":
                    # Visible, and asked again next sweep: no price, no price exit.
                    result.refused("EARLY_EXIT_MARK_UNKNOWN")
                elif verdict.reason == "HOLD_PEAK_INCOMPLETE":
                    result.refused("EARLY_EXIT_PEAK_INCOMPLETE")
                continue
            result.triggered += 1
            code = verdict.trigger.value
            result.triggers[code] = result.triggers.get(code, 0) + 1
            if result.executed >= self.max_exits:
                result.refused("EXIT_BUDGET_REACHED")
                continue
            try:
                reading = await self.exits.execute_position_exit(
                    position.id,
                    request_key=exit_request_key(self.policy, position.cycle_id, now),
                    trigger=ExitTriggerRecord(
                        trigger=code,
                        policy_version=self.policy.version,
                        basis={
                            "policy": self.policy.model_dump(mode="json"),
                            "inputs": inputs.model_dump(mode="json"),
                            "verdict": verdict.model_dump(mode="json"),
                        },
                    ),
                )
            except PaperExitUnavailable as failure:
                # Nothing was sold; the trigger still holds and the next sweep asks again.
                result.refused(str(failure))
                continue
            if isinstance(reading, PaperExitRecorded):
                if reading.replayed:
                    # Another sweep already sold it under this key: the sale
                    # exists once, and this sweep did not make it.
                    result.refused("EXIT_ALREADY_RECORDED")
                else:
                    result.executed += 1
            else:
                result.refused(reading.reason.value)
        return result

    async def _inputs(
        self, position: Position, entry: TradeCaseExecutionRow, now: datetime
    ) -> EarlyExitInputs:
        feed = OneSnapshot(self.markets, self.include_fixtures)
        valuation = await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
        ).value([position], now)
        mark = valuation.by_asset.get(position.asset_id)
        liquidity: Decimal | None = None
        if position.market_pair_id is not None:
            snapshot = await feed.latest(position.market_pair_id)
            if (
                snapshot is not None
                # The held market's own reading: same pool, and the same chain,
                # network and provider the position recorded — the rule the
                # mark itself is held to. Another source's reading of the pool
                # is not this position's liquidity.
                and snapshot.pair.pair_id == position.market_pair_id
                and same_market(snapshot, position)
                and snapshot.liquidity.status == Availability.AVAILABLE
                and (now - snapshot.freshness_at).total_seconds()
                <= self.limits.max_snapshot_age_seconds
            ):
                liquidity = snapshot.liquidity.value_usd
        entered_at = aware(entry.filled_at)
        async with self.sessions() as session:
            peak = await observed_peak(
                session,
                position,
                since=entered_at,
                until=now,
                include_fixtures=self.include_fixtures,
            )
        return EarlyExitInputs(
            entry_cost_per_unit_usd=position.cost_basis_usd / position.quantity,
            entered_at=entered_at,
            mark_price_usd=None if mark is None else mark.price_usd,
            mark_observed_at=None if mark is None else mark.observed_at,
            peak=peak,
            liquidity_usd=liquidity,
            now=now,
        )


async def early_entries(
    session: AsyncSession, positions: list[Position]
) -> dict[Any, TradeCaseExecutionRow]:
    """The case-bound entry of every listed position whose cycle an early case opened."""
    cycles = [item.cycle_id for item in positions if item.cycle_id is not None]
    if not cycles:
        return {}
    rows = (
        await session.scalars(
            select(TradeCaseExecutionRow)
            .join(TradeCaseRow, TradeCaseRow.id == TradeCaseExecutionRow.trade_case_id)
            .where(
                TradeCaseExecutionRow.cycle_id.in_(cycles),
                TradeCaseRow.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1,
            )
        )
    ).all()
    return {row.cycle_id: row for row in rows}


__all__ = [
    "EARLY_EXIT_VERSION",
    "EARLY_PAPER_EXIT_V1",
    "EarlyExitInputs",
    "EarlyExitPolicy",
    "EarlyExitService",
    "EarlyExitTrigger",
    "EarlyExitVerdict",
    "ObservedPeak",
    "early_entries",
    "evaluate_early",
    "observed_peak",
]
