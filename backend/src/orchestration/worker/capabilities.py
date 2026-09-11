"""Role-scoped capability composition handed to future specialist workers.

A worker receives only the ports its role is allowed to use. There is deliberately
no object on which every method exists, so a worker cannot reach a capability it
was not composed with, and no prompt instruction is load-bearing for safety.

Absent by construction, for every role: database sessions, connections or
credentials, raw RPC clients, raw HTTP clients, provider SDKs, signer, wallet,
private keys, executor, ledger writes, SENTINEL mutation, TradeCase status
setters and force transitions. The process may have network access; the worker
abstraction never hands it over.

These objects are convenience, not security. Every authoritative effect is
re-verified server-side in ``WorkerRuntimeService``.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.core.models import AgentRole
from src.markets.models import MarketCandidate, MarketSnapshot
from src.orchestration.worker.models import TaskLease
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    SpecialistTask,
    TradeCase,
    TradeSetupPayload,
)


class MarketDiscoveryPort(Protocol):
    """Recorded market observations only, never a provider client.

    Trusted infrastructure implements this; it is not handed to a worker directly.
    A discovery worker receives the narrower context port below, assembled from
    this one, so it never chooses what to read.
    """

    async def candidates(
        self, *, include_fixtures: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[MarketCandidate, ...]: ...

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


class DiscoveryContextPort(Protocol):
    """ORBIT input: one purpose-built view of the candidate under assessment.

    There is no method to browse other markets, read another role's evidence, see
    a risk outcome or read TradeCase status.
    """

    async def candidate_context(self, trade_case_id: UUID, task_id: UUID) -> object: ...


class OnchainContextPort(Protocol):
    """ATLAS input: one assembled view of the deterministic on-chain snapshot.

    The worker never holds an RPC client, an indexer client or a session. A
    trusted collector performs the reads and hands over the finished facts, so
    ATLAS cannot choose what to query or how to interpret a failed call.
    """

    async def onchain_context(self, trade_case_id: UUID, task_id: UUID) -> object: ...


class SentimentPort(Protocol):
    """SIGNAL input: one assembled view of the normalized social observation set.

    The worker never holds a social API client, a session or a URL. Trusted
    infrastructure decides which source to ask and how to authenticate, applies
    the observation window and hands over finished normalized observations, so
    SIGNAL cannot choose what to fetch or widen the interval it reports on.
    """

    async def sentiment_context(self, trade_case_id: UUID, task_id: UUID) -> object: ...


class MarketHistoryPort(Protocol):
    """VECTOR input. Unimplemented in Phase 2B."""

    async def history(self, market_key: str) -> object: ...


class TriggerFeedPort(Protocol):
    """PULSE input: the exact current setup plus a permitted realtime feed."""

    async def current_setup(self, trade_case_id: UUID) -> TradeSetupPayload | None: ...


class ExecutionAssessmentPort(Protocol):
    """ANCHOR input: liquidity and routing reads only. Never signs, never sends."""

    async def quote(self, market_key: str) -> object: ...


class ValidatedEvidencePort(Protocol):
    """FUSE input: current, fresh, role-bound evidence only.

    There is no method to change an evidence status, clear a blocker or turn an
    UNKNOWN into an AVAILABLE.
    """

    async def evidence_for_fuse(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class WorkflowStatePort(Protocol):
    """COMMANDER input plus deterministic orchestration requests.

    ``evaluate`` asks the deterministic evaluator to recompute state. It cannot
    choose the resulting state, and there is no status setter or force transition.
    """

    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def tasks(self, trade_case_id: UUID) -> tuple[SpecialistTask, ...]: ...

    async def create_required_tasks(self, trade_case_id: UUID) -> tuple[SpecialistTask, ...]: ...

    async def evaluate_trade_case(self, trade_case_id: UUID) -> TradeCase: ...


class EvidenceSubmissionPort(Protocol):
    """The only authoritative write a specialist worker can reach.

    The lease is bound into the capability, so a worker never chooses which task or
    attempt it is answering for, and it submits typed evidence rather than an
    instruction.
    """

    @property
    def lease(self) -> TaskLease: ...

    async def submit_evidence(self, submission: object, *, result_key: str) -> object: ...


@dataclass(frozen=True)
class OrbitCapabilities:
    lease: TaskLease
    context: DiscoveryContextPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class AtlasCapabilities:
    lease: TaskLease
    context: OnchainContextPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class SignalCapabilities:
    lease: TaskLease
    context: SentimentPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class VectorCapabilities:
    lease: TaskLease
    history: MarketHistoryPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class PulseCapabilities:
    lease: TaskLease
    triggers: TriggerFeedPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class AnchorCapabilities:
    lease: TaskLease
    execution: ExecutionAssessmentPort
    submit: EvidenceSubmissionPort


@dataclass(frozen=True)
class FuseCapabilities:
    """Read-only. FUSE has no submission port in Phase 2B: it may summarize valid
    evidence, never restate it as new specialist evidence."""

    lease: TaskLease
    evidence: ValidatedEvidencePort


@dataclass(frozen=True)
class CommanderCapabilities:
    """Coordination only. No specialist submission port, so COMMANDER cannot file
    evidence on another role's behalf."""

    lease: TaskLease
    workflow: WorkflowStatePort


CAPABILITY_TYPES: dict[AgentRole, type] = {
    AgentRole.ORBIT: OrbitCapabilities,
    AgentRole.ATLAS: AtlasCapabilities,
    AgentRole.SIGNAL: SignalCapabilities,
    AgentRole.VECTOR: VectorCapabilities,
    AgentRole.PULSE: PulseCapabilities,
    AgentRole.ANCHOR: AnchorCapabilities,
    AgentRole.FUSE: FuseCapabilities,
    AgentRole.COMMANDER: CommanderCapabilities,
}
