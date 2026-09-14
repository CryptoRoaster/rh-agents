"""Versioned FUSE input and synthesis policy.

Two things are decided here and nowhere else: which evidence a synthesis is
built from, and which structured facts in that evidence constitute a blocker
rather than a remark.

Neither is a trading opinion. There is no threshold on sentiment, no minimum
attention, no preferred setup shape — those would be a second strategy layer
competing with VECTOR and a future FUSE consumer. What this bounds is narrower:
what counts as present, what counts as bad, and what counts as merely worth
saying.
"""

from dataclasses import dataclass

from src.core.models import AgentRole
from src.orchestration.workflow.models import EvidenceType

# The evidence a pre-trigger synthesis reads.
#
# This mirrors the workflow's own pre-trigger requirement set rather than
# restating it: a synthesis stage that read a different set than the stage it
# sits in would describe a case nobody is working on. TRIGGER and
# LIQUIDITY_EXECUTION are deliberately absent — they do not exist yet when this
# task runs, and reaching forward for them would either block on evidence that
# has not been produced or quietly synthesize a different case than the one the
# workflow is at.
PRE_TRIGGER_SOURCES: tuple[EvidenceType, ...] = (
    EvidenceType.DISCOVERY,
    EvidenceType.ONCHAIN,
    EvidenceType.SENTIMENT,
    EvidenceType.TRADE_SETUP,
)


@dataclass(frozen=True)
class FuseSynthesisPolicy:
    version: str
    # Which evidence types this stage reads. Ordered, because the synthesis is
    # walked in this order and a stable order makes the output reproducible.
    sources: tuple[EvidenceType, ...]
    # Whether a synthesis may be produced at all when a required safety-critical
    # source is unusable. It may — and it says INSUFFICIENT. Recording *why* a
    # case cannot proceed is more useful than recording nothing, and it is the
    # only way the gap becomes visible to anything reading evidence.
    synthesize_over_gaps: bool

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("A synthesis must read something")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("Each source type is read once")
        forward = {EvidenceType.TRIGGER, EvidenceType.LIQUIDITY_EXECUTION}
        if forward & set(self.sources):
            raise ValueError("A pre-trigger synthesis cannot read post-trigger evidence")


FUSE_SYNTHESIS_V1 = FuseSynthesisPolicy(
    version="fuse-synthesis-v1",
    sources=PRE_TRIGGER_SOURCES,
    synthesize_over_gaps=True,
)


# ------------------------------------------------------------- what is a fact
#
# The codes below are read from the sources' own structured fields. None is
# interpreted, scored or weighed; each is a lookup from a value a specialist
# already committed to.

# The three ATLAS integrity axes, named for a reader. `FAIL` on any of them is a
# measurement of something bad; `UNKNOWN` is the absence of a measurement. They
# become a blocker and a gap respectively, and never the same thing.
ONCHAIN_AXIS_LABELS: dict[str, str] = {
    "holder_integrity": "holder distribution",
    "dev_wallet_integrity": "developer wallet conduct",
    "contract_integrity": "contract code",
}

# SIGNAL qualities that mean the reading is worth less than it looks. Never a
# blocker: sentiment is required but not safety-critical, and a thin social
# picture is a reason to be careful rather than a reason the asset is dangerous.
DEGRADED_SENTIMENT_QUALITY: frozenset[str] = frozenset({"DEGRADED", "POOR", "UNKNOWN"})
CONCERNING_MANIPULATION: frozenset[str] = frozenset({"HIGH", "ELEVATED", "SEVERE"})
THIN_BREADTH: frozenset[str] = frozenset({"NARROW", "CONCENTRATED", "SINGLE_SOURCE"})

# Below this many recorded bars, a setup is worth flagging as thinly grounded.
# Not a validity rule — VECTOR already decided the setup was sound, and this
# does not second-guess it. It is the number below which a reader deserves to
# be told how little history the geometry rests on.
THIN_HISTORY_BARS = 30

# ORBIT classifications worth carrying either way.
POSITIVE_DISCOVERY: frozenset[str] = frozenset({"INTERESTING", "PROMISING", "STRONG"})
NEGATIVE_DISCOVERY: frozenset[str] = frozenset({"UNINTERESTING", "WEAK", "NOISE"})

# SIGNAL assessments worth carrying either way.
POSITIVE_SENTIMENT: frozenset[str] = frozenset({"POSITIVE"})
NEGATIVE_SENTIMENT: frozenset[str] = frozenset({"NEGATIVE"})

ROLE_FOR_TYPE: dict[EvidenceType, AgentRole] = {
    EvidenceType.DISCOVERY: AgentRole.ORBIT,
    EvidenceType.ONCHAIN: AgentRole.ATLAS,
    EvidenceType.SENTIMENT: AgentRole.SIGNAL,
    EvidenceType.TRADE_SETUP: AgentRole.VECTOR,
}
