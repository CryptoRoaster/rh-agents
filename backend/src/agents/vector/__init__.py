"""VECTOR trade setup specialist.

Proposes a precise, falsifiable setup: a side, a machine-evaluable trigger, the
level at which the idea is wrong, ordered objectives and an expiry. It cannot
approve a trade, size a position, choose a route or a slippage tolerance, create
a risk binding, reach a wallet, signer or executor, or move a TradeCase into an
executable state — and its output schema has no field in which any of that could
be expressed.

A proposal is not an authorization. PULSE watches the trigger, ANCHOR assesses
execution conditions and SENTINEL decides risk, in that order and independently.
"""

from src.agents.vector.context import (
    VectorContextReader,
    build_setup,
    reasoning_payload,
    setup_document,
    setup_fingerprint,
    vector_input_digest,
)
from src.agents.vector.handler import VECTOR_TASK_TYPE, VectorWorkerHandler
from src.agents.vector.models import (
    PRICE_BASIS,
    VECTOR_OUTPUT_SCHEMA_VERSION,
    EvidenceSummary,
    ObservedMeasurement,
    SetupKind,
    TriggerCondition,
    TriggerType,
    VectorMarketContext,
    VectorReasonCode,
    VectorSetup,
    VectorSetupProposal,
    VectorTaskInput,
)
from src.agents.vector.policy import REQUIRED_TRIGGER, VECTOR_SETUP_V1, VectorSetupPolicy
from src.agents.vector.ports import VectorContextPort, VectorContextUnavailable
from src.agents.vector.prompt import (
    VECTOR_INSTRUCTIONS,
    VECTOR_PROMPT_HASH,
    VECTOR_PROMPT_VERSION,
)
from src.agents.vector.validation import VectorValidationError, trigger_for, validate_proposal

__all__ = [
    "PRICE_BASIS",
    "REQUIRED_TRIGGER",
    "VECTOR_INSTRUCTIONS",
    "VECTOR_OUTPUT_SCHEMA_VERSION",
    "VECTOR_PROMPT_HASH",
    "VECTOR_PROMPT_VERSION",
    "VECTOR_SETUP_V1",
    "VECTOR_TASK_TYPE",
    "EvidenceSummary",
    "ObservedMeasurement",
    "SetupKind",
    "TriggerCondition",
    "TriggerType",
    "VectorContextPort",
    "VectorContextReader",
    "VectorContextUnavailable",
    "VectorMarketContext",
    "VectorReasonCode",
    "VectorSetup",
    "VectorSetupPolicy",
    "VectorSetupProposal",
    "VectorTaskInput",
    "VectorValidationError",
    "VectorWorkerHandler",
    "build_setup",
    "reasoning_payload",
    "setup_document",
    "setup_fingerprint",
    "trigger_for",
    "validate_proposal",
    "vector_input_digest",
]
