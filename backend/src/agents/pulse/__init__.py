"""PULSE deterministic trigger monitor.

Watches for exactly one thing: whether the currently authoritative VECTOR trigger
condition has become true before its setup expires. It compares Decimals. There
is no model, no prompt, no reasoning provider and no paid call anywhere in this
package, because the question has one correct answer and a probabilistic one
would make it impossible to say afterwards why the system acted.

It cannot decide whether a trade is good, size a position, choose a route or a
slippage tolerance, judge liquidity, create a risk binding, reach a wallet,
signer or executor, or move a TradeCase into an executable state. It produces
TRIGGER_EVIDENCE and nothing else, and only when the condition actually held.

"Not yet" is the normal answer, often for hours. It is a durable reschedule, not
a failure, and it writes no evidence at all.
"""

from src.agents.pulse.context import PulseContextReader, price_observation, watched_trigger
from src.agents.pulse.evaluator import condition_met, evaluate
from src.agents.pulse.handler import PULSE_TASK_TYPE, PulseWorkerHandler, trigger_digest
from src.agents.pulse.models import (
    PULSE_OUTPUT_SCHEMA_VERSION,
    WAITING_OUTCOMES,
    PriceObservation,
    PulseReasonCode,
    PulseTaskInput,
    TriggerEvaluation,
    TriggerOutcome,
    WatchedTrigger,
)
from src.agents.pulse.policy import PULSE_TRIGGER_V1, PulseTriggerPolicy
from src.agents.pulse.ports import PulseContextPort, PulseContextUnavailable

__all__ = [
    "PULSE_OUTPUT_SCHEMA_VERSION",
    "PULSE_TASK_TYPE",
    "PULSE_TRIGGER_V1",
    "WAITING_OUTCOMES",
    "PriceObservation",
    "PulseContextPort",
    "PulseContextReader",
    "PulseContextUnavailable",
    "PulseReasonCode",
    "PulseTaskInput",
    "PulseTriggerPolicy",
    "PulseWorkerHandler",
    "TriggerEvaluation",
    "TriggerOutcome",
    "WatchedTrigger",
    "condition_met",
    "evaluate",
    "price_observation",
    "trigger_digest",
    "watched_trigger",
]
