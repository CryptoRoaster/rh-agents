"""The single narrow read path ORBIT is given, and the digest of what it saw.

ORBIT never receives a session, repository, provider client or RPC client. It
receives one purpose-built view assembled here from services that already own
market data, so market logic is not duplicated and GeckoTerminal is never called
from a worker.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from src.agents.orbit.models import (
    ObservedMeasurement,
    OrbitCandidateContext,
    OrbitEvaluationInput,
    OrbitTaskInput,
    age_seconds,
)
from src.core.clock import Clock, SystemClock
from src.core.numbers import canonical_decimal
from src.markets.models import MarketCandidate, MarketSnapshot, Measurement
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import EvidenceEnvelope, EvidenceType, TradeCase


class OrbitContextUnavailable(Exception):
    """No usable discovery context exists. Carries a safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class OrbitContextPort(Protocol):
    """ORBIT's only read capability."""

    async def candidate_context(self, trade_case_id: UUID, task_id: UUID) -> OrbitTaskInput: ...


class OrbitMarketInput(Protocol):
    """Recorded market data only: no DB writes, no provider or transport client.

    Trusted infrastructure implements this port. A worker never receives it
    directly; the context reader below turns it into one bounded candidate view.
    """

    async def candidates(
        self, *, include_fixtures: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[MarketCandidate, ...]: ...

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


class TradeCaseIdentitySource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


def measurement_view(measurement: Measurement) -> ObservedMeasurement:
    """Copy a recorded measurement without ever collapsing UNKNOWN into zero."""
    return ObservedMeasurement(
        observation_id=measurement.id,
        status=measurement.status,
        value_usd=measurement.value_usd,
        observed_at=measurement.observed_at,
    )


@dataclass(frozen=True)
class OrbitContextReader:
    """Assembles the discovery view from existing market and workflow services."""

    cases: TradeCaseIdentitySource
    markets: OrbitMarketInput
    liquidity_floor_usd: Decimal
    max_input_age: timedelta
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def candidate_context(self, trade_case_id: UUID, task_id: UUID) -> OrbitTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        if snapshot is None:
            raise OrbitContextUnavailable("MARKET_OBSERVATION_MISSING")
        if snapshot.pair.pair_id != trade_case.market.pair_id:
            raise OrbitContextUnavailable("MARKET_IDENTITY_MISMATCH")
        evaluation = evaluation_input(
            snapshot,
            liquidity_floor_usd=self.liquidity_floor_usd,
            max_input_age=self.max_input_age,
            now=self.clock.now(),
        )
        # Opening a case records a provenance discovery envelope. ORBIT's verified
        # assessment supersedes it, so only the one identifier it must replace is
        # read here; no other role's evidence reaches the worker.
        current = active_evidence(await self.cases.evidence(trade_case_id))
        existing = current.get(EvidenceType.DISCOVERY)
        return OrbitTaskInput(
            **dict(evaluation),
            trade_case_id=trade_case_id,
            task_id=task_id,
            discovery_reference=trade_case.originating_discovery_reference,
            supersedes_evidence_id=existing.evidence_id if existing is not None else None,
        )


def evaluation_input(
    snapshot: MarketSnapshot,
    *,
    liquidity_floor_usd: Decimal,
    max_input_age: timedelta,
    now: datetime,
) -> OrbitEvaluationInput:
    """The ORBIT input for one recorded snapshot, or a typed refusal.

    The one place a snapshot becomes what ORBIT is shown. The TradeCase reader
    above and the early-discovery scout both come through here, so a market
    looks identical to ORBIT whoever is asking about it, and the same
    freshness rule refuses it before any model is called.
    """
    # Discovery freshness is its own policy, deliberately separate from the
    # execution and risk thresholds that ANCHOR and SENTINEL apply later.
    if snapshot.observed_at > now:
        raise OrbitContextUnavailable("MARKET_OBSERVATION_IN_FUTURE")
    if now - snapshot.freshness_at > max_input_age:
        raise OrbitContextUnavailable("MARKET_OBSERVATION_TOO_STALE")
    candidate = OrbitCandidateContext(
        snapshot_id=snapshot.id,
        pair_id=snapshot.pair.pair_id,
        chain=snapshot.chain,
        network=snapshot.network,
        venue=snapshot.pair.venue,
        base_symbol=snapshot.pair.base.symbol,
        quote_symbol=snapshot.pair.quote.symbol,
        provider=snapshot.provider,
        is_fixture=snapshot.is_fixture,
        observed_at=snapshot.observed_at,
        age_seconds=age_seconds(snapshot.observed_at, now),
        price=measurement_view(snapshot.price),
        liquidity=measurement_view(snapshot.liquidity),
        volume=measurement_view(snapshot.volume),
        volume_window_seconds=snapshot.volume.window_seconds,
    )
    return OrbitEvaluationInput(
        candidate=candidate,
        discovery_liquidity_floor_usd=liquidity_floor_usd,
        evaluated_at=now,
    )


def _measurement_document(measurement: ObservedMeasurement) -> dict[str, object]:
    return {
        "observation_id": str(measurement.observation_id),
        "status": measurement.status.value,
        # Null, never 0: an unobserved value is a different fact from a zero one.
        "value_usd": (
            None if measurement.value_usd is None else canonical_decimal(measurement.value_usd)
        ),
        "observed_at": measurement.observed_at.isoformat(),
    }


def observation_document(task_input: OrbitEvaluationInput) -> dict[str, object]:
    """The exact document ORBIT is shown, built in one place.

    Both the provider payload and the input digest come from here, so the digest
    always fingerprints precisely what the model saw. Decimals use a canonical
    textual form and never pass through a float.
    """
    candidate = task_input.candidate
    return {
        "snapshot_id": str(candidate.snapshot_id),
        "pair_id": candidate.pair_id,
        "chain": candidate.chain,
        "network": candidate.network,
        "venue": candidate.venue,
        "base_symbol": candidate.base_symbol,
        "quote_symbol": candidate.quote_symbol,
        "provider": candidate.provider,
        "is_fixture": candidate.is_fixture,
        "observed_at": candidate.observed_at.isoformat(),
        "price": _measurement_document(candidate.price),
        "liquidity": _measurement_document(candidate.liquidity),
        "volume": _measurement_document(candidate.volume),
        "volume_window_seconds": candidate.volume_window_seconds,
        "discovery_liquidity_floor_usd": canonical_decimal(
            task_input.discovery_liquidity_floor_usd
        ),
    }


def orbit_input_digest(task_input: OrbitEvaluationInput) -> str:
    """Canonical fingerprint of exactly what ORBIT was shown.

    This identifies the input only. It is never a claim that the model's output is
    deterministic. Observation age is deliberately excluded: it is relative to the
    moment of reading, so including it would make one unchanged observation hash
    differently on every pass.
    """
    canonical = json.dumps(
        observation_document(task_input),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def reasoning_payload(task_input: OrbitEvaluationInput) -> dict[str, object]:
    """The quoted data document handed to the provider, with no instructions in it."""
    document = observation_document(task_input)
    document["age_seconds"] = task_input.candidate.age_seconds
    return {"market_observation": document}
