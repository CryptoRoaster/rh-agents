"""The automatic exit sweep: evaluate every open PAPER position, exit through the one path.

For each open, case-bound PAPER position the sweep reads the entry fill, the
current mark and the held market's current liquidity and asks `PAPER_EXIT_V1`
for a trigger (`plan`). Only for a position where one fires is the existing
`PaperExitService.execute_position_exit` called, in the order and under the
budget `exitpolicy.drive` decides — shared, in a run, with the early policy.
That service does everything an exit has always done: whole holding, fresh
SENTINEL SELL check, one order per key, one exit per cycle, fill + ledger +
exit record in one transaction. The
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

from collections.abc import Awaitable
from dataclasses import dataclass
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
from src.orchestration.commander.context import SystemPauseUnavailable
from src.orchestration.exitpolicy.drive import (
    PLAN_TIME_BUDGET,
    ExitCandidate,
    alone,
    attempt_exits,
    exit_order,
    judgement_failed,
)
from src.orchestration.exitpolicy.drive import ExitSweep as ExitSweep
from src.orchestration.exitpolicy.policy import (
    ExitInputs,
    ExitVerdict,
    PaperExitPolicy,
    evaluate,
)
from src.orchestration.exitpolicy.refresh import (
    PreExitMarketRefresh,
    RefreshContext,
    RefreshDeadline,
    Unbounded,
)
from src.orchestration.paperexit.models import ExitReading
from src.orchestration.paperexit.service import (
    ExitConfirmation,
    ExitTriggerRecord,
    PaperExitService,
)
from src.orchestration.riskrequest.service import OneSnapshot
from src.orchestration.valuation.service import PositionValuationReader, held_market_identities
from src.orchestration.workflow.models import TradeCase


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
    # Attempts (chain reads) per sweep on its own. `None` is one per exit it may book.
    max_attempts: int | None = None

    async def sweep(
        self,
        *,
        deadline: RefreshDeadline | None = None,
        networks: VerifiedNetworkRegistry | None = None,
    ) -> ExitSweep:
        """This policy's exits on their own, under this service's own budget.

        A run never calls this: its exit phase plans every policy and asks for
        the sales under one shared budget (`src.orchestration.exitpolicy.drive`).
        """
        result = ExitSweep()
        candidates = await self.plan(result, deadline=deadline)
        await attempt_exits(
            exit_order([candidates], self.clock.now()),
            alone(
                max_exits=self.max_exits,
                max_attempts=self.max_attempts,
                max_refreshes=self.max_refreshes,
            ),
            sessions=self.sessions,
            deadline=deadline,
            networks=networks,
        )
        return result

    async def plan(
        self,
        result: ExitSweep,
        *,
        deadline: RefreshDeadline | None = None,
        candidates: list[ExitCandidate] | None = None,
    ) -> list[ExitCandidate]:
        """Judge every open position by PAPER_EXIT_V1 and name the triggered ones.

        Database reads only — nothing here calls a provider or sells. Counts
        are written into `result` and candidates appended to `candidates` as
        they are made, so a judgement cut off part-way keeps both. Positions not
        reached stay counted as `unevaluated`, never as held.
        """
        bound = deadline if deadline is not None else Unbounded()
        found = candidates if candidates is not None else []
        async with self.sessions() as session:
            positions = [
                item
                for item in await self.exits.paper.positions_in_session(session)
                if item.quantity > 0 and item.cycle_id is not None
            ]
            entries = await self._entries(session, positions)
            early = await _early_cycles(session, list(entries.values()))
        own = []
        for position in sorted(positions, key=lambda item: str(item.id)):
            if position.cycle_id not in entries:
                # No case-bound entry: not a position this policy may close.
                result.refused("POSITION_ORIGIN_UNKNOWN")
            elif position.cycle_id in early:
                # PRE_VECTOR_EARLY_ENTRY_V1 has its own exit contract,
                # EARLY_PAPER_EXIT_V1. This policy never closes its positions.
                result.refused("EARLY_POSITION_OWN_EXIT_POLICY")
            else:
                own.append(position)
        result.unevaluated += len(own)
        for position in own:
            if bound.expired:
                result.plan_stop = PLAN_TIME_BUDGET
                break
            entry = entries[position.cycle_id]
            now = self.clock.now()
            try:
                inputs, _ = await self._inputs(position, entry, now)
                verdict = evaluate(self.policy, inputs)
            except SystemPauseUnavailable:
                raise
            except Exception:
                # Issue #111: one position nobody can judge holds up no other.
                judgement_failed(result, position.id)
                continue
            result.unevaluated -= 1
            result.evaluated += 1
            if verdict.trigger is None:
                result.held += 1
                if verdict.reason == "HOLD_MARK_UNKNOWN":
                    result.mark_unknown += 1
                continue
            result.triggered += 1
            code = verdict.trigger.value
            result.triggers[code] = result.triggers.get(code, 0) + 1
            result.triggered_at(mark_age(inputs.mark_observed_at, now))
            found.append(self._candidate(result, position, entry, code, inputs, verdict, now))
        return found

    def _candidate(
        self,
        result: ExitSweep,
        position: Position,
        entry: TradeCaseExecutionRow,
        code: str,
        inputs: ExitInputs,
        verdict: ExitVerdict,
        now: datetime,
    ) -> ExitCandidate:
        key = exit_request_key(self.policy, position.cycle_id, now)

        def start(context: RefreshContext) -> Awaitable[ExitReading]:
            return self.exits.execute_position_exit(
                position.id,
                request_key=key,
                trigger=ExitTriggerRecord(
                    trigger=code,
                    policy_version=self.policy.version,
                    basis=_basis(self.policy, inputs, verdict),
                ),
                confirm=self._confirmer(position, entry, code, inputs, verdict, context),
            )

        return ExitCandidate(
            position_id=position.id,
            request_key=key,
            result=result,
            port=self.refresh,
            start=start,
        )

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
