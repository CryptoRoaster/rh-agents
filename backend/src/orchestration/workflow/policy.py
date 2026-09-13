"""One versioned location for TradeCase requirements and role capabilities."""

from dataclasses import dataclass
from datetime import timedelta

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
class WaitPolicy:
    """When and how often a monitoring task may be rechecked.

    Server-owned, deliberately. A worker reports what it found; the schedule is
    not its to choose, because a worker that could name its own cadence could
    postpone a task indefinitely or poll a provider as fast as it liked.

    The horizon is expressed as a duration rather than a count so it can be
    compared against the thing being watched. The number of permitted checks
    follows from it, rather than being a number somebody picked.
    """

    interval: timedelta
    horizon: timedelta
    # Only these reasons may be reported. An unknown one is refused rather than
    # recorded, so a worker cannot invent a category to wait under.
    reasons: frozenset[str]
    # Reasons that end the watch instead of rescheduling it. The mapping is
    # policy's, not the worker's: the worker reports a fact and the runtime
    # decides what that fact means for the task.
    terminal_reasons: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("A recheck interval must be positive")
        if self.horizon < self.interval:
            raise ValueError("A watch horizon must cover at least one check")
        if not self.reasons:
            raise ValueError("A wait policy must allow at least one reason")
        if not self.terminal_reasons <= self.reasons:
            raise ValueError("A terminal reason must also be an allowed reason")

    @property
    def max_waits(self) -> int:
        """How many ordinary rechecks the horizon affords, rounded up."""
        return -(-int(self.horizon.total_seconds()) // int(self.interval.total_seconds()))


@dataclass(frozen=True)
class TaskDefinition:
    role: AgentRole
    task_type: str
    required: bool
    completed_on_open: bool = False
    # The failure budget. Unchanged for every role: three attempts that went
    # wrong. Waiting does not spend it, so a monitor that rechecks two hundred
    # times still has its full allowance for things actually going wrong.
    max_attempts: int | None = None
    # Present only for tasks that monitor rather than compute. Absent means the
    # task may not wait at all.
    wait: WaitPolicy | None = None

    @property
    def claim_ceiling(self) -> int | None:
        """The outer bound on claims, covering both waits and failures.

        A belt-and-braces stop: the precise budgets are enforced when an outcome
        is reported, and each sets the task terminal on its own exhaustion. This
        exists so a task can never be claimed unboundedly if one of those paths
        is ever missed.
        """
        if self.wait is None:
            return self.max_attempts
        return self.wait.max_waits + (self.max_attempts or 0) + 1


@dataclass(frozen=True)
class WorkflowPolicy:
    version: str
    requirements: tuple[EvidenceRequirement, ...]
    tasks: tuple[TaskDefinition, ...]

    def task(self, role: AgentRole, task_type: str) -> TaskDefinition | None:
        """The server-side definition governing one task slot."""
        for definition in self.tasks:
            if definition.role == role and definition.task_type == task_type:
                return definition
        return None

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
        # FUSE synthesis: optional, not safety-critical, before the trigger.
        #
        # Both flags are load-bearing and neither is timidity.
        #
        # Not safety-critical, because `safety_types` is exactly what
        # `risk_input_digest` hashes. A safety-critical synthesis would put its
        # own fingerprint into the risk snapshot, and that fingerprint covers
        # SENTIMENT facts — so a change in social data would silently invalidate
        # a risk authorization through the back door. Phase 2F decided
        # deliberately that SENTIMENT gates the workflow without binding risk,
        # and a summariser must not be able to overturn that decision by
        # summarising.
        #
        # Not required, because a synthesis is a reading of the evidence rather
        # than a fact the case needs. Requiring it would let a synthesizer
        # outage block cases whose canonical evidence is complete, which would
        # make the commentary layer load-bearing.
        #
        # The consequence is that FUSE evidence is recorded, superseded and
        # audited like everything else, and gates nothing. That is the correct
        # weight for commentary.
        EvidenceRequirement(
            AgentRole.FUSE, EvidenceType.SYNTHESIS, "SYNTHESIZE_EVIDENCE", False, False, True
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
        # A trigger monitor re-claims its slot on every check, so it needs a
        # watch horizon rather than a larger retry budget. Five hours at a
        # ninety-second cadence covers the four a VECTOR setup may live with room
        # for scheduling drift; in practice the setup's own expiry ends the watch
        # first, and this is the outer bound. A test holds the horizon against
        # VECTOR's maximum lifetime so the two cannot drift apart silently.
        TaskDefinition(
            AgentRole.PULSE,
            "WAIT_FOR_TRIGGER",
            True,
            wait=WaitPolicy(
                interval=timedelta(seconds=90),
                horizon=timedelta(hours=5),
                reasons=frozenset(
                    {
                        "CONDITION_NOT_MET",
                        "SETUP_NOT_YET_VALID",
                        "NO_CURRENT_SETUP",
                        "OBSERVATION_TOO_STALE",
                        "OBSERVATION_BEFORE_SETUP",
                        "PRICE_UNAVAILABLE",
                        "SETUP_EXPIRED",
                    }
                ),
                # The window closed. Rechecking a setup that can never become
                # valid again is work queued to fail.
                terminal_reasons=frozenset({"SETUP_EXPIRED"}),
            ),
        ),
        TaskDefinition(AgentRole.ANCHOR, "ASSESS_EXECUTION", True),
    ),
)
