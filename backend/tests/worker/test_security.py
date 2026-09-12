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
    async def onchain_context(self, trade_case_id, task_id) -> object:
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
    assert reachable == {"lease", "context", "submit"}

    # The bound write port cannot be aimed at another task or another attempt.
    submit_surface = {name for name in dir(capabilities.submit) if not name.startswith("_")}
    assert submit_surface == {"lease", "submit_evidence"}
    assert capabilities.submit.lease.task_id == lease.task_id
    # The read port offers one question and no way to ask another.
    assert {name for name in dir(capabilities.context) if not name.startswith("_")} == {
        "onchain_context"
    }


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
    AgentRole.ORBIT: ("context", {"context", "lease", "submit"}),
    AgentRole.ATLAS: ("onchain", {"context", "lease", "submit"}),
    AgentRole.SIGNAL: ("sentiment", {"context", "lease", "submit"}),
    AgentRole.VECTOR: ("setup", {"context", "lease", "submit"}),
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
    # No role reaches an attribute that belongs only to some other role. ORBIT and
    # ATLAS both name their read port "context", so compare surfaces rather than
    # assuming the provider field and the capability attribute share a name.
    foreign = {attribute for _, surface in ROLE_PORT.values() for attribute in surface} - expected
    assert reachable.isdisjoint(foreign)


@pytest.mark.parametrize("role", sorted(ROLE_PORT))
async def test_a_role_cannot_borrow_another_roles_port(runtime, now, trace, role):
    wrong = next(name for name, _ in ROLE_PORT.values() if name != ROLE_PORT[role][0])
    provider = CapabilityProvider(service=runtime, **{wrong: FakePort()})
    with pytest.raises(WorkerFailure) as caught:
        provider.build(fake_lease(role, now, trace))
    assert caught.value.code == WorkerErrorCode.ROLE_NOT_AUTHORIZED


# ------------------------------------------------- runtime identity and leases


async def test_registration_retry_within_one_start_is_idempotent(runtime):
    """A: the same registration request resolves to the same instance."""
    from src.orchestration.worker.models import WorkerRegistration

    registration = WorkerRegistration(
        registration_key="runtime:fixed-start",
        role=AgentRole.ATLAS,
        runtime_version="worker-runtime-v1",
    )
    first = await runtime.register_worker(registration)
    retried = await runtime.register_worker(registration)
    assert retried.worker_instance_id == first.worker_instance_id


async def test_two_genuine_runtime_starts_are_two_instances(runtime, now, trace):
    """B: same role and version, two process lifetimes, two identities."""
    from src.orchestration.worker.runner import new_registration_key
    from tests.worker.test_scenarios import CrashingAtlasWorker, atlas_provider

    first_key = new_registration_key()
    second_key = new_registration_key()
    assert first_key != second_key

    from src.orchestration.worker.runner import WorkerRunner

    runners = [
        WorkerRunner(runtime, CrashingAtlasWorker(), atlas_provider(runtime)) for _ in range(2)
    ]
    # A runner mints its own key, so nothing pins a role to a permanent identity.
    assert runners[0].registration_key != runners[1].registration_key
    ids = [await runner.register() for runner in runners]
    assert ids[0] != ids[1]
    assert len(await runtime.workers(role=AgentRole.ATLAS)) == 2


async def test_registration_key_is_never_derived_from_role_or_version(runtime):
    from src.orchestration.worker.runner import new_registration_key

    keys = {new_registration_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert "ATLAS" not in key and "worker-runtime-v1" not in key


async def test_lease_ids_are_random_and_not_derived(worker_db, now, trace):
    """lease_id must stay unguessable; identical inputs still yield new tokens."""
    from datetime import timedelta

    from src.orchestration.worker.policy import WORKER_RUNTIME_V1
    from tests.worker.conftest import build_runtime
    from tests.worker.test_worker_runtime import register

    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    _, first = await claimed(runtime, now, trace, key="identity-lease")
    # Recovery starts the retry backoff, so reclaiming happens one step later.
    expiry = build_runtime(sessions, now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    await expiry.recover_expired_leases()
    later = build_runtime(
        sessions,
        now
        + WORKER_RUNTIME_V1.lease_duration
        + WORKER_RUNTIME_V1.retry_delay(1)
        + timedelta(seconds=2),
    )
    second_worker = await register(later, key="identity-lease-2")
    second = await later.claim_next_task(second_worker.worker_instance_id)
    assert second is not None
    # Same task, same role: a derived token would repeat. A random one cannot.
    assert second.lease_id != first.lease_id
    assert second.lease_id.version == 4


async def test_identity_and_lease_are_not_interchangeable(worker_db, now, trace):
    """C-H: only the exact current pairing may mutate anything."""
    from datetime import timedelta

    from src.orchestration.worker.policy import WORKER_RUNTIME_V1
    from tests.worker.conftest import build_runtime
    from tests.worker.test_worker_runtime import evidence_result, register

    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "identity-matrix")
    old_worker = await register(runtime, key="identity-old")
    old_lease = await runtime.claim_next_task(old_worker.worker_instance_id)
    assert old_lease is not None

    # C: the process dies; its replacement is a different runtime instance.
    expiry = build_runtime(sessions, now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    await expiry.recover_expired_leases()
    later = build_runtime(
        sessions,
        now
        + WORKER_RUNTIME_V1.lease_duration
        + WORKER_RUNTIME_V1.retry_delay(1)
        + timedelta(seconds=2),
    )
    new_worker = await register(later, key="identity-new")
    assert new_worker.worker_instance_id != old_worker.worker_instance_id

    # D: the replacement reclaims and receives a genuinely new lease.
    new_lease = await later.claim_next_task(new_worker.worker_instance_id)
    assert new_lease is not None
    assert new_lease.lease_id != old_lease.lease_id
    assert new_lease.attempt_number == old_lease.attempt_number + 1

    result = evidence_result(trade_case, new_lease, later.clock.now(), result_key="matrix")

    # E: old identity with its own old lease.
    with pytest.raises(WorkerFailure) as caught:
        await later.submit_task_result(old_lease, result)
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED

    # F: new identity presenting the old lease token.
    with pytest.raises(WorkerFailure) as caught:
        await later.submit_task_result(
            old_lease.model_copy(update={"worker_instance_id": new_worker.worker_instance_id}),
            result,
        )
    assert caught.value.code in {
        WorkerErrorCode.LEASE_OWNER_MISMATCH,
        WorkerErrorCode.LEASE_EXPIRED,
    }

    # G: old identity presenting the current lease token.
    with pytest.raises(WorkerFailure) as caught:
        await later.submit_task_result(
            new_lease.model_copy(update={"worker_instance_id": old_worker.worker_instance_id}),
            result,
        )
    assert caught.value.code == WorkerErrorCode.LEASE_OWNER_MISMATCH

    # H: only the exact current pairing is accepted.
    accepted = await later.submit_task_result(new_lease, result)
    assert accepted.outcome.value == "SUCCEEDED"
    onchain = [
        item
        for item in await later.cases.evidence(trade_case.id)
        if item.evidence_type.value == "ONCHAIN_EVIDENCE"
    ]
    assert len(onchain) == 1


async def test_a_worker_without_standing_cannot_write_its_own_denial(worker_db, now, trace):
    """A denied audit record must never become an authorization bypass."""
    from datetime import timedelta

    from src.orchestration.worker.policy import WORKER_RUNTIME_V1
    from src.orchestration.worker.runner import WorkerRunner
    from tests.worker.conftest import build_runtime
    from tests.worker.test_scenarios import SuccessfulAtlasWorker, atlas_provider

    _, sessions = worker_db
    runtime = build_runtime(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "identity-standing")
    handler = SuccessfulAtlasWorker(trade_case=trade_case, now=now)
    runner = WorkerRunner(runtime, handler, atlas_provider(runtime))
    await runner.register()
    lease = await runtime.claim_next_task(runner.worker_instance_id)
    assert lease is not None

    expired = build_runtime(sessions, now + WORKER_RUNTIME_V1.lease_duration + timedelta(seconds=1))
    stale_runner = WorkerRunner(
        expired,
        handler,
        atlas_provider(expired),
        registration_key=runner.registration_key,
    )
    stale_runner.worker_instance_id = runner.worker_instance_id
    before = len(await expired.attempts(task_id=lease.task_id))
    with pytest.raises(WorkerFailure) as caught:
        await stale_runner._record_refusal(lease, WorkerFailure(WorkerErrorCode.LEASE_EXPIRED))
    assert caught.value.code == WorkerErrorCode.LEASE_EXPIRED
    # It propagated instead of writing; recovery owns the abandoned attempt.
    assert len(await expired.attempts(task_id=lease.task_id)) == before
