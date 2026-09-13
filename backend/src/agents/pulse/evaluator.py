"""The deterministic trigger evaluation. No model, no provider, no judgement.

This is the whole of PULSE's decision-making: a sequence of exact comparisons
over Decimals and timezone-aware instants, in a fixed order, with one answer at
the end. Given the same input it returns the same result, always, and nothing it
can be told changes what it concludes.

**Order matters and is deliberate.** Identity and units are checked before
anything else, because a price from another market or in another unit is not a
price that has failed to cross a threshold — it is a number that cannot be
compared to this condition at all, and reporting it as "not yet" would schedule a
patient wait for an answer that can never arrive. Time is checked before value,
because a price outside the setup's own window cannot trigger it however
favourable the number looks.

**No smoothing, no confirmation, no tolerance.** The first fresh, valid,
authoritative observation satisfying the condition triggers it. There is no
moving average, no epsilon, no "two consecutive ticks" rule and no debounce.
Those would each be a trading policy wearing the costume of a safety measure, and
none of them is in the trigger contract VECTOR wrote. If confirmation is ever
wanted it belongs in a separately versioned policy that says so out loud.
"""

from datetime import datetime

from src.agents.pulse.models import (
    PriceObservation,
    PulseReasonCode,
    PulseTaskInput,
    TriggerEvaluation,
    TriggerOutcome,
    WatchedTrigger,
)
from src.agents.pulse.policy import PULSE_TRIGGER_V1, PulseTriggerPolicy
from src.agents.vector.models import PRICE_BASIS, TriggerType


def condition_met(trigger: WatchedTrigger, price: object) -> bool:
    """Whether the observed price satisfies the condition, exactly.

    Decimal comparison throughout; no float ever participates. The boundary is
    inclusive on both threshold forms because VECTOR's grammar names them
    ``PRICE_GTE`` and ``PRICE_LTE`` — a price exactly at the level has reached
    it, and an exclusive comparison would silently mean something the setup did
    not say.
    """
    from decimal import Decimal

    assert isinstance(price, Decimal)
    if trigger.type == TriggerType.PRICE_GTE:
        assert trigger.reference_price is not None
        return price >= trigger.reference_price
    if trigger.type == TriggerType.PRICE_LTE:
        assert trigger.reference_price is not None
        return price <= trigger.reference_price
    assert trigger.zone_low is not None and trigger.zone_high is not None
    return trigger.zone_low <= price <= trigger.zone_high


def _waiting(outcome: TriggerOutcome, reason: PulseReasonCode, now: datetime) -> TriggerEvaluation:
    return TriggerEvaluation(outcome=outcome, reason_code=reason, evaluated_at=now)


def _incomparable(
    observation: PriceObservation,
    task_input: PulseTaskInput,
    trigger: WatchedTrigger,
    now: datetime,
    policy: PulseTriggerPolicy,
) -> PulseReasonCode | None:
    """Why this observation cannot be compared to this condition at all.

    Only identity, units and an impossible timestamp qualify. Each says the
    reading does not belong to this question — a price from another pool or in
    another unit is not a level not yet reached, and calling it "not yet" would
    schedule a patient wait for an answer that can never arrive.
    """
    if observation.pair_id != task_input.market_pair_id:
        return PulseReasonCode.MARKET_IDENTITY_MISMATCH
    if observation.price_basis != trigger.price_basis or observation.price_basis != PRICE_BASIS:
        # No conversion and no reciprocal inference. Comparing a threshold in one
        # unit to a price in another gives a numerically valid answer to a
        # question nobody asked.
        return PulseReasonCode.PRICE_BASIS_MISMATCH
    if observation.observed_at > now + policy.max_clock_skew:
        return PulseReasonCode.OBSERVATION_IN_FUTURE
    return None


def _in_window(
    observation: PriceObservation,
    trigger: WatchedTrigger,
    now: datetime,
    policy: PulseTriggerPolicy,
) -> bool:
    """Whether this reading is one this check is entitled to act on.

    Outside these bounds an observation is simply not part of the question, so it
    is passed over rather than refused: it predates the setup, or it has aged out
    of the freshness window, or it belongs after the setup ended. The context
    builds its window to these same bounds; checking again here means the
    evaluator is correct whoever assembled its input.
    """
    if observation.observed_at < trigger.valid_from:
        return False
    if observation.observed_at >= trigger.expires_at:
        return False
    return now - observation.observed_at <= policy.max_observation_age


def evaluate(
    task_input: PulseTaskInput,
    now: datetime,
    policy: PulseTriggerPolicy = PULSE_TRIGGER_V1,
) -> TriggerEvaluation:
    """Decide whether the authoritative condition has become true.

    The window is scanned oldest first and the **first** qualifying observation
    wins, so the evidence answers "when did this system first observe the
    trigger?" with a stable fact rather than with whichever row happened to be
    newest when somebody looked.
    """
    trigger = task_input.trigger
    if trigger is None:
        # Nothing current to watch. A new setup may still arrive, so this is a
        # wait rather than the end of the watch.
        return _waiting(TriggerOutcome.NO_CURRENT_SETUP, PulseReasonCode.NO_CURRENT_SETUP, now)

    # The setup's own window, judged against trusted time. A watch that outlived
    # its setup must stop rather than keep asking.
    if now >= trigger.expires_at:
        return _waiting(TriggerOutcome.SETUP_EXPIRED, PulseReasonCode.SETUP_EXPIRED, now)
    if now < trigger.valid_from:
        return _waiting(TriggerOutcome.NOT_TRIGGERED, PulseReasonCode.SETUP_NOT_YET_VALID, now)

    usable: list[PriceObservation] = []
    for observation in task_input.observations:
        problem = _incomparable(observation, task_input, trigger, now, policy)
        if problem is not None:
            # One incomparable row poisons the whole check rather than being
            # skipped: quietly ignoring it would hide a wiring fault behind a
            # patient-looking wait.
            return TriggerEvaluation(
                outcome=TriggerOutcome.OBSERVATION_INVALID,
                reason_code=problem,
                evaluated_at=now,
            )
        if _in_window(observation, trigger, now, policy):
            usable.append(observation)

    for observation in usable:
        if condition_met(trigger, observation.price):
            return TriggerEvaluation(
                outcome=TriggerOutcome.TRIGGERED,
                reason_code=PulseReasonCode.CONDITION_MET,
                evaluated_at=now,
                observed_price=observation.price,
                observed_at=observation.observed_at,
            )

    if task_input.window_truncated:
        # Nothing visible crossed, but rows existed that this read could not
        # return. Reporting "not yet" would be a claim about data nobody looked
        # at, so the shortfall is surfaced instead.
        return TriggerEvaluation(
            outcome=TriggerOutcome.OBSERVATION_BUDGET_EXCEEDED,
            reason_code=PulseReasonCode.OBSERVATION_BUDGET_EXCEEDED,
            evaluated_at=now,
        )
    if usable:
        newest = usable[-1]
        return TriggerEvaluation(
            outcome=TriggerOutcome.NOT_TRIGGERED,
            reason_code=PulseReasonCode.CONDITION_NOT_MET,
            evaluated_at=now,
            observed_price=newest.price,
            observed_at=newest.observed_at,
        )

    # The window held nothing. Whether that is a stalled feed or a market that
    # has never been priced changes the reason code, not the answer.
    latest = task_input.latest
    if latest is None:
        return _waiting(TriggerOutcome.OBSERVATION_STALE, PulseReasonCode.PRICE_UNAVAILABLE, now)
    if latest.observed_at < trigger.valid_from:
        # Everything recorded predates the setup. Ordinary in the moments after
        # one is published, and it resolves itself on the next observation.
        return _waiting(
            TriggerOutcome.OBSERVATION_PRECEDES_SETUP,
            PulseReasonCode.OBSERVATION_BEFORE_SETUP,
            now,
        )
    return _waiting(TriggerOutcome.OBSERVATION_STALE, PulseReasonCode.OBSERVATION_TOO_STALE, now)
