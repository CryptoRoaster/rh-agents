"""PULSE end to end: one check in, one typed answer out.

The question each test asks is what the *runtime* is told, because that is what
decides whether the case advances, the task is rescheduled, or somebody is paged.
The central distinction is between a condition that has not become true — which
must look like ordinary operation — and something that actually went wrong.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.pulse.handler import PULSE_TASK_TYPE, PulseWorkerHandler, trigger_digest
from src.agents.pulse.models import PulseTaskInput, TriggerOutcome
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.agents.pulse.ports import PulseContextUnavailable
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import PulseCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    TaskWaitReport,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import EvidenceStatus, EvidenceType
from tests.pulse.conftest import LEVEL, observed, task_input, watched


def lease_for(context, now, trace=None) -> TaskLease:
    return TaskLease(
        lease_id=uuid4(),
        task_id=context.task_id,
        trade_case_id=context.trade_case_id,
        role=AgentRole.PULSE,
        task_type=PULSE_TASK_TYPE,
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace or uuid4(),
    )


class Fixed:
    def __init__(self, context) -> None:
        self._context = context

    async def trigger_context(self, trade_case_id, task_id):
        return self._context


class Failing:
    def __init__(self, reason_code: str) -> None:
        self._reason_code = reason_code

    async def trigger_context(self, trade_case_id, task_id):
        raise PulseContextUnavailable(self._reason_code)


class NoSubmit:
    @property
    def lease(self):  # pragma: no cover - presence is the point
        raise AssertionError("A handler must not read the lease from the submit port")

    async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
        raise AssertionError("A handler must not submit evidence itself")


async def run(now, *, context=None, port=None, trace=None):
    context = context if context is not None else task_input(now)
    handler = PulseWorkerHandler()
    lease = lease_for(context, now, trace)
    outcome = await handler.handle(
        lease,
        PulseCapabilities(lease=lease, context=port or Fixed(context), submit=NoSubmit()),
    )
    return context, outcome


# ------------------------------------------------------ a crossing


async def test_a_crossing_becomes_trigger_evidence(now):
    context, outcome = await run(
        now, context=task_input(now, observation=observed(now, price=LEVEL))
    )
    assert isinstance(outcome, EvidenceTaskResult)
    submission = outcome.submission
    assert submission.evidence_type == EvidenceType.TRIGGER
    assert submission.producer_role == AgentRole.PULSE
    assert submission.status == EvidenceStatus.AVAILABLE

    payload = submission.payload
    assert payload.setup_evidence_id == context.trigger.setup_evidence_id
    assert payload.observed_price == LEVEL
    assert payload.trigger_code == "PRICE_GTE"


async def test_the_evidence_records_the_comparison_that_was_made(now):
    """Why did PULSE say TRIGGERED? The record answers without prose."""
    context, outcome = await run(
        now, context=task_input(now, observation=observed(now, price=LEVEL))
    )
    assert isinstance(outcome, EvidenceTaskResult)
    detail = outcome.submission.payload.detail
    assert detail is not None
    trigger, observation = context.trigger, context.observation
    assert detail.trigger_type == "PRICE_GTE"
    assert detail.reference_price == trigger.reference_price
    assert detail.setup_id == trigger.setup_id
    assert detail.setup_fingerprint == trigger.setup_fingerprint
    assert detail.observed_at == observation.observed_at
    assert detail.observation_id == observation.observation_id
    assert detail.pair_id == observation.pair_id
    assert detail.price_basis == "USD_PER_BASE_UNIT"
    assert detail.policy_version == PULSE_TRIGGER_V1.version
    # The evidence stops being current exactly when the setup it fired for does.
    assert outcome.submission.valid_until == trigger.expires_at
    # Source time, never the moment it was read.
    assert outcome.submission.observed_at == observation.observed_at


async def test_the_evidence_carries_no_judgement_of_any_kind(now):
    context, outcome = await run(
        now, context=task_input(now, observation=observed(now, price=LEVEL))
    )
    assert isinstance(outcome, EvidenceTaskResult)
    # The envelope carries a shared confidence field for every evidence type.
    # A comparison between two Decimals has no confidence, so PULSE leaves it
    # empty rather than inventing a number to put there.
    assert outcome.submission.confidence is None
    assert outcome.submission.reason_codes == ()

    payload = json.loads(outcome.submission.model_dump_json())["payload"]
    for forbidden in (
        "confidence",
        "score",
        "sentiment",
        "rationale",
        "summary",
        "position_size",
        "notional_usd",
        "slippage_bps",
        "route",
        "risk_outcome",
        "approved",
    ):
        assert forbidden not in payload
        assert forbidden not in payload["detail"]


# --------------------------------------------- waiting is not failing


@pytest.mark.parametrize(
    ("observation_price", "reason"),
    [(Decimal("1.19"), "CONDITION_NOT_MET")],
)
async def test_an_unmet_condition_is_a_wait_not_a_failure(now, observation_price, reason):
    """Scenario O. The normal answer, possibly for hours."""
    _, outcome = await run(
        now, context=task_input(now, observation=observed(now, price=observation_price))
    )
    assert isinstance(outcome, TaskWaitReport)
    assert not isinstance(outcome, TaskFailureReport)
    assert outcome.reason_code == reason
    assert outcome.retry_after == PULSE_TRIGGER_V1.poll_interval


async def test_a_stale_price_is_a_wait(now):
    """Scenario G at the handler: the feed may catch up."""
    _, outcome = await run(
        now, context=task_input(now, observation=observed(now, price=LEVEL, seconds_ago=600))
    )
    assert isinstance(outcome, TaskWaitReport)
    assert outcome.reason_code == "OBSERVATION_TOO_STALE"


async def test_a_price_older_than_a_fresh_setup_is_a_wait(now):
    """The ordinary state of affairs the instant after VECTOR publishes.

    Classifying this as a fault would fail the task on every new setup, because
    the newest recorded snapshot is always older than the proposal that was just
    written.
    """
    trigger = watched(now, valid_from=now - timedelta(seconds=5))
    _, outcome = await run(
        now,
        context=task_input(
            now, trigger=trigger, observation=observed(now, price=LEVEL, seconds_ago=30)
        ),
    )
    assert isinstance(outcome, TaskWaitReport)
    assert outcome.reason_code == "OBSERVATION_BEFORE_SETUP"


async def test_no_current_setup_is_a_wait(now):
    _, outcome = await run(now, context=task_input(now, trigger=None))
    assert isinstance(outcome, TaskWaitReport)
    assert outcome.reason_code == "NO_CURRENT_SETUP"


async def test_an_expired_setup_is_a_wait_rather_than_an_error(now):
    """Scenario J. The window closed; that is an answer, not a fault."""
    expired = watched(
        now, valid_from=now - timedelta(hours=3), expires_at=now - timedelta(minutes=1)
    )
    _, outcome = await run(
        now,
        context=task_input(now, trigger=expired, observation=observed(now, price=Decimal("1.50"))),
    )
    assert isinstance(outcome, TaskWaitReport)
    assert outcome.reason_code == "SETUP_EXPIRED"


async def test_no_waiting_answer_ever_produces_evidence(now):
    """Scenario O and §27: a poll that found nothing writes nothing at all."""
    cases = [
        task_input(now, observation=observed(now, price=Decimal("1.19"))),
        task_input(now, observation=observed(now, price=LEVEL, seconds_ago=600)),
        task_input(now, observation=None),
        task_input(now, trigger=None),
        task_input(
            now,
            trigger=watched(
                now, valid_from=now - timedelta(hours=3), expires_at=now - timedelta(minutes=1)
            ),
        ),
    ]
    for context in cases:
        _, outcome = await run(now, context=context)
        assert not isinstance(outcome, EvidenceTaskResult), context


# ------------------------------------------- a fault is a fault


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        ("wrong_market", "MARKET_IDENTITY_MISMATCH"),
        ("wrong_basis", "PRICE_BASIS_MISMATCH"),
        ("future", "OBSERVATION_IN_FUTURE"),
    ],
)
async def test_an_incomparable_observation_is_an_internal_fault(now, observation, reason):
    """Scenarios H, I, Q, R. Waiting for these would wait forever."""
    variants = {
        "wrong_market": observed(
            now, price=LEVEL, pair_id="robinhood:mainnet:contract_address:0x" + "ff" * 20
        ),
        "wrong_basis": observed(now, price=LEVEL).model_copy(
            update={"price_basis": "USD_PER_QUOTE"}
        ),
        "future": observed(now, price=LEVEL).model_copy(
            update={"observed_at": now + timedelta(minutes=5)}
        ),
    }
    _, outcome = await run(now, context=task_input(now, observation=variants[observation]))
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.INTERNAL
    assert outcome.reason_code == reason


async def test_scenario_p_a_market_layer_that_cannot_answer_is_transient(now):
    """A provider outage is weather. It is never a neutral wait and never a trigger."""
    _, outcome = await run(now, port=Failing("MARKET_UNAVAILABLE"))
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.TRANSIENT
    assert outcome.reason_code == "MARKET_UNAVAILABLE"


async def test_the_wrong_capability_is_refused_before_anything_else(now):
    outcome = await PulseWorkerHandler().handle(lease_for(task_input(now), now), object())
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.CAPABILITY_DENIED


async def test_a_context_port_returning_the_wrong_shape_is_caught(now):
    class WrongShape:
        async def trigger_context(self, trade_case_id, task_id):
            return {"price": "1.20"}

    context = task_input(now)
    lease = lease_for(context, now)
    outcome = await PulseWorkerHandler().handle(
        lease, PulseCapabilities(lease=lease, context=WrongShape(), submit=NoSubmit())
    )
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.reason_code == "CONTEXT_SCHEMA_MISMATCH"


# ------------------------------------------------ scheduling the next look


async def test_the_next_check_is_never_scheduled_past_the_window(now):
    """Queueing a check that cannot possibly succeed is pure waste."""
    closing = watched(now, expires_at=now + timedelta(seconds=20))
    _, outcome = await run(
        now,
        context=task_input(now, trigger=closing, observation=observed(now, price=Decimal("1.19"))),
    )
    assert isinstance(outcome, TaskWaitReport)
    assert outcome.retry_after == timedelta(seconds=20)
    assert outcome.retry_after < PULSE_TRIGGER_V1.poll_interval


async def test_the_ordinary_cadence_matches_how_often_data_changes(now):
    """Polling faster than the market layer records re-reads the same number."""
    from src.core.config import Settings

    configured = Settings(_env_file=None, database_url="postgresql+asyncpg://u@localhost/d")
    assert PULSE_TRIGGER_V1.poll_interval == timedelta(seconds=90)
    assert PULSE_TRIGGER_V1.poll_interval >= timedelta(
        seconds=configured.market_watch_interval_seconds
    )


# ---------------------------------------------------------- the digest


async def test_the_same_crossing_fingerprints_identically(now):
    context = task_input(now, observation=observed(now, price=LEVEL))
    first = await run(now, context=context)
    again = await run(now, context=context)
    assert isinstance(first[1], EvidenceTaskResult) and isinstance(again[1], EvidenceTaskResult)
    assert first[1].result_key == again[1].result_key


async def test_a_different_worker_and_attempt_produce_the_same_digest(now):
    """The digest is of the event, not of who noticed it.

    Otherwise a replay by another worker would look like a second crossing.
    """
    context = task_input(now, observation=observed(now, price=LEVEL))
    first = await run(now, context=context, trace=uuid4())
    again = await run(now, context=context, trace=uuid4())
    assert first[1].result_key == again[1].result_key


@pytest.mark.parametrize(
    "change",
    [
        {"price": Decimal("1.25")},
        {"observation_id": uuid4()},
        {"pair_id": "robinhood:mainnet:contract_address:0x" + "dd" * 20},
    ],
)
async def test_a_different_observation_is_a_different_event(now, change):
    base = task_input(now, observation=observed(now, price=LEVEL))
    moved_observation = observed(now, price=LEVEL).model_copy(update=change)
    moved = base.model_copy(
        update={"observation": moved_observation, "market_pair_id": moved_observation.pair_id}
    )
    first = await run(now, context=base)
    second = await run(now, context=moved)
    assert isinstance(first[1], EvidenceTaskResult)
    if isinstance(second[1], EvidenceTaskResult):
        assert first[1].result_key != second[1].result_key


async def test_a_different_setup_is_a_different_event(now):
    base = task_input(now, observation=observed(now, price=LEVEL))
    other = base.model_copy(
        update={"trigger": watched(now, setup_evidence_id=uuid4(), setup_fingerprint="f" * 64)}
    )
    first = await run(now, context=base)
    second = await run(now, context=other)
    assert first[1].result_key != second[1].result_key


def test_the_digest_excludes_who_looked_and_when_they_looked(now):
    """Same factual crossing, same digest, whatever the runtime was doing.

    Asserted by behaviour rather than by reading the source: the docstring above
    the function names the very things it excludes, and a substring search would
    have failed for the wrong reason.
    """
    from src.agents.pulse.evaluator import evaluate

    context = task_input(now, observation=observed(now, price=LEVEL))
    evaluation = evaluate(context, now, PULSE_TRIGGER_V1)
    baseline = trigger_digest(
        context, context.trigger, context.observation, evaluation, PULSE_TRIGGER_V1
    )
    # A different task and a different case-level identity for the same crossing.
    elsewhere = context.model_copy(update={"task_id": uuid4()})
    assert (
        trigger_digest(
            elsewhere, elsewhere.trigger, elsewhere.observation, evaluation, PULSE_TRIGGER_V1
        )
        == baseline
    )
    # And the same crossing noticed a minute later by a slower worker.
    later = evaluate(context, now + timedelta(seconds=60), PULSE_TRIGGER_V1)
    assert later.outcome == evaluation.outcome
    assert (
        trigger_digest(context, context.trigger, context.observation, later, PULSE_TRIGGER_V1)
        == baseline
    )


# -------------------------------------------------------- no authority


def test_a_handler_exposes_one_role_and_one_task_type():
    handler = PulseWorkerHandler()
    assert handler.role == AgentRole.PULSE
    assert handler.task_type == "WAIT_FOR_TRIGGER"


def test_the_task_input_has_no_field_through_which_a_capability_could_arrive():
    for forbidden in ("session", "client", "markets", "provider_client", "rpc", "submit"):
        assert forbidden not in PulseTaskInput.model_fields


async def test_the_input_carries_no_client_session_or_url(now):
    rendered = task_input(now).model_dump_json()
    for forbidden in ("http://", "https://", "postgresql", "api_key", "Authorization"):
        assert forbidden not in rendered


def test_every_outcome_is_accounted_for():
    """No outcome may fall through to a default the handler did not consider."""
    handled = {
        TriggerOutcome.TRIGGERED,
        TriggerOutcome.OBSERVATION_INVALID,
        TriggerOutcome.SETUP_EXPIRED,
        TriggerOutcome.NOT_TRIGGERED,
        TriggerOutcome.NO_CURRENT_SETUP,
        TriggerOutcome.OBSERVATION_STALE,
        TriggerOutcome.OBSERVATION_PRECEDES_SETUP,
    }
    assert set(TriggerOutcome) == handled
