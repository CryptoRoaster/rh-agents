"""Assembly of the VECTOR view, the input digest, and the setup fingerprint.

No model participates here. The reader takes the recorded market snapshot and
whatever evidence the workflow already considers current, refuses anything too
old or too incomplete to build a price level on, and hands over one bounded view.

Two fingerprints matter and they answer different questions. The **input digest**
identifies what VECTOR was shown, so an unchanged market produces an unchanged
digest however often it is read. The **setup fingerprint** identifies the
proposal itself, so two identical setups are one setup and a single moved level
is a different one — which is what lets a future PULSE reference a specific
setup rather than a TradeCase that keeps changing underneath it.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from src.agents.vector.models import (
    PRICE_BASIS,
    EvidenceSummary,
    ObservedBar,
    ObservedMeasurement,
    TriggerCondition,
    VectorMarketContext,
    VectorMarketStructure,
    VectorSetup,
    VectorSetupProposal,
    VectorTaskInput,
)
from src.agents.vector.policy import VECTOR_SETUP_V1, VectorSetupPolicy
from src.agents.vector.ports import VectorContextUnavailable
from src.agents.vector.sufficiency import VectorMarketDataSufficiency, assess
from src.core.clock import Clock, SystemClock
from src.core.numbers import canonical_decimal
from src.markets.history import (
    MarketHistory,
    MarketHistorySource,
    MarketHistoryUnavailable,
    UnconfiguredHistorySource,
    interval_seconds,
)
from src.markets.models import Availability, MarketIdentity, MarketSnapshot, Measurement
from src.orchestration.workflow.engine import active_evidence, unusable_reason
from src.orchestration.workflow.models import EvidenceEnvelope, EvidenceType, TradeCase

# Which other specialists' conclusions VECTOR may see. ORBIT, ATLAS and SIGNAL
# are the pre-trigger analytical roles; TRIGGER and LIQUIDITY_EXECUTION are
# downstream of a setup and reading them here would be circular.
READABLE_EVIDENCE = (EvidenceType.DISCOVERY, EvidenceType.ONCHAIN, EvidenceType.SENTIMENT)


class TradeCaseIdentitySource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class VectorMarketInput(Protocol):
    """Recorded market data only: no DB writes, no provider or transport client."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


def measurement_view(measurement: Measurement) -> ObservedMeasurement:
    """Copy a recorded measurement without ever collapsing UNKNOWN into zero."""
    return ObservedMeasurement(
        observation_id=measurement.id,
        status=measurement.status,
        value_usd=measurement.value_usd,
        observed_at=measurement.observed_at,
    )


def structure_view(history: MarketHistory, now: datetime) -> VectorMarketStructure:
    """Flatten a recorded series into the bounded view the model is shown.

    A copy rather than a reference, because the input digest has to fingerprint
    precisely the numbers that were shown. Retrieval time is not copied: it says
    when we looked, and including it would make one unchanged window hash
    differently on every pass.
    """
    assert history.bars and history.observed_at is not None and history.window_start is not None
    return VectorMarketStructure(
        provider=history.provider,
        timeframe=history.timeframe,
        interval_seconds=interval_seconds(history.timeframe, history.aggregate),
        bars=tuple(
            ObservedBar(
                opened_at=bar.opened_at,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in history.bars
        ),
        coverage=history.coverage.value,
        requested_bars=history.requested_bars,
        missing_intervals=history.missing_intervals,
        window_start=history.window_start,
        window_end=history.observed_at,
        age_seconds=max(0, int((now - history.observed_at).total_seconds())),
    )


def structure_document(structure: VectorMarketStructure) -> dict[str, object]:
    """The canonical form of the market structure, built exactly once.

    The model document, the input digest and the durable record all come through
    here. One canonicalization means a stored snapshot cannot drift from what was
    hashed, and a later reconstruction cannot disagree with the original for
    formatting reasons rather than substantive ones.

    Retrieval time is absent by construction: it says when we looked, never what
    the market did. Source bar timestamps are present, because they are the
    market's own account of itself.
    """
    return {
        "provider": structure.provider,
        "timeframe": structure.timeframe,
        "interval_seconds": structure.interval_seconds,
        "price_basis": structure.price_basis,
        "coverage": structure.coverage,
        "requested_bars": structure.requested_bars,
        "missing_intervals": structure.missing_intervals,
        "window_start": structure.window_start.isoformat(),
        "window_end": structure.window_end.isoformat(),
        "observed_range_low": canonical_decimal(structure.range_low),
        "observed_range_high": canonical_decimal(structure.range_high),
        "bars": [
            {
                "opened_at": bar.opened_at.isoformat(),
                "open": canonical_decimal(bar.open),
                "high": canonical_decimal(bar.high),
                "low": canonical_decimal(bar.low),
                "close": canonical_decimal(bar.close),
                "volume": canonical_decimal(bar.volume),
            }
            for bar in structure.bars
        ],
    }


def structure_digest(structure: VectorMarketStructure) -> str:
    """Fingerprint of the market structure alone.

    Distinct from the input digest, which also covers the price snapshot and the
    other roles' conclusions. This one is self-contained, so the structure stored
    on an accepted setup can be reconstructed and verified on its own — without
    still possessing the rest of the input, and without the provider still
    serving the same bars.
    """
    return _canonical_digest(structure_document(structure))


def _canonical_digest(document: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def _summary(
    envelope: EvidenceEnvelope, headline: str | None, codes: tuple[str, ...]
) -> EvidenceSummary:
    return EvidenceSummary(
        evidence_id=envelope.evidence_id,
        evidence_type=envelope.evidence_type.value,
        status=envelope.status.value,
        acceptance=envelope.payload.acceptance().value,
        headline=headline,
        codes=codes[:12],
    )


def evidence_summaries(
    current: dict[EvidenceType, EvidenceEnvelope], now: datetime
) -> tuple[EvidenceSummary, ...]:
    """Flatten the other roles' current conclusions into bounded codes.

    Only evidence the workflow already considers usable is included. A blocked
    or stale finding is not quietly downgraded into context a setup could be
    built on top of — it is simply absent, and its absence is visible.
    """
    summaries: list[EvidenceSummary] = []
    for evidence_type in READABLE_EVIDENCE:
        envelope = current.get(evidence_type)
        if envelope is None:
            continue
        if unusable_reason(envelope, now) is not None:
            continue
        payload = envelope.payload
        headline: str | None = None
        codes: tuple[str, ...] = ()
        if evidence_type == EvidenceType.ONCHAIN:
            intelligence = getattr(payload, "intelligence", None)
            headline = None if intelligence is None else intelligence.verdict
            codes = () if intelligence is None else tuple(intelligence.blockers)
        elif evidence_type == EvidenceType.SENTIMENT:
            intelligence = getattr(payload, "intelligence", None)
            headline = getattr(payload, "assessment", None)
            codes = (
                ()
                if intelligence is None
                else (
                    intelligence.organic_breadth,
                    intelligence.manipulation_concern,
                    intelligence.data_quality,
                )
            )
        else:
            assessment = getattr(payload, "assessment", None)
            headline = None if assessment is None else assessment.classification
            codes = () if assessment is None else tuple(assessment.reason_codes)
        summaries.append(_summary(envelope, headline, codes))
    return tuple(summaries)


@dataclass(frozen=True)
class VectorContextReader:
    """Assembles the setup view from existing market and workflow services."""

    cases: TradeCaseIdentitySource
    markets: VectorMarketInput
    # Recorded market structure. Defaults to the source that says no provider is
    # wired, so a deployment that forgot to configure one produces a refusal
    # rather than a setup drawn from a single price.
    history: MarketHistorySource = UnconfiguredHistorySource()
    policy: VectorSetupPolicy = VECTOR_SETUP_V1
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def setup_context(self, trade_case_id: UUID, task_id: UUID) -> VectorTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        if snapshot is None:
            raise VectorContextUnavailable("MARKET_OBSERVATION_MISSING")
        if snapshot.pair.pair_id != trade_case.market.pair_id:
            raise VectorContextUnavailable("MARKET_IDENTITY_MISMATCH")
        now = self.clock.now()
        if snapshot.observed_at > now:
            raise VectorContextUnavailable("MARKET_OBSERVATION_IN_FUTURE")
        if now - snapshot.freshness_at > self.policy.max_input_age:
            # A setup is a statement about price levels. Built on a price that is
            # no longer current it would be a statement about a market that has
            # moved, so no model is asked and no setup is produced.
            raise VectorContextUnavailable("MARKET_OBSERVATION_TOO_STALE")
        if snapshot.price.status != Availability.AVAILABLE or snapshot.price.value_usd is None:
            # There is no level to reason from. Not a zero, not a guess.
            raise VectorContextUnavailable("PRICE_UNAVAILABLE")

        structure = await self._structure(trade_case.market, now)
        market = VectorMarketContext(
            snapshot_id=snapshot.id,
            pair_id=snapshot.pair.pair_id,
            chain=snapshot.chain,
            network=snapshot.network,
            venue=snapshot.pair.venue,
            base_asset_id=trade_case.market.base_asset_id,
            quote_asset_id=trade_case.market.quote_asset_id,
            base_symbol=snapshot.pair.base.symbol,
            provider=snapshot.provider,
            is_fixture=snapshot.is_fixture,
            observed_at=snapshot.observed_at,
            age_seconds=max(0, int((now - snapshot.observed_at).total_seconds())),
            price=measurement_view(snapshot.price),
            liquidity=measurement_view(snapshot.liquidity),
            volume=measurement_view(snapshot.volume),
            volume_window_seconds=snapshot.volume.window_seconds,
            structure=structure,
        )
        current = active_evidence(await self.cases.evidence(trade_case_id))
        existing = current.get(EvidenceType.TRADE_SETUP)
        return VectorTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            market=market,
            evidence=evidence_summaries(current, now),
            policy_version=self.policy.version,
            evaluated_at=now,
            supersedes_evidence_id=existing.evidence_id if existing is not None else None,
        )

    async def _structure(self, identity: MarketIdentity, now: datetime) -> VectorMarketStructure:
        """Obtain recorded structure, or refuse before anything is asked of a model.

        Exactly one provider read per context acquisition. The Phase 2B runtime
        owns retries, so a failure here ends the attempt and is retried as a task
        rather than re-fetched in a loop that would multiply provider requests.
        """
        try:
            history = await self.history.history(
                identity,
                timeframe=self.policy.history_timeframe,
                aggregate=self.policy.history_aggregate,
                bars=self.policy.history_bars,
            )
        except MarketHistoryUnavailable as error:
            raise VectorContextUnavailable(error.reason_code) from None
        verdict = assess(history, identity, now, self.policy)
        if verdict != VectorMarketDataSufficiency.SUFFICIENT:
            # No model call. A market whose structure cannot be established is one
            # this system has nothing to say about, and saying nothing is the
            # correct output rather than a gap to be filled in.
            raise VectorContextUnavailable(verdict.value)
        return structure_view(history, now)


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


def setup_document(task_input: VectorTaskInput) -> dict[str, object]:
    """The exact document VECTOR is shown, built once.

    Both the provider payload and the input digest come from here, so the digest
    always fingerprints precisely what the model saw. Decimals use a canonical
    textual form and never pass through a float.
    """
    market = task_input.market
    return {
        "pair_id": market.pair_id,
        "chain": market.chain,
        "network": market.network,
        "venue": market.venue,
        "base_asset_id": market.base_asset_id,
        "quote_asset_id": market.quote_asset_id,
        "base_symbol": market.base_symbol,
        "provider": market.provider,
        "is_fixture": market.is_fixture,
        "price_basis": PRICE_BASIS,
        "snapshot_id": str(market.snapshot_id),
        "observed_at": market.observed_at.isoformat(),
        "price": _measurement_document(market.price),
        "liquidity": _measurement_document(market.liquidity),
        "volume": _measurement_document(market.volume),
        "volume_window_seconds": market.volume_window_seconds,
        # The closed bars themselves, so the digest fingerprints the structure
        # that was shown rather than a summary of it. Bar age is excluded for the
        # same reason snapshot age is.
        "market_structure": structure_document(market.structure),
        "evidence": [
            {
                "evidence_id": str(item.evidence_id),
                "evidence_type": item.evidence_type,
                "status": item.status,
                "acceptance": item.acceptance,
                "headline": item.headline,
                "codes": list(item.codes),
            }
            for item in task_input.evidence
        ],
        "policy_version": task_input.policy_version,
    }


def vector_input_digest(task_input: VectorTaskInput) -> str:
    """Canonical fingerprint of exactly what VECTOR was given.

    Identifies the input only, and is never a claim that the model's output is
    deterministic. Observation age is excluded because it is relative to the
    moment of reading, so including it would make one unchanged snapshot hash
    differently on every pass.
    """
    return _canonical_digest(setup_document(task_input))


def setup_fingerprint(
    proposal: VectorSetupProposal,
    trigger: TriggerCondition,
    task_input: VectorTaskInput,
    input_digest: str,
) -> str:
    """Canonical fingerprint of the setup itself.

    Binds the geometry, the trigger, the expiry, the market and the input it was
    drawn from. A future PULSE watches a specific setup, so "the same setup"
    has to be a decidable question rather than a matter of which TradeCase it
    happens to hang from.
    """
    canonical = json.dumps(
        {
            "market": task_input.market.pair_id,
            "chain": task_input.market.chain,
            "network": task_input.market.network,
            "base_asset_id": task_input.market.base_asset_id,
            "price_basis": PRICE_BASIS,
            "kind": proposal.kind.value,
            "side": proposal.side.value,
            "entry_low": canonical_decimal(proposal.entry_low),
            "entry_high": canonical_decimal(proposal.entry_high),
            "invalidation_price": canonical_decimal(proposal.invalidation_price),
            "targets": [canonical_decimal(target) for target in proposal.targets],
            "trigger_type": trigger.type.value,
            "trigger_reference": (
                None
                if trigger.reference_price is None
                else canonical_decimal(trigger.reference_price)
            ),
            "trigger_zone": (
                None
                if trigger.zone_low is None or trigger.zone_high is None
                else [canonical_decimal(trigger.zone_low), canonical_decimal(trigger.zone_high)]
            ),
            "expires_at": proposal.expires_at.isoformat(),
            "policy_version": task_input.policy_version,
            "input_digest": input_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def build_setup(
    proposal: VectorSetupProposal,
    trigger: TriggerCondition,
    task_input: VectorTaskInput,
    input_digest: str,
) -> VectorSetup:
    """Turn an accepted proposal into the recorded setup, with system identity.

    The model does not mint the identifier. It is derived from the fingerprint,
    so an identical proposal over identical input is the same setup and any
    moved level is a different one.
    """
    fingerprint = setup_fingerprint(proposal, trigger, task_input, input_digest)
    return VectorSetup(
        setup_id=uuid5(NAMESPACE_URL, f"rh-agents:vector:setup:{fingerprint}"),
        setup_fingerprint=fingerprint,
        policy_version=task_input.policy_version,
        kind=proposal.kind,
        side=proposal.side,
        entry_low=proposal.entry_low,
        entry_high=proposal.entry_high,
        invalidation_price=proposal.invalidation_price,
        targets=proposal.targets,
        trigger=trigger,
        expires_at=proposal.expires_at,
        reason_codes=proposal.reason_codes,
        summary=proposal.summary,
        reference_price=task_input.latest_price,
        input_digest=input_digest,
    )


def reasoning_payload(task_input: VectorTaskInput) -> dict[str, object]:
    """The quoted data document handed to the provider, with no instructions in it."""
    document = setup_document(task_input)
    document["age_seconds"] = task_input.market.age_seconds
    document["market_structure_age_seconds"] = task_input.market.structure.age_seconds
    document["evaluated_at"] = task_input.evaluated_at.isoformat()
    document["setup_horizon"] = {
        "minimum_seconds": int(VECTOR_SETUP_V1.min_setup_lifetime.total_seconds()),
        "maximum_seconds": int(VECTOR_SETUP_V1.max_setup_lifetime.total_seconds()),
    }
    return {"market_context": document}
