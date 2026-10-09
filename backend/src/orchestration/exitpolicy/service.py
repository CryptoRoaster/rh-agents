"""The automatic exit sweep: evaluate every open PAPER position, exit through the one path.

For each open, case-bound PAPER position the sweep reads the entry fill, the
current mark and the held market's current liquidity, asks `PAPER_EXIT_V1`
for a trigger, and — only when one fires — calls the existing
`PaperExitService.execute_position_exit`. That service does everything an
exit has always done: whole holding, fresh SENTINEL SELL check, one order per
key, one exit per cycle, fill + ledger + exit record in one transaction. The
sweep adds only *why*: the trigger, the policy version and the numbers are
stored on that same exit record.

No second exit path exists, nothing here bypasses a stop, and no model is
asked. Re-entry is not part of this.

**Idempotency.** The key is `auto-exit:<policy>:<cycle>:<UTC minute>`. Within a
run a retry finds its own order; one exit per cycle is enforced by the
database, so a later run that tries again after a refusal can never produce a
second sale of the same holding.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import Position, RiskLimits
from src.data.repository import aware
from src.data.tables import TradeCaseExecutionRow
from src.markets.models import Availability
from src.markets.scope import MarketScope
from src.orchestration.exitpolicy.policy import (
    ExitInputs,
    ExitVerdict,
    PaperExitPolicy,
    evaluate,
)
from src.orchestration.paperexit.models import PaperExitRecorded
from src.orchestration.paperexit.service import (
    ExitTriggerRecord,
    PaperExitService,
    PaperExitUnavailable,
)
from src.orchestration.riskrequest.service import OneSnapshot
from src.orchestration.valuation.service import PositionValuationReader, held_market_identities


@dataclass
class ExitSweep:
    """What one sweep did. Counts and codes only."""

    evaluated: int = 0
    triggered: int = 0
    executed: int = 0
    held: int = 0
    triggers: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)

    def refused(self, code: str) -> None:
        self.refusals[code] = self.refusals.get(code, 0) + 1


class VersionedPolicy(Protocol):
    @property
    def version(self) -> str: ...


def exit_request_key(policy: VersionedPolicy, cycle_id: Any, now: datetime) -> str:
    return f"auto-exit:{policy.version}:{cycle_id}:{now.strftime('%Y%m%dT%H%M')}"


@dataclass(frozen=True)
class AutoExitService:
    sessions: async_sessionmaker[AsyncSession]
    exits: PaperExitService
    markets: Any
    policy: PaperExitPolicy
    limits: RiskLimits
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
            entries = await self._entries(session, positions)
            early = await _early_cycles(session, list(entries.values()))
        for position in sorted(positions, key=lambda item: str(item.id)):
            entry = entries.get(position.cycle_id)
            if entry is None:
                # No case-bound entry: not a position this policy may close.
                result.refused("POSITION_ORIGIN_UNKNOWN")
                continue
            if position.cycle_id in early:
                # PRE_VECTOR_EARLY_ENTRY_V1 has its own exit contract,
                # EARLY_PAPER_EXIT_V1. This policy never closes its positions.
                result.refused("EARLY_POSITION_OWN_EXIT_POLICY")
                continue
            now = self.clock.now()
            inputs = await self._inputs(position, entry, now)
            verdict = evaluate(self.policy, inputs)
            result.evaluated += 1
            if verdict.trigger is None:
                result.held += 1
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
                        basis=_basis(self.policy, inputs, verdict),
                    ),
                )
            except PaperExitUnavailable as failure:
                # Unknown is not permission: nothing was sold, the next run asks again.
                result.refused(str(failure))
                continue
            if isinstance(reading, PaperExitRecorded):
                result.executed += 1
            else:
                result.refused(reading.reason.value)
        return result

    @staticmethod
    async def _entries(
        session: AsyncSession, positions: list[Position]
    ) -> dict[Any, TradeCaseExecutionRow]:
        cycles = [item.cycle_id for item in positions if item.cycle_id is not None]
        if not cycles:
            return {}
        rows = (
            await session.scalars(
                select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.cycle_id.in_(cycles))
            )
        ).all()
        return {row.cycle_id: row for row in rows}

    async def _inputs(
        self, position: Position, entry: TradeCaseExecutionRow, now: datetime
    ) -> ExitInputs:
        """Entry fill, current mark and current liquidity — each only if it is fresh."""
        feed = OneSnapshot(self.markets, self.include_fixtures)
        async with self.sessions() as session:
            identities = await held_market_identities(session, [position])
        valuation = await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
            identities=identities,
        ).value([position], now)
        mark = valuation.by_asset.get(position.asset_id)
        liquidity: Decimal | None = None
        if position.market_pair_id is not None:
            held = MarketScope.held(position)
            identity = identities.get(position.asset_id)
            # The case's full identity when it agrees with the holding, so the
            # liquidity is read from exactly the market the mark was.
            scope = (
                MarketScope.of(identity)
                if held is not None and identity is not None and held.matches_identity(identity)
                else held
            )
            # The held market's own reading only; another source's reading of
            # the pool is not this position's liquidity.
            snapshot = None if scope is None else await feed.latest_in(scope)
            if (
                snapshot is not None
                and snapshot.liquidity.status == Availability.AVAILABLE
                and (now - snapshot.freshness_at).total_seconds()
                <= self.limits.max_snapshot_age_seconds
            ):
                liquidity = snapshot.liquidity.value_usd
        return ExitInputs(
            entry_price_usd=entry.execution_price_usd,
            entered_at=aware(entry.filled_at),
            mark_price_usd=None if mark is None else mark.price_usd,
            mark_observed_at=None if mark is None else mark.observed_at,
            liquidity_usd=liquidity,
            min_liquidity_usd=self.limits.min_liquidity_usd,
            now=now,
        )


async def _early_cycles(session: AsyncSession, entries: list[TradeCaseExecutionRow]) -> set[Any]:
    """Which of these entries an early case made."""
    from src.data.tables import TradeCaseRow
    from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1

    cases = [item.trade_case_id for item in entries]
    if not cases:
        return set()
    early = set(
        (
            await session.scalars(
                select(TradeCaseRow.id).where(
                    TradeCaseRow.id.in_(cases),
                    TradeCaseRow.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1,
                )
            )
        ).all()
    )
    return {item.cycle_id for item in entries if item.trade_case_id in early}


def _basis(policy: PaperExitPolicy, inputs: ExitInputs, verdict: ExitVerdict) -> dict[str, Any]:
    """Everything the trigger was decided on, recomputable by the same function."""
    return {
        "policy": policy.model_dump(mode="json"),
        "inputs": inputs.model_dump(mode="json"),
        "verdict": verdict.model_dump(mode="json"),
    }
