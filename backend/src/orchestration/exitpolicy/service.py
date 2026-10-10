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

**Judged again before the sale.** The trigger is decided on the marks the run
recorded at its start; the exit's own on-chain read then takes its time. So the
sale is asked with a `confirm` step: after that read, the held market is
observed again (`exitpolicy.refresh`) and the policy evaluated again on the new
reading. A trigger that has gone sells nothing; a trigger that changed is
recorded as the new one, with the original beside it.

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
from src.markets.geckoterminal.networks import VerifiedNetworkRegistry
from src.markets.models import Availability
from src.markets.scope import MarketScope
from src.orchestration.exitpolicy.policy import (
    ExitInputs,
    ExitVerdict,
    PaperExitPolicy,
    evaluate,
)
from src.orchestration.exitpolicy.refresh import (
    PreExitMarketRefresh,
    RefreshBudget,
    RefreshContext,
    RefreshDeadline,
    Unbounded,
)
from src.orchestration.paperexit.models import ExitReading, PaperExitRecorded
from src.orchestration.paperexit.service import (
    ExitConfirmation,
    ExitTriggerRecord,
    PaperExitService,
    PaperExitUnavailable,
)
from src.orchestration.riskrequest.service import OneSnapshot
from src.orchestration.valuation.service import PositionValuationReader, held_market_identities
from src.orchestration.workflow.models import TradeCase


@dataclass
class ExitSweep:
    """What one sweep did. Counts and codes only."""

    evaluated: int = 0
    triggered: int = 0
    executed: int = 0
    held: int = 0
    triggers: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    # The pre-exit refresh and the second judgement of each trigger.
    refresh_attempts: int = 0
    refresh_failures: int = 0
    refresh_provider_requests: int = 0
    triggers_cleared: int = 0
    triggers_changed: int = 0
    reevaluated: dict[str, int] = field(default_factory=dict)
    max_refresh_seconds: float | None = None
    max_atlas_read_seconds: float | None = None
    max_mark_age_at_trigger_seconds: float | None = None
    max_mark_age_at_final_seconds: float | None = None

    def refused(self, code: str) -> None:
        self.refusals[code] = self.refusals.get(code, 0) + 1

    def triggered_at(self, mark_age: float | None) -> None:
        self.max_mark_age_at_trigger_seconds = _larger(
            self.max_mark_age_at_trigger_seconds, mark_age
        )

    def measured(self, reading: ExitReading) -> None:
        """Fold one exit's freshness into the sweep's. Numbers and codes only."""
        freshness = reading.freshness
        if freshness is None:
            return
        self.refresh_attempts += freshness.refresh_attempts
        self.refresh_provider_requests += freshness.refresh_provider_requests
        if freshness.refresh_attempts and freshness.refresh_reason is not None:
            self.refresh_failures += 1
        if freshness.trigger_cleared:
            self.triggers_cleared += 1
        reevaluated = freshness.reevaluated_trigger
        if reevaluated is not None:
            self.reevaluated[reevaluated] = self.reevaluated.get(reevaluated, 0) + 1
            if reevaluated != freshness.original_trigger:
                self.triggers_changed += 1
        self.max_refresh_seconds = _larger(self.max_refresh_seconds, freshness.refresh_seconds)
        self.max_atlas_read_seconds = _larger(
            self.max_atlas_read_seconds, freshness.atlas_read_seconds
        )
        self.max_mark_age_at_final_seconds = _larger(
            self.max_mark_age_at_final_seconds, freshness.mark_age_at_final_seconds
        )


def _larger(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return value if current is None else max(current, value)


def mark_age(observed_at: datetime | None, now: datetime) -> float | None:
    return None if observed_at is None else round((now - observed_at).total_seconds(), 3)


def confirmation(
    trigger: str | None,
    record: ExitTriggerRecord | None,
    *,
    original: str,
    snapshot_id: Any,
    observed: tuple[str | None, int, int, float | None],
    mark_age_seconds: float | None,
) -> ExitConfirmation:
    """What a sweep answers when asked to judge its trigger again."""
    refusal, attempts, requests, seconds = observed
    return ExitConfirmation(
        trigger=record if trigger is not None else None,
        snapshot_id=snapshot_id,
        original_trigger=original,
        refresh_attempts=attempts,
        refresh_provider_requests=requests,
        refresh_seconds=seconds,
        refresh_reason=refusal,
        mark_age_seconds=mark_age_seconds,
    )


def refused_confirmation(
    original: str, observed: tuple[str | None, int, int, float | None]
) -> ExitConfirmation:
    refusal, attempts, requests, seconds = observed
    return ExitConfirmation(
        trigger=None,
        refusal=refusal,
        original_trigger=original,
        refresh_attempts=attempts,
        refresh_provider_requests=requests,
        refresh_seconds=seconds,
        refresh_reason=refusal,
    )


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
    # The held market observed again after the exit's on-chain read. Absent,
    # the trigger is still judged again — on what is recorded.
    refresh: PreExitMarketRefresh | None = None
    # Refresh attempts per sweep. `None` is one per exit this sweep may book.
    max_refreshes: int | None = None

    async def sweep(
        self,
        *,
        deadline: RefreshDeadline | None = None,
        networks: VerifiedNetworkRegistry | None = None,
    ) -> ExitSweep:
        result = ExitSweep()
        context = RefreshContext(
            port=self.refresh,
            budget=RefreshBudget(
                self.max_refreshes if self.max_refreshes is not None else self.max_exits
            ),
            deadline=deadline if deadline is not None else Unbounded(),
            networks=networks,
        )
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
            inputs, _ = await self._inputs(position, entry, now)
            verdict = evaluate(self.policy, inputs)
            result.evaluated += 1
            if verdict.trigger is None:
                result.held += 1
                continue
            result.triggered += 1
            code = verdict.trigger.value
            result.triggers[code] = result.triggers.get(code, 0) + 1
            result.triggered_at(mark_age(inputs.mark_observed_at, now))
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
                    confirm=self._confirmer(position, entry, code, inputs, verdict, context),
                )
            except PaperExitUnavailable as failure:
                # Unknown is not permission: nothing was sold, the next run asks again.
                result.refused(str(failure))
                continue
            result.measured(reading)
            if isinstance(reading, PaperExitRecorded):
                result.executed += 1
            else:
                result.refused(reading.reason.value)
        return result

    def _confirmer(
        self,
        position: Position,
        entry: TradeCaseExecutionRow,
        original: str,
        first: ExitInputs,
        first_verdict: ExitVerdict,
        context: RefreshContext,
    ) -> Any:
        """The trigger judged again, on the held market observed after the chain read."""

        async def confirm(trade_case: TradeCase) -> ExitConfirmation:
            observed = await context.observe(trade_case)
            if observed[0] is not None:
                return refused_confirmation(original, observed)
            now = self.clock.now()
            inputs, snapshot_id = await self._inputs(position, entry, now)
            verdict = evaluate(self.policy, inputs)
            trigger = None if verdict.trigger is None else verdict.trigger.value
            if trigger is None and inputs.mark_price_usd is None:
                # No current mark of the held market: neither a breach nor a
                # recovery is known. Refused by name, never read as cleared.
                return refused_confirmation(original, ("MARK_UNAVAILABLE", *observed[1:]))
            record = (
                None
                if trigger is None
                else ExitTriggerRecord(
                    trigger=trigger,
                    policy_version=self.policy.version,
                    basis={
                        **_basis(self.policy, inputs, verdict),
                        # What first fired it, recorded beside what it was sold on.
                        "original": {
                            "inputs": first.model_dump(mode="json"),
                            "verdict": first_verdict.model_dump(mode="json"),
                        },
                    },
                )
            )
            return confirmation(
                trigger,
                record,
                original=original,
                snapshot_id=snapshot_id,
                observed=observed,
                mark_age_seconds=mark_age(inputs.mark_observed_at, now),
            )

        return confirm

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
    ) -> tuple[ExitInputs, Any]:
        """Entry fill, current mark and current liquidity — each only if it is fresh.

        With the id of the held market's reading they were taken from, or
        `None` when there is none.
        """
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
        snapshot_id: Any = None
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
            snapshot_id = None if snapshot is None else snapshot.id
            if (
                snapshot is not None
                and snapshot.liquidity.status == Availability.AVAILABLE
                and (now - snapshot.freshness_at).total_seconds()
                <= self.limits.max_snapshot_age_seconds
            ):
                liquidity = snapshot.liquidity.value_usd
        inputs = ExitInputs(
            entry_price_usd=entry.execution_price_usd,
            entered_at=aware(entry.filled_at),
            mark_price_usd=None if mark is None else mark.price_usd,
            mark_observed_at=None if mark is None else mark.observed_at,
            liquidity_usd=liquidity,
            min_liquidity_usd=self.limits.min_liquidity_usd,
            now=now,
        )
        return inputs, snapshot_id


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
