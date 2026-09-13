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


def evaluate(
    task_input: PulseTaskInput,
    now: datetime,
    policy: PulseTriggerPolicy = PULSE_TRIGGER_V1,
) -> TriggerEvaluation:
    """Decide whether the authoritative condition has become true."""
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

    observation = task_input.observation
    if observation is None:
        return _waiting(TriggerOutcome.OBSERVATION_STALE, PulseReasonCode.PRICE_UNAVAILABLE, now)

    # Identity before value. A price from another pool is not a price that has
    # not yet crossed; it is not this market's price at all.
    if observation.pair_id != task_input.market_pair_id:
        return TriggerEvaluation(
            outcome=TriggerOutcome.OBSERVATION_INVALID,
            reason_code=PulseReasonCode.MARKET_IDENTITY_MISMATCH,
            evaluated_at=now,
        )
    if observation.price_basis != trigger.price_basis or observation.price_basis != PRICE_BASIS:
        # Comparing a threshold in one unit to a price in another would produce a
        # numerically valid answer to a question nobody asked. There is no
        # conversion here and no reciprocal inference: the check simply refuses.
        return TriggerEvaluation(
            outcome=TriggerOutcome.OBSERVATION_INVALID,
            reason_code=PulseReasonCode.PRICE_BASIS_MISMATCH,
            evaluated_at=now,
        )

    # Time before value, all of it by source observation time. Fetching a price
    # now does not make it a statement about now.
    if observation.observed_at > now + policy.max_clock_skew:
        return TriggerEvaluation(
            outcome=TriggerOutcome.OBSERVATION_INVALID,
            reason_code=PulseReasonCode.OBSERVATION_IN_FUTURE,
            evaluated_at=now,
        )
    if observation.observed_at < trigger.valid_from:
        # Market data from before the setup existed cannot trigger it: a setup is
        # a statement about what happens next, not a test applied to the past.
        # This is a wait rather than a fault, because it is the ordinary state of
        # affairs in the moments after a setup is published — the newest recorded
        # snapshot is simply older than the proposal, and the next one will not
        # be.
        return _waiting(
            TriggerOutcome.OBSERVATION_PRECEDES_SETUP,
            PulseReasonCode.OBSERVATION_BEFORE_SETUP,
            now,
        )
    if observation.observed_at >= trigger.expires_at:
        return TriggerEvaluation(
            outcome=TriggerOutcome.OBSERVATION_INVALID,
            reason_code=PulseReasonCode.OBSERVATION_AFTER_SETUP,
            evaluated_at=now,
        )
    if now - observation.observed_at > policy.max_observation_age:
        # Old enough that it says nothing about the current market. Waiting is
        # correct: the feed may catch up.
        return _waiting(
            TriggerOutcome.OBSERVATION_STALE, PulseReasonCode.OBSERVATION_TOO_STALE, now
        )

    if not condition_met(trigger, observation.price):
        return TriggerEvaluation(
            outcome=TriggerOutcome.NOT_TRIGGERED,
            reason_code=PulseReasonCode.CONDITION_NOT_MET,
            evaluated_at=now,
            observed_price=observation.price,
            observed_at=observation.observed_at,
        )
    return TriggerEvaluation(
        outcome=TriggerOutcome.TRIGGERED,
        reason_code=PulseReasonCode.CONDITION_MET,
        evaluated_at=now,
        observed_price=observation.price,
        observed_at=observation.observed_at,
    )
