"""Assembly of the PULSE view: the authoritative condition and one price.

No model participates and nothing is inferred. The reader takes whichever
TRADE_SETUP evidence the workflow currently considers authoritative, copies the
machine-evaluable trigger out of it, reads the latest recorded market
observation, and hands over a bounded view.

Two decisions shape this file.

**Authoritative means the workflow's answer, not the newest-looking row.** The
current setup is selected through the same ``active_evidence`` the evaluator
uses, so a superseded setup is invisible here rather than merely unlikely to be
chosen. A monitor watching a setup the case has moved on from would produce a
trigger nobody could act on, and the runtime would have to catch it later.

**Recorded observations, never a fresh provider call.** The market watcher
already records snapshots on its own cadence, and PULSE reads what it recorded.
A monitor that fetched on every check would become the highest-frequency consumer
of a rate-limited public API to re-read a number that is cached upstream for a
minute anyway. Freshness is enforced against the observation's own source time,
so consuming recorded data costs nothing in correctness.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from src.agents.pulse.models import (
    PriceObservation,
    PulseTaskInput,
    WatchedTrigger,
)
from src.agents.pulse.policy import PULSE_TRIGGER_V1, PulseTriggerPolicy
from src.agents.vector.models import TriggerType
from src.core.clock import Clock, SystemClock
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.workflow.engine import active_evidence, unusable_reason
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceStatus,
    EvidenceType,
    TradeCase,
    TradeSetupPayload,
)


class TradeCaseTriggerSource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class PulseMarketInput(Protocol):
    """Recorded market data only: no DB writes, no provider or transport client."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...

    async def observations(
        self,
        identity: str,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        include_fixtures: bool = False,
    ) -> tuple[MarketSnapshot, ...]: ...


def watched_trigger(envelope: EvidenceEnvelope, now: datetime) -> WatchedTrigger | None:
    """Copy the machine-evaluable condition out of the authoritative setup.

    Returns ``None`` when there is nothing a monitor could watch: evidence the
    workflow considers unusable on its content, or a setup written before the
    trigger grammar existed. Neither is inferred around — a setup whose condition
    is only prose has no condition as far as PULSE is concerned, and guessing one
    from the entry price would be inventing the contract.

    Expiry is the deliberate exception. VECTOR sets an envelope's validity to the
    setup's own expiry, so a setup that has run out is *both* stale evidence and
    an expired condition at the same instant. Treating it only as unusable would
    report "nothing to watch", which is what a monitor says while it waits for a
    new setup — and the watch would go on rechecking a window that has closed
    forever. Surfacing the condition lets the evaluator say what actually
    happened, and lets the runtime end the watch.
    """
    reason = unusable_reason(envelope, now)
    if reason is not None and reason != EvidenceStatus.STALE.value:
        return None
    payload = envelope.payload
    if not isinstance(payload, TradeSetupPayload) or payload.setup is None:
        return None
    detail = payload.setup
    trigger = detail.trigger
    try:
        kind = TriggerType(trigger.type)
    except ValueError:
        # A condition in a grammar this monitor does not implement is not
        # something to approximate.
        return None
    return WatchedTrigger(
        setup_evidence_id=envelope.evidence_id,
        setup_id=payload.setup_id,
        setup_fingerprint=detail.setup_fingerprint,
        type=kind,
        price_basis=detail.price_basis,  # type: ignore[arg-type]
        reference_price=trigger.reference_price,
        zone_low=trigger.zone_low,
        zone_high=trigger.zone_high,
        valid_from=trigger.valid_from,
        expires_at=trigger.expires_at,
    )


# Pydantic's own codes for "this decimal does not fit the declared envelope".
# Named rather than re-derived: restating the arithmetic here would be a second
# rule that could disagree with the one the model actually enforces.
PRICE_ENVELOPE_REFUSALS = frozenset(
    {"decimal_max_places", "decimal_max_digits", "decimal_whole_digits"}
)


def price_observation(snapshot: MarketSnapshot, trade_case: TradeCase) -> PriceObservation | None:
    """The one recorded price, or nothing when the market has no usable one.

    An unknown or unavailable price is absent rather than zero. Nothing here
    substitutes a previous value, and a market that cannot currently be priced
    simply produces no observation to compare.

    **A price outside the comparison envelope is one of those absences.** The
    market layer records whatever a provider reports — its `Amount` allows a
    hundred significant digits — while everything downstream of a trigger holds
    money in the ledger's `Numeric(38, 18)`, which is the envelope `Price`
    declares. A real BNB Smart Chain pool reported a price with nineteen decimal
    places; it recorded correctly, and building the comparison out of it raised
    a validation error that reached the runtime as an unknown handler bug, spent
    an attempt from the failure budget, and had to be classified as an incident
    that had not happened.

    Such a price is *not comparable*, which is exactly what this function's
    `None` already means, so that is what it returns. It is deliberately **not**
    rounded into range: quantizing would change what the market said in the one
    place where a comparison decides whether an order is armed, and it would
    arm on a number the ledger could not then store. Only the envelope refusal
    is read this way — every other validation failure is a wiring fault and
    still raises, because a market answering about the wrong pair must not turn
    into a monitor quietly seeing no price.
    """
    if snapshot.price.status != Availability.AVAILABLE or snapshot.price.value_usd is None:
        return None
    if snapshot.price.value_usd <= 0:
        # The market layer already refuses a non-positive available price; this
        # keeps a malformed one from reaching a comparison if that ever changes.
        return None
    try:
        return PriceObservation(
            observation_id=snapshot.price.id,
            snapshot_id=snapshot.id,
            pair_id=snapshot.pair.pair_id,
            chain=snapshot.chain,
            network=snapshot.network,
            venue=snapshot.pair.venue,
            base_asset_id=trade_case.market.base_asset_id,
            quote_asset_id=trade_case.market.quote_asset_id,
            provider=snapshot.provider,
            is_fixture=snapshot.is_fixture,
            price=snapshot.price.value_usd,
            observed_at=snapshot.price.observed_at,
        )
    except ValidationError as error:
        beyond_envelope = [
            item
            for item in error.errors()
            if item["loc"] == ("price",) and item["type"] in PRICE_ENVELOPE_REFUSALS
        ]
        if len(beyond_envelope) != len(error.errors()):
            raise
        return None


@dataclass(frozen=True)
class PulseContextReader:
    """Assembles the trigger view from existing workflow and market services."""

    cases: TradeCaseTriggerSource
    markets: PulseMarketInput
    policy: PulseTriggerPolicy = PULSE_TRIGGER_V1
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def trigger_context(self, trade_case_id: UUID, task_id: UUID) -> PulseTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        now = self.clock.now()
        current = active_evidence(await self.cases.evidence(trade_case_id))
        setup = current.get(EvidenceType.TRADE_SETUP)
        trigger = None if setup is None else watched_trigger(setup, now)

        window: tuple[MarketSnapshot, ...] = ()
        truncated = False
        if trigger is not None:
            # Everything recorded since the later of "the setup began" and "the
            # freshness window opened". A crossing outside either bound is not an
            # opportunity: one predates the proposal, the other is too old to be
            # a statement about now.
            since = max(trigger.valid_from, now - self.policy.max_observation_age)
            window = await self.markets.observations(
                trade_case.market.pair_id,
                since=since,
                until=now + self.policy.max_clock_skew,
                limit=self.policy.max_observations,
                include_fixtures=self.include_fixtures,
            )
            # The read returns one extra row precisely so this is knowable.
            truncated = len(window) > self.policy.max_observations
            window = window[: self.policy.max_observations]

        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        # A market read that answered about the wrong pair is a wiring fault, and
        # it is left for the evaluator to refuse explicitly rather than silently
        # dropped here — a monitor that quietly saw no price would wait forever
        # without anyone learning why.
        return PulseTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            market_pair_id=trade_case.market.pair_id,
            trigger=trigger,
            observations=tuple(
                observation
                for observation in (price_observation(item, trade_case) for item in window)
                if observation is not None
            ),
            window_truncated=truncated,
            latest=None if snapshot is None else price_observation(snapshot, trade_case),
            policy_version=self.policy.version,
            evaluated_at=now,
        )
