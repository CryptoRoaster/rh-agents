"""SIGNAL social attention specialist.

Probabilistic by nature, adversarial by input. The deterministic layer measures
who spoke, how often and how much of it was the same text; the model reads what
the language means. SIGNAL cannot approve trades, size positions, define a setup,
create a risk binding, call SENTINEL, reach a wallet, signer or executor, or
mutate TradeCase state — and its output schema has no field in which any of that
could be expressed.

Sentiment is not demand, attention is not agreement, and zero observations is not
neutral. Each is kept on its own axis so nothing downstream can collapse them.
"""

from src.agents.signal.context import (
    SignalContextReader,
    reasoning_payload,
    sample,
    signal_document,
    signal_input_digest,
    token_address_of,
)
from src.agents.signal.handler import SIGNAL_TASK_TYPE, SignalWorkerHandler
from src.agents.signal.models import (
    SIGNAL_OUTPUT_SCHEMA_VERSION,
    STRONG_BINDING_BASES,
    DuplicateCluster,
    MarketBindingBasis,
    ObservationEngagement,
    ObservationKind,
    QualitativeLevel,
    SentimentDirection,
    SentimentStrength,
    SignalAssessment,
    SignalDataQuality,
    SignalGap,
    SignalNarrative,
    SignalObservation,
    SignalQualityFeatures,
    SignalRepresentative,
    SignalSource,
    SignalStructuralAssessment,
    SignalTaskInput,
    SignalWindow,
    SocialDemandIndication,
    SourceBreakdown,
)
from src.agents.signal.policy import (
    SIGNAL_QUALITY_V1,
    SignalQualityPolicy,
    assess_structure,
    exceeds_ceiling,
)
from src.agents.signal.ports import (
    SignalContextPort,
    SignalObservationReadPort,
    SignalSourceUnavailable,
)
from src.agents.signal.prompt import (
    SIGNAL_INSTRUCTIONS,
    SIGNAL_PROMPT_HASH,
    SIGNAL_PROMPT_VERSION,
)
from src.agents.signal.quality import (
    CONTENT_HASH_ALGORITHM,
    Admission,
    admit,
    compute_features,
    content_hash,
    normalize_content,
)
from src.agents.signal.validation import SignalValidationError, validate_assessment

__all__ = [
    "CONTENT_HASH_ALGORITHM",
    "SIGNAL_INSTRUCTIONS",
    "SIGNAL_OUTPUT_SCHEMA_VERSION",
    "SIGNAL_PROMPT_HASH",
    "SIGNAL_PROMPT_VERSION",
    "SIGNAL_QUALITY_V1",
    "SIGNAL_TASK_TYPE",
    "STRONG_BINDING_BASES",
    "Admission",
    "DuplicateCluster",
    "MarketBindingBasis",
    "ObservationEngagement",
    "ObservationKind",
    "QualitativeLevel",
    "SentimentDirection",
    "SentimentStrength",
    "SignalAssessment",
    "SignalContextPort",
    "SignalContextReader",
    "SignalDataQuality",
    "SignalGap",
    "SignalNarrative",
    "SignalObservation",
    "SignalObservationReadPort",
    "SignalQualityFeatures",
    "SignalQualityPolicy",
    "SignalRepresentative",
    "SignalSource",
    "SignalSourceUnavailable",
    "SignalStructuralAssessment",
    "SignalTaskInput",
    "SignalValidationError",
    "SignalWindow",
    "SignalWorkerHandler",
    "SocialDemandIndication",
    "SourceBreakdown",
    "admit",
    "assess_structure",
    "compute_features",
    "content_hash",
    "exceeds_ceiling",
    "normalize_content",
    "reasoning_payload",
    "sample",
    "signal_document",
    "signal_input_digest",
    "token_address_of",
    "validate_assessment",
]
