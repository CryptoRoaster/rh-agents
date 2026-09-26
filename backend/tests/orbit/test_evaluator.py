"""One ORBIT, whoever asks it.

The TradeCase worker and the early-discovery scout both evaluate a market with
ORBIT. They must be the same ORBIT: the same instructions, the same prompt hash,
the same document shown to the model, the same validator and the same digest.
A second prompt or a second validator would be a second analyst whose
disagreements with the first nobody could see.

The pinned values below were taken from `main` before the evaluator existed.
They are the regression: extracting the evaluator must not change what the
TradeCase path sends, hashes or accepts.
"""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from src.agents.orbit import evaluator as evaluator_module
from src.agents.orbit.context import (
    evaluation_input,
    observation_document,
    orbit_input_digest,
    reasoning_payload,
)
from src.agents.orbit.evaluator import OrbitEvaluation, OrbitEvaluator
from src.agents.orbit.handler import OrbitWorkerHandler
from src.agents.orbit.models import OrbitEvaluationInput, OrbitTaskInput
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS, ORBIT_PROMPT_HASH, ORBIT_PROMPT_VERSION
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from src.core.clock import FixedClock
from src.orchestration.worker.capabilities import OrbitCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    WorkerFailureCategory,
)
from src.reasoning.fake import DeterministicReasoningProvider
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure
from tests.orbit.conftest import DISCOVERY_FLOOR, MAX_INPUT_AGE, assessment_for, reader_for

# Pinned on `main` at fd98ce0, before the evaluator was extracted.
PINNED_PROMPT_VERSION = "orbit-v1"
PINNED_PROMPT_HASH = "d823b391500c583928f9aafa0a2be829fde61cb722b7f2d3504a1b53b372615d"
PINNED_FIXTURE_DIGEST = "32381f4ba421c18e287bbac7b526b6c58e72a78d46393534f1c8fe6a130892da"
PINNED_FIXTURE_PAYLOAD = "9f10871cdb5354cd0f3e36a32eb51ddeea073d8f62375a924238ac71caac6de6"


def payload_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_tradecase_orbit_prompt_is_unchanged():
    assert ORBIT_PROMPT_VERSION == PINNED_PROMPT_VERSION
    assert hashlib.sha256(ORBIT_INSTRUCTIONS.encode()).hexdigest() == PINNED_PROMPT_HASH


def test_tradecase_orbit_prompt_hash_is_unchanged():
    assert ORBIT_PROMPT_HASH == PINNED_PROMPT_HASH


async def test_tradecase_orbit_digest_is_unchanged(task_input):
    assert orbit_input_digest(task_input) == PINNED_FIXTURE_DIGEST
    assert payload_fingerprint(reasoning_payload(task_input)) == PINNED_FIXTURE_PAYLOAD


async def test_the_tradecase_input_is_an_evaluation_input_plus_bookkeeping(task_input):
    """The workflow identifiers ride along beside the model input, never inside it."""
    assert isinstance(task_input, OrbitEvaluationInput)
    shown = json.dumps(reasoning_payload(task_input))
    for bookkeeping in (
        task_input.trade_case_id,
        task_input.task_id,
        task_input.discovery_reference,
    ):
        assert str(bookkeeping) not in shown


async def test_scout_and_tradecase_show_the_model_the_same_market_document(
    snapshot, now, trace, task_input
):
    scout = evaluation_input(
        snapshot, liquidity_floor_usd=DISCOVERY_FLOOR, max_input_age=MAX_INPUT_AGE, now=now
    )
    assert type(scout) is OrbitEvaluationInput
    assert observation_document(scout) == observation_document(task_input)
    assert reasoning_payload(scout) == reasoning_payload(task_input)
    assert orbit_input_digest(scout) == orbit_input_digest(task_input)


async def test_scout_and_tradecase_use_the_same_validator(task_input):
    """There is exactly one validator, and the evaluator calls it."""
    assert evaluator_module.validate_assessment is validate_assessment
    lying = assessment_for(task_input, pair_id="robinhood:mainnet:somewhere-else")
    evaluator = OrbitEvaluator(
        provider=DeterministicReasoningProvider.returning(lying.model_dump())
    )
    with pytest.raises(OrbitValidationError) as refused:
        await evaluator.evaluate(task_input)
    assert refused.value.reason_code == "MARKET_MISMATCH"


async def test_the_evaluator_sends_the_one_orbit_prompt(snapshot, now):
    scout = evaluation_input(
        snapshot, liquidity_floor_usd=DISCOVERY_FLOOR, max_input_age=MAX_INPUT_AGE, now=now
    )
    reply = assessment_for(scout)
    provider = DeterministicReasoningProvider.returning(reply.model_dump())
    result = await OrbitEvaluator(provider=provider).evaluate(scout)
    assert isinstance(result, OrbitEvaluation)
    (request,) = provider.calls
    assert request.instructions == ORBIT_INSTRUCTIONS
    assert request.data == reasoning_payload(scout)
    assert result.input_digest == orbit_input_digest(scout)
    assert result.assessment == reply
    assert result.model.provider == provider.name


async def test_a_provider_failure_reaches_the_caller_typed(snapshot, now):
    scout = evaluation_input(
        snapshot, liquidity_floor_usd=DISCOVERY_FLOOR, max_input_age=MAX_INPUT_AGE, now=now
    )
    provider = DeterministicReasoningProvider.failing(ReasoningErrorCategory.PROVIDER_TIMEOUT)
    with pytest.raises(ReasoningFailure) as failed:
        await OrbitEvaluator(provider=provider).evaluate(scout)
    assert failed.value.category is ReasoningErrorCategory.PROVIDER_TIMEOUT


async def test_the_scout_input_refuses_stale_and_future_observations(snapshot, now):
    from src.agents.orbit.context import OrbitContextUnavailable

    with pytest.raises(OrbitContextUnavailable) as stale:
        evaluation_input(
            snapshot,
            liquidity_floor_usd=DISCOVERY_FLOOR,
            max_input_age=MAX_INPUT_AGE,
            now=now + MAX_INPUT_AGE + MAX_INPUT_AGE,
        )
    assert stale.value.reason_code == "MARKET_OBSERVATION_TOO_STALE"
    with pytest.raises(OrbitContextUnavailable) as future:
        evaluation_input(
            snapshot,
            liquidity_floor_usd=DISCOVERY_FLOOR,
            max_input_age=MAX_INPUT_AGE,
            now=now - MAX_INPUT_AGE,
        )
    assert future.value.reason_code == "MARKET_OBSERVATION_IN_FUTURE"


# ------------------------------------------------ the TradeCase worker, unchanged


def lease_for(task_input: OrbitTaskInput, now) -> TaskLease:
    return TaskLease(
        lease_id=uuid4(),
        task_id=task_input.task_id,
        trade_case_id=task_input.trade_case_id,
        role="ORBIT",
        task_type="VERIFY_DISCOVERY",
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=uuid4(),
    )


class FixedContext:
    def __init__(self, task_input):
        self.task_input = task_input

    async def candidate_context(self, trade_case_id, task_id):
        return self.task_input


def capabilities(task_input, now):
    return OrbitCapabilities(
        lease=lease_for(task_input, now), context=FixedContext(task_input), submit=None
    )


async def test_the_worker_evidence_is_built_exactly_as_before(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    task_input = await reader.candidate_context(reader.cases.trade_case.id, uuid4())
    reply = assessment_for(task_input)
    handler = OrbitWorkerHandler(
        provider=DeterministicReasoningProvider.returning(reply.model_dump())
    )
    outcome = await handler.handle(lease_for(task_input, now), capabilities(task_input, now))
    assert isinstance(outcome, EvidenceTaskResult)
    assessment = outcome.submission.payload.assessment
    assert assessment.input_digest == PINNED_FIXTURE_DIGEST
    assert assessment.prompt_hash == PINNED_PROMPT_HASH
    assert assessment.prompt_version == PINNED_PROMPT_VERSION
    assert outcome.result_key == f"orbit:{PINNED_FIXTURE_DIGEST}"
    assert (
        outcome.submission.idempotency_key == f"orbit:{task_input.task_id}:{PINNED_FIXTURE_DIGEST}"
    )


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ReasoningErrorCategory.PROVIDER_TIMEOUT, WorkerFailureCategory.TRANSIENT),
        (ReasoningErrorCategory.PROVIDER_RATE_LIMIT, WorkerFailureCategory.TRANSIENT),
        (ReasoningErrorCategory.PROVIDER_UNAVAILABLE, WorkerFailureCategory.TRANSIENT),
        (ReasoningErrorCategory.PROVIDER_REFUSED, WorkerFailureCategory.INVALID_RESULT),
        (ReasoningErrorCategory.INVALID_MODEL_OUTPUT, WorkerFailureCategory.INVALID_RESULT),
        (ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST, WorkerFailureCategory.INTERNAL),
        (ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, WorkerFailureCategory.CAPABILITY_DENIED),
    ],
)
async def test_the_worker_failure_mapping_is_unchanged(task_input, now, category, expected):
    handler = OrbitWorkerHandler(provider=DeterministicReasoningProvider.failing(category))
    outcome = await handler.handle(lease_for(task_input, now), capabilities(task_input, now))
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category is expected
    assert outcome.reason_code == category.value


async def test_the_worker_still_refuses_contradicted_output(task_input, now):
    lying = assessment_for(task_input, chain="bsc")
    handler = OrbitWorkerHandler(
        provider=DeterministicReasoningProvider.returning(lying.model_dump())
    )
    outcome = await handler.handle(lease_for(task_input, now), capabilities(task_input, now))
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category is WorkerFailureCategory.INVALID_RESULT
    assert outcome.reason_code == "CHAIN_MISMATCH"


def test_no_second_orbit_prompt_or_validator_exists():
    """The scout package carries no instructions and no validator of its own."""
    from pathlib import Path

    scout = Path(__file__).parents[2] / "src" / "scout"
    text = "\n".join(path.read_text() for path in sorted(scout.glob("*.py")))
    assert "You are ORBIT" not in text
    assert "def validate_assessment" not in text
    assert "ORBIT_INSTRUCTIONS =" not in text
    assert "OrbitEvaluator" in text


async def test_the_clock_used_for_the_scout_input_is_the_callers(snapshot, now):
    """`evaluated_at` is the caller's instant; nothing reads a wall clock."""
    scout = evaluation_input(
        snapshot, liquidity_floor_usd=DISCOVERY_FLOOR, max_input_age=MAX_INPUT_AGE, now=now
    )
    assert scout.evaluated_at == FixedClock(now).now()
    assert scout.candidate.age_seconds == 0
