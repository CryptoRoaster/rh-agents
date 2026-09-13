"""One versioned location for TradeCase requirements and role capabilities."""

from dataclasses import dataclass

from src.core.models import AgentRole
from src.orchestration.workflow.models import EvidenceType


@dataclass(frozen=True)
class EvidenceRequirement:
    role: AgentRole
    evidence_type: EvidenceType
    task_type: str
    required: bool
    safety_critical: bool
    before_trigger: bool


@dataclass(frozen=True)
class TaskDefinition:
    role: AgentRole
    task_type: str
    required: bool
    completed_on_open: bool = False
    # How many times this slot may be claimed before the runtime stops. For a
    # one-shot specialist this is a retry budget and three is generous. For a
    # monitor it is a *watch* budget, because every unmet check is another claim,
    # and three would stop the watch a couple of minutes after it began.
    max_attempts: int | None = None


@dataclass(frozen=True)
class WorkflowPolicy:
    version: str
    requirements: tuple[EvidenceRequirement, ...]
    tasks: tuple[TaskDefinition, ...]

    def requirement(self, evidence_type: EvidenceType) -> EvidenceRequirement:
        return next(item for item in self.requirements if item.evidence_type == evidence_type)

    @property
    def safety_types(self) -> frozenset[EvidenceType]:
        return frozenset(item.evidence_type for item in self.requirements if item.safety_critical)


TRADE_CASE_V1 = WorkflowPolicy(
    version="trade-case-v1",
    requirements=(
        EvidenceRequirement(
            AgentRole.ORBIT, EvidenceType.DISCOVERY, "VERIFY_DISCOVERY", True, False, True
        ),
        EvidenceRequirement(
            AgentRole.ATLAS, EvidenceType.ONCHAIN, "ASSESS_ONCHAIN_INTEGRITY", True, True, True
        ),
        EvidenceRequirement(
            AgentRole.SIGNAL, EvidenceType.SENTIMENT, "ASSESS_SENTIMENT", True, False, True
        ),
        EvidenceRequirement(
            AgentRole.VECTOR, EvidenceType.TRADE_SETUP, "DEFINE_TRADE_SETUP", True, True, True
        ),
        EvidenceRequirement(
            AgentRole.PULSE, EvidenceType.TRIGGER, "WAIT_FOR_TRIGGER", True, True, False
        ),
        EvidenceRequirement(
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            "ASSESS_EXECUTION",
            True,
            True,
            False,
        ),
    ),
    tasks=(
        # A discovery worker verifies the candidate the case was opened from, so
        # this is real claimable work rather than a formality. The provenance
        # envelope recorded at open time is superseded by that assessment.
        TaskDefinition(AgentRole.ORBIT, "VERIFY_DISCOVERY", True),
        TaskDefinition(AgentRole.COMMANDER, "OPEN_TRADE_CASE", True, True),
        TaskDefinition(AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY", True),
        TaskDefinition(AgentRole.SIGNAL, "ASSESS_SENTIMENT", True),
        TaskDefinition(AgentRole.VECTOR, "DEFINE_TRADE_SETUP", True),
        TaskDefinition(AgentRole.FUSE, "SYNTHESIZE_EVIDENCE", False),
        # A trigger monitor re-claims its slot on every check. At the PULSE
        # policy's ninety-second cadence, 300 claims cover seven and a half hours
        # of watching — comfortably beyond the four hours a VECTOR setup may
        # live, so in practice the setup's own expiry ends the watch and this is
        # the outer bound rather than the operative one. It is deliberately not
        # unlimited: a system that would watch forever has no way to say it has
        # stopped.
        TaskDefinition(AgentRole.PULSE, "WAIT_FOR_TRIGGER", True, max_attempts=300),
        TaskDefinition(AgentRole.ANCHOR, "ASSESS_EXECUTION", True),
    ),
)
