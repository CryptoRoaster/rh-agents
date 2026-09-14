"""Fixtures for the control-plane tests.

Most questions here are about what COMMANDER declines to do, so the fixtures
build whole cases at particular workflow stages rather than isolated objects.
"""

from uuid import uuid4

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.orchestration.commander.context import CommanderContextReader
from src.orchestration.commander.intake import CommanderIntakeService
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.service import TradeCaseService
from tests.fuse.conftest import onchain, onchain_envelope_kwargs, sentiment, trade_setup
from tests.worker.conftest import submission
from tests.worker.conftest import worker_db as worker_db  # noqa: F401


def build_stack(sessions, instant, *, kill_switch=False):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = CommanderContextReader(
        cases=cases, sessions=sessions, clock=clock, kill_switch=kill_switch
    )
    return runtime, reader


async def open_case(cases, now, trace, key):
    from tests.worker.conftest import open_case as _open

    return await _open(cases, now, trace, key)


async def record(cases, trade_case, now, role, evidence_type, payload, *, key, **kw):
    """Record one envelope, honouring the workflow's own rules for its type."""
    if evidence_type is EvidenceType.ONCHAIN:
        kw = {**onchain_envelope_kwargs(payload), **kw}
    return await cases.record_evidence(
        trade_case.id,
        submission(trade_case, now, role, evidence_type, payload, key=key, **kw),
    )


async def pre_trigger_evidence(cases, trade_case, now, **overrides):
    """ATLAS, SIGNAL and VECTOR; ORBIT is recorded when the case opens."""
    await record(
        cases,
        trade_case,
        now,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        overrides.get("onchain", onchain()),
        key=f"cmd-atlas-{trade_case.id}",
    )
    await record(
        cases,
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        overrides.get("sentiment", sentiment()),
        key=f"cmd-signal-{trade_case.id}",
    )
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        overrides.get("trade_setup", trade_setup(now)),
        key=f"cmd-setup-{trade_case.id}",
    )


async def triggered(cases, trade_case, now, setup_evidence):
    from tests.worker.conftest import trigger_payload

    return await record(
        cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup_evidence.evidence_id),
        key=f"cmd-trigger-{trade_case.id}",
    )


async def context_for(runtime, reader, trade_case):
    return await reader.commander_context(trade_case.id, uuid4())


class StubMarkets:
    """Recorded candidates and snapshots. Never a provider."""

    def __init__(self, candidates=(), snapshots=None) -> None:
        self._candidates = tuple(candidates)
        self._snapshots = snapshots or {}

    async def candidates(self, *, include_fixtures=False, limit=50, offset=0):
        return tuple(item for item in self._candidates if include_fixtures or not item.is_fixture)[
            offset : offset + limit
        ]

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        return self._snapshots.get(identity)


def intake_service(sessions, instant, *, candidates=(), snapshots=None, **overrides):
    clock = FixedClock(instant)
    return CommanderIntakeService(
        cases=TradeCaseService(sessions, clock=clock),
        markets=StubMarkets(candidates, snapshots),
        sessions=sessions,
        clock=clock,
        **overrides,
    )
