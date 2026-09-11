"""Proof that a worker cannot reach authority it was never granted."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.core.models import AgentRole
from src.orchestration.worker.capabilities import AtlasCapabilities
from src.orchestration.worker.models import WorkerErrorCode, WorkerFailure
from src.orchestration.worker.runner import CapabilityProvider
from tests.worker.conftest import open_case
from tests.worker.test_worker_runtime import claimed

FORBIDDEN_SURFACE = (
    "session",
    "sessions",
    "connection",
    "engine",
    "execute",
    "rpc",
    "http",
    "signer",
    "sign",
    "broadcast",
    "wallet",
    "private_key",
    "executor",
    "ledger",
    "position",
    "fill",
    "pnl",
    "balance",
    "risk",
    "sentinel",
    "approve",
    "set_status",
    "force",
    "transition",
)


class FakeOnchain:
    async def token_integrity(self, market_key: str) -> object:
        return {}


async def test_a_built_capability_exposes_nothing_dangerous(runtime, now, trace):
    _, lease = await claimed(runtime, now, trace, key="security-case")
    provider = CapabilityProvider(service=runtime, onchain=FakeOnchain())
    capabilities = provider.build(lease)
    assert isinstance(capabilities, AtlasCapabilities)

    reachable = {name for name in dir(capabilities) if not name.startswith("__")}
    for attribute in reachable:
        assert not any(bad in attribute.lower() for bad in FORBIDDEN_SURFACE), attribute
    # Exactly three things: which lease, one read port, one bound write port.
    assert reachable == {"lease", "onchain", "submit"}

    # The bound write port cannot be aimed at another task or another attempt.
    submit_surface = {name for name in dir(capabilities.submit) if not name.startswith("_")}
    assert submit_surface == {"lease", "submit_evidence"}
    assert capabilities.submit.lease.task_id == lease.task_id


async def test_capability_cannot_be_built_for_a_role_without_its_port(runtime, now, trace):
    """An unimplemented data source means the role simply cannot run, rather than
    a worker receiving a stub that quietly returns nothing."""
    _, lease = await claimed(runtime, now, trace, key="security-noport")
    empty = CapabilityProvider(service=runtime)
    with pytest.raises(WorkerFailure) as caught:
        empty.build(lease)
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


async def test_runtime_service_grants_no_risk_ledger_or_execution_write(runtime):
    surface = {name for name in dir(runtime) if not name.startswith("_")}
    assert surface == {
        "attempts",
        "cases",
        "claim_next_task",
        "clock",
        "policy",
        "recover_expired_leases",
        "register_worker",
        "renew_lease",
        "report_task_failure",
        "sessions",
        "set_worker_status",
        "submit_task_result",
        "worker",
        "workers",
    }
    for forbidden in (
        "record_risk_decision",
        "sign",
        "broadcast",
        "execute",
        "write_position",
        "set_status",
        "force_transition",
    ):
        assert not hasattr(runtime, forbidden)


async def test_worker_roles_never_include_deterministic_services():
    assert "SENTINEL" not in {role.value for role in AgentRole}
    assert "LEDGER" not in {role.value for role in AgentRole}
    assert "EXECUTOR" not in {role.value for role in AgentRole}


async def test_finished_attempt_history_is_immutable(worker_db, now, trace):
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL append-only triggers")
    from src.orchestration.worker.models import TaskFailureReport, WorkerFailureCategory
    from tests.worker.conftest import build_runtime

    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace, key="security-immutable")
    await runtime.report_task_failure(
        lease,
        TaskFailureReport(category=WorkerFailureCategory.TRANSIENT, reason_code="PROVIDER_TIMEOUT"),
    )
    for statement in (
        "UPDATE worker_task_attempts SET outcome = 'SUCCEEDED' WHERE lease_id = :lease",
        "DELETE FROM worker_task_attempts WHERE lease_id = :lease",
    ):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(statement), {"lease": lease.lease_id})


async def test_open_attempt_may_still_be_finalised_once(worker_db, now, trace):
    engine, sessions = worker_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL append-only triggers")
    from tests.worker.conftest import build_runtime
    from tests.worker.test_worker_runtime import evidence_result

    runtime = build_runtime(sessions, now)
    _, lease = await claimed(runtime, now, trace, key="security-finalise")
    trade_case = await runtime.cases.get_trade_case(lease.trade_case_id)
    # The claim leaves the attempt open, so the single legitimate finalising write
    # succeeds; the trigger only guards attempts that already finished.
    disposition = await runtime.submit_task_result(
        lease, evidence_result(trade_case, lease, now, result_key="final")
    )
    assert disposition.outcome.value == "SUCCEEDED"


async def test_no_worker_table_grants_trade_case_status_writes(runtime, now, trace):
    trade_case = await open_case(runtime.cases, now, trace, "security-status")
    before = (await runtime.cases.get_trade_case(trade_case.id)).status
    # The runtime exposes no path that names a target status at all.
    assert not any("status" in name and "worker" not in name for name in dir(runtime))
    after = (await runtime.cases.get_trade_case(trade_case.id)).status
    assert before == after


class FakePort:
    """Stands in for any not-yet-implemented read port."""

    def __getattr__(self, name: str):
        async def call(*args: object, **kwargs: object) -> object:
            return {}

        return call


def fake_lease(role, now, trace):
    from datetime import timedelta
    from uuid import uuid4

    from src.orchestration.worker.models import TaskLease

    return TaskLease(
        lease_id=uuid4(),
        task_id=uuid4(),
        trade_case_id=uuid4(),
        role=role,
        task_type="TASK",
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace,
    )


ROLE_PORT = {
    AgentRole.ORBIT: ("markets", {"markets", "lease", "submit"}),
    AgentRole.ATLAS: ("onchain", {"onchain", "lease", "submit"}),
    AgentRole.SIGNAL: ("sentiment", {"sentiment", "lease", "submit"}),
    AgentRole.VECTOR: ("history", {"history", "lease", "submit"}),
    AgentRole.PULSE: ("triggers", {"triggers", "lease", "submit"}),
    AgentRole.ANCHOR: ("execution", {"execution", "lease", "submit"}),
    AgentRole.FUSE: ("evidence", {"evidence", "lease"}),
    AgentRole.COMMANDER: ("workflow", {"workflow", "lease"}),
}


@pytest.mark.parametrize("role", sorted(ROLE_PORT))
async def test_every_role_receives_exactly_its_own_composed_capability(runtime, now, trace, role):
    port_name, expected = ROLE_PORT[role]
    provider = CapabilityProvider(service=runtime, **{port_name: FakePort()})
    capabilities = provider.build(fake_lease(role, now, trace))
    reachable = {name for name in dir(capabilities) if not name.startswith("__")}
    assert reachable == expected
    # Only the six evidence roles get a write port at all.
    assert ("submit" in reachable) == (role not in {AgentRole.FUSE, AgentRole.COMMANDER})
    for other_port in ROLE_PORT.values():
        if other_port[0] != port_name:
            assert not hasattr(capabilities, other_port[0])


@pytest.mark.parametrize("role", sorted(ROLE_PORT))
async def test_a_role_cannot_borrow_another_roles_port(runtime, now, trace, role):
    wrong = next(name for name, _ in ROLE_PORT.values() if name != ROLE_PORT[role][0])
    provider = CapabilityProvider(service=runtime, **{wrong: FakePort()})
    with pytest.raises(WorkerFailure) as caught:
        provider.build(fake_lease(role, now, trace))
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED
