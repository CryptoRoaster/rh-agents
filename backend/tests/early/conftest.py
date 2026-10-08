"""Fixtures for PRE_VECTOR_EARLY_ENTRY_V1.

One market throughout — the same one the risk-data and risk-request suites use —
so an early case is carried through the real workflow, the real EARLY producer,
PULSE, ANCHOR, SENTINEL and the fill without a second notion of which token
this is. Every fact is synthetic; nothing keys on a real market.
"""

from datetime import timedelta
from decimal import Decimal

from src.agents.early.context import EarlyContextReader
from src.agents.early.handler import EarlyWorkerHandler
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.fake import fixture_history
from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1
from src.orchestration.worker.capabilities import EarlyCapabilities
from src.orchestration.worker.models import TaskLease
from src.orchestration.workflow.models import (
    EvidenceType,
    FundingGraphSummary,
    OnchainPayload,
    PrelaunchFundingSummary,
)
from src.orchestration.workflow.policy import TRADE_CASE_EARLY_V1
from tests.riskdata.conftest import IDENTITY, onchain_payload, record, record_onchain
from tests.riskrequest.conftest import fresh_onchain, risk_db  # noqa: F401
from tests.vector.conftest import StubHistory

CREATION_SOURCE = "evm-rpc:eth_getBlockByNumber"


def early_onchain(now, *, created_ago: timedelta | None = timedelta(hours=2), fresh=True):
    """ATLAS evidence carrying the chain-side creation timestamp, or none at all."""
    base: OnchainPayload = fresh_onchain(now) if fresh else onchain_payload(now)
    if created_ago is None:
        return base
    intelligence = base.intelligence
    assert intelligence is not None
    funding = FundingGraphSummary(
        status="UNAVAILABLE",
        gap="FUNDING_SOURCE_UNAVAILABLE",
        source="test-funding",
        holder_basis="UNKNOWN",
        prelaunch=PrelaunchFundingSummary(
            status="UNAVAILABLE",
            gap="PRELAUNCH_SOURCE_UNAVAILABLE",
            source="test-funding",
            creation_block=900_000,
            creation_timestamp=now - created_ago,
            creation_time_source=CREATION_SOURCE,
            holder_basis="UNKNOWN",
        ),
    )
    return base.model_copy(
        update={"intelligence": intelligence.model_copy(update={"funding_graph": funding})}
    )


async def open_early_case(cases, sessions, now, trace, *, key="early-case", identity=None):
    """A PRE_VECTOR_EARLY_ENTRY_V1 case, opened the way the early intake opens one."""
    from uuid import uuid4

    async with sessions.begin() as session:
        trade_case, _ = await cases.open_trade_case_in_session(
            session,
            identity or IDENTITY,
            originating_discovery_reference=uuid4(),
            correlation_id=trace,
            idempotency_key=key,
            expires_at=now + timedelta(hours=1),
            strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1,
            workflow=TRADE_CASE_EARLY_V1,
        )
    return trade_case


def young_history(now, bars: int = 3, **overrides):
    """A closed hourly series of `bars` bars, as VECTOR requests it (48)."""
    newest = overrides.pop("newest_close", now.replace(minute=0, second=0, microsecond=0))
    return fixture_history(IDENTITY, newest_close=newest, bars=bars, requested_bars=48, **overrides)


def early_reader(cases, markets, now, history):
    return EarlyContextReader(
        cases=cases,
        markets=markets,
        history=history if hasattr(history, "history") else StubHistory(history),
        clock=FixedClock(now),
    )


def early_lease(trade_case, now):
    from uuid import uuid4

    return TaskLease(
        lease_id=uuid4(),
        task_id=uuid4(),
        trade_case_id=trade_case.id,
        role=AgentRole.EARLY,
        task_type="DEFINE_EARLY_SETUP",
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trade_case.correlation_id,
        worker_instance_id=uuid4(),
    )


async def early_setup(cases, trade_case, markets, now, history):
    """Run the real EARLY handler and record its setup through the real workflow."""
    handler = EarlyWorkerHandler()
    reader = early_reader(cases, markets, now, history)
    lease = early_lease(trade_case, now)
    capabilities = EarlyCapabilities(lease=lease, context=reader, submit=None)  # type: ignore[arg-type]
    result = await handler.handle(lease, capabilities)
    if result.kind != "evidence":
        return result, None
    envelope = await cases.record_evidence(trade_case.id, result.submission)
    return result, envelope


async def record_early_onchain(cases, trade_case, now, **kw):
    return await record_onchain(
        cases, trade_case, now, early_onchain(now, **kw), key=f"early-atlas-{trade_case.id}"
    )


async def record_trigger(cases, trade_case, now, setup):
    from tests.worker.conftest import trigger_payload

    return await record(
        cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup.evidence_id),
        key=f"early-trigger-{trade_case.id}",
    )


SPOT = Decimal("1.25")
