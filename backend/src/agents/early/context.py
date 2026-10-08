"""Assembly of the EARLY view: the facts that make a market early-eligible.

No model participates. The reader refuses, in a fixed order, everything that
would make a pre-VECTOR entry anything other than what the strategy says:

1. a case that is not a PRE_VECTOR_EARLY_ENTRY_V1 case of the early workflow;
2. no usable ATLAS evidence yet (a wait), or ATLAS evidence without the
   chain-side creation timestamp (a refusal — the age is unprovable);
3. a token older than the strategy's maximum age, measured from that
   timestamp and never from when the scout first saw the pool;
4. no fresh recorded price (a wait);
5. a history VECTOR's own ``assess`` does not call *too young*: SUFFICIENT
   belongs to the normal VECTOR path, and a series for the wrong market, in
   the wrong unit or on the wrong timeframe is a fault;
6. a young series that has stopped arriving or is mostly gaps. VECTOR's
   ``assess`` reports a series shorter than 24 bars as too short before it
   ever looks at age or gaps, so the same ``assess`` is asked a second time
   with the bar minimum lowered to one and every other threshold unchanged.
"""

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from src.agents.early.models import EarlyTaskInput
from src.agents.early.ports import EarlyContextPending, EarlyContextUnavailable
from src.agents.vector.policy import VECTOR_SETUP_V1, VectorSetupPolicy
from src.agents.vector.sufficiency import VectorMarketDataSufficiency, assess
from src.core.clock import Clock, SystemClock
from src.markets.history import MarketHistorySource, MarketHistoryUnavailable
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.strategy.early import (
    EARLY_ENTRY_V1,
    EARLY_WORKFLOW_VERSION,
    EarlyEntryPolicy,
    is_early,
)
from src.orchestration.workflow.engine import active_evidence, unusable_reason
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceType,
    OnchainPayload,
    TradeCase,
)

# VECTOR's own policy with one change: a single closed bar is enough to judge
# whether the series is current and continuous. Not a new heuristic — the same
# function, the same age and gap thresholds, the same request.
YOUNG_HISTORY_POLICY = replace(
    VECTOR_SETUP_V1,
    version="pre-vector-young-history-v1",
    min_closed_bars=1,
    max_setup_lifetime=timedelta(hours=1),
)


class EarlyCaseSource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class EarlyMarketInput(Protocol):
    """Recorded market data only: no DB writes, no provider or transport client."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


@dataclass(frozen=True)
class EarlyContextReader:
    cases: EarlyCaseSource
    markets: EarlyMarketInput
    history: MarketHistorySource
    policy: EarlyEntryPolicy = EARLY_ENTRY_V1
    vector: VectorSetupPolicy = VECTOR_SETUP_V1
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def early_context(self, trade_case_id: UUID, task_id: UUID) -> EarlyTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        if (
            not is_early(trade_case.strategy_policy_id)
            or trade_case.workflow_version != EARLY_WORKFLOW_VERSION
        ):
            # Never inferred: only a case the early intake opened is one.
            raise EarlyContextUnavailable("EARLY_STRATEGY_MISMATCH")
        current = active_evidence(await self.cases.evidence(trade_case_id))
        now = self.clock.now()

        onchain = current.get(EvidenceType.ONCHAIN)
        if onchain is None or unusable_reason(onchain, now) is not None:
            raise EarlyContextPending("ATLAS_EVIDENCE_PENDING")
        payload = onchain.payload
        intelligence = payload.intelligence if isinstance(payload, OnchainPayload) else None
        funding = None if intelligence is None else intelligence.funding_graph
        prelaunch = None if funding is None else funding.prelaunch
        if (
            prelaunch is None
            or prelaunch.creation_timestamp is None
            or prelaunch.creation_block is None
            or prelaunch.creation_time_source is None
        ):
            raise EarlyContextUnavailable("EARLY_CREATION_TIME_UNAVAILABLE")
        age = now - prelaunch.creation_timestamp
        if age < timedelta(0):
            raise EarlyContextUnavailable("EARLY_CREATION_TIME_IN_FUTURE")
        if age > self.policy.max_age:
            raise EarlyContextUnavailable("EARLY_CANDIDATE_TOO_OLD")

        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        if snapshot is None:
            raise EarlyContextPending("MARKET_OBSERVATION_PENDING")
        if snapshot.pair.pair_id != trade_case.market.pair_id:
            raise EarlyContextUnavailable("MARKET_IDENTITY_MISMATCH")
        if snapshot.observed_at > now:
            raise EarlyContextUnavailable("MARKET_OBSERVATION_IN_FUTURE")
        if now - snapshot.freshness_at > self.policy.max_input_age:
            raise EarlyContextPending("MARKET_OBSERVATION_PENDING")
        price = snapshot.price
        if (
            price.status != Availability.AVAILABLE
            or price.value_usd is None
            or price.value_usd <= 0
        ):
            raise EarlyContextPending("MARKET_OBSERVATION_PENDING")

        try:
            history = await self.history.history(
                trade_case.market,
                timeframe=self.vector.history_timeframe,
                aggregate=self.vector.history_aggregate,
                bars=self.vector.history_bars,
            )
        except MarketHistoryUnavailable as error:
            raise EarlyContextUnavailable(error.reason_code) from None
        verdict = assess(history, trade_case.market, now, self.vector)
        if verdict is VectorMarketDataSufficiency.SUFFICIENT:
            # The normal VECTOR path owns a market with enough history.
            raise EarlyContextUnavailable("EARLY_VECTOR_HISTORY_SUFFICIENT")
        if verdict.value not in self.policy.allowed_history:
            raise EarlyContextUnavailable(verdict.value)
        young: VectorMarketDataSufficiency | None = None
        if history.bars:
            young = assess(history, trade_case.market, now, YOUNG_HISTORY_POLICY)
            if young is not VectorMarketDataSufficiency.SUFFICIENT:
                raise EarlyContextUnavailable(young.value)

        existing = current.get(EvidenceType.TRADE_SETUP)
        return EarlyTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            strategy_policy_id=trade_case.strategy_policy_id or "",
            workflow_version=trade_case.workflow_version,
            pair_id=trade_case.market.pair_id,
            chain=trade_case.market.chain,
            network=trade_case.market.network,
            base_asset_id=trade_case.market.base_asset_id,
            market_snapshot_id=snapshot.id,
            price_observation_id=price.id,
            reference_price=price.value_usd,
            price_observed_at=price.observed_at,
            vector_sufficiency=verdict.value,
            young_history_sufficiency=None if young is None else young.value,
            closed_bars=len(history.bars),
            history_provider=history.provider,
            history_timeframe=history.timeframe,
            history_aggregate=history.aggregate,
            history_requested_bars=history.requested_bars,
            history_policy_version=self.vector.version,
            creation_block=prelaunch.creation_block,
            creation_timestamp=prelaunch.creation_timestamp,
            creation_time_source=prelaunch.creation_time_source,
            onchain_evidence_id=onchain.evidence_id,
            candidate_age_seconds=int(age.total_seconds()),
            max_age_seconds=int(self.policy.max_age.total_seconds()),
            evaluated_at=now,
            supersedes_evidence_id=None if existing is None else existing.evidence_id,
        )
