"""ATLAS on-chain intelligence specialist.

Safety-critical. The verdict is computed by a versioned deterministic policy from
collected facts; the model may only add commentary that is recorded alongside it.
ATLAS cannot approve trades, size positions, create risk bindings, call SENTINEL,
reach a wallet, signer or executor, or mutate TradeCase state.
"""

from src.agents.atlas.context import (
    AtlasContextPort,
    AtlasContextReader,
    AtlasContextUnavailable,
    AtlasSnapshotBuilder,
    AtlasTaskInput,
    atlas_snapshot_digest,
    snapshot_document,
    token_address_of,
)
from src.agents.atlas.handler import ATLAS_TASK_TYPE, AtlasWorkerHandler, domain_verdicts
from src.agents.atlas.models import (
    AtlasAssessment,
    AtlasDomain,
    AtlasOnchainSnapshot,
    AtlasReasonCode,
    AtlasSafetyDecision,
    AtlasSourceFailure,
    AtlasVerdict,
    ChainSnapshot,
    ContractFacts,
    HolderCompleteness,
    HolderFacts,
    HolderFactsSourceResult,
    HolderObservationBasis,
    HolderShare,
    HolderSourceRow,
    OriginFacts,
    OriginVerification,
    ProxyObservation,
)
from src.agents.atlas.policy import (
    ATLAS_POLICY_V1,
    ATLAS_POLICY_V2,
    AtlasPolicy,
    evaluate_snapshot,
)
from src.agents.atlas.prompt import ATLAS_PROMPT_HASH, ATLAS_PROMPT_VERSION
from src.agents.atlas.unavailable import UnconfiguredHolderSource, UnconfiguredOriginSource
from src.agents.atlas.validation import AtlasValidationError, validate_assessment

__all__ = [
    "ATLAS_POLICY_V1",
    "ATLAS_POLICY_V2",
    "ATLAS_PROMPT_HASH",
    "ATLAS_PROMPT_VERSION",
    "ATLAS_TASK_TYPE",
    "AtlasAssessment",
    "AtlasContextPort",
    "AtlasContextReader",
    "AtlasContextUnavailable",
    "AtlasDomain",
    "AtlasOnchainSnapshot",
    "AtlasPolicy",
    "AtlasReasonCode",
    "AtlasSafetyDecision",
    "AtlasSnapshotBuilder",
    "AtlasSourceFailure",
    "AtlasTaskInput",
    "AtlasValidationError",
    "AtlasVerdict",
    "AtlasWorkerHandler",
    "ChainSnapshot",
    "ContractFacts",
    "HolderCompleteness",
    "HolderFacts",
    "HolderFactsSourceResult",
    "HolderObservationBasis",
    "HolderShare",
    "HolderSourceRow",
    "OriginFacts",
    "OriginVerification",
    "ProxyObservation",
    "UnconfiguredHolderSource",
    "UnconfiguredOriginSource",
    "atlas_snapshot_digest",
    "domain_verdicts",
    "evaluate_snapshot",
    "snapshot_document",
    "token_address_of",
    "validate_assessment",
]
