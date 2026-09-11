from datetime import timedelta

import pytest

from src.core.models import AgentRole
from src.orchestration.worker.capabilities import (
    CAPABILITY_TYPES,
    CommanderCapabilities,
    FuseCapabilities,
)
from src.orchestration.worker.models import WorkerFailureCategory
from src.orchestration.worker.policy import (
    WORKER_RUNTIME_V1,
    WorkerRuntimePolicy,
    authorized_evidence_type,
    authorized_task_type,
    evidence_roles,
    role_evidence_matrix,
)
from src.orchestration.workflow.models import EvidenceType

EXPECTED_MATRIX = {
    AgentRole.ORBIT: EvidenceType.DISCOVERY,
    AgentRole.ATLAS: EvidenceType.ONCHAIN,
    AgentRole.SIGNAL: EvidenceType.SENTIMENT,
    AgentRole.VECTOR: EvidenceType.TRADE_SETUP,
    AgentRole.PULSE: EvidenceType.TRIGGER,
    AgentRole.ANCHOR: EvidenceType.LIQUIDITY_EXECUTION,
}


def test_role_evidence_matrix_is_exactly_the_workflow_requirements():
    assert role_evidence_matrix() == EXPECTED_MATRIX
    assert evidence_roles() == frozenset(EXPECTED_MATRIX)


@pytest.mark.parametrize("role,evidence", sorted(EXPECTED_MATRIX.items()))
def test_each_role_has_exactly_one_authorized_evidence_type(role, evidence):
    assert authorized_evidence_type(role) == evidence
    assert authorized_task_type(role) is not None
    # Exactly one type, and no other role shares it.
    assert [other for other in EXPECTED_MATRIX.values()].count(evidence) == 1


@pytest.mark.parametrize("role", [AgentRole.FUSE, AgentRole.COMMANDER])
def test_fuse_and_commander_have_no_evidence_authority(role):
    assert authorized_evidence_type(role) is None
    assert authorized_task_type(role) is None


def test_deterministic_services_are_not_worker_roles():
    # SENTINEL, LEDGER and EXECUTOR are absent from AgentRole entirely, so they
    # cannot be claimed, registered or handed capabilities.
    assert {member.value for member in AgentRole} == {
        "ORBIT",
        "ATLAS",
        "SIGNAL",
        "VECTOR",
        "PULSE",
        "ANCHOR",
        "FUSE",
        "COMMANDER",
    }


def test_fuse_and_commander_capabilities_expose_no_submission_port():
    assert not hasattr(FuseCapabilities, "submit")
    assert not hasattr(CommanderCapabilities, "submit")
    assert set(CAPABILITY_TYPES) == set(AgentRole)


@pytest.mark.parametrize(
    "capability",
    sorted(CAPABILITY_TYPES.values(), key=lambda item: item.__name__),
)
def test_no_capability_exposes_infrastructure(capability):
    forbidden = {
        "session",
        "sessions",
        "connection",
        "engine",
        "rpc",
        "http",
        "client",
        "signer",
        "wallet",
        "private_key",
        "executor",
        "ledger",
        "sentinel",
        "set_status",
        "force_transition",
    }
    fields = set(getattr(capability, "__dataclass_fields__", {}))
    assert fields.isdisjoint(forbidden)
    assert forbidden.isdisjoint(dir(capability))


@pytest.mark.parametrize(
    "category,retryable",
    [
        (WorkerFailureCategory.TRANSIENT, True),
        (WorkerFailureCategory.INVALID_RESULT, True),
        (WorkerFailureCategory.INTERNAL, True),
        (WorkerFailureCategory.CAPABILITY_DENIED, False),
        (WorkerFailureCategory.TASK_INVALIDATED, False),
    ],
)
def test_retry_classification_is_explicit_per_category(category, retryable):
    assert WORKER_RUNTIME_V1.is_retryable(category) is retryable


def test_backoff_is_deterministic_bounded_and_monotonic():
    delays = [WORKER_RUNTIME_V1.retry_delay(attempt) for attempt in range(1, 12)]
    assert delays == sorted(delays)
    assert delays[0] == WORKER_RUNTIME_V1.retry_initial_delay
    assert max(delays) <= WORKER_RUNTIME_V1.retry_max_delay
    assert delays == [WORKER_RUNTIME_V1.retry_delay(attempt) for attempt in range(1, 12)]


def test_backoff_rejects_invalid_attempt_numbers():
    with pytest.raises(ValueError):
        WORKER_RUNTIME_V1.retry_delay(0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lease_duration", timedelta(0)),
        ("max_attempts", 0),
        ("max_lease_renewals", -1),
        ("retry_initial_delay", timedelta(seconds=-1)),
        ("claim_batch", 0),
        ("claim_batch", 51),
    ],
)
def test_invalid_runtime_policy_is_rejected(field, value):
    base = {
        "version": "test",
        "lease_duration": timedelta(seconds=30),
        "max_attempts": 3,
        "max_lease_renewals": 5,
        "retry_initial_delay": timedelta(seconds=1),
        "retry_max_delay": timedelta(seconds=60),
        "claim_batch": 5,
    }
    with pytest.raises(ValueError):
        WorkerRuntimePolicy(**{**base, field: value})


def test_maximum_delay_below_initial_is_rejected():
    with pytest.raises(ValueError):
        WorkerRuntimePolicy(
            version="test",
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
            max_lease_renewals=5,
            retry_initial_delay=timedelta(seconds=60),
            retry_max_delay=timedelta(seconds=1),
            claim_batch=5,
        )
