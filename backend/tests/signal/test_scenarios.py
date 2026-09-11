"""SIGNAL end to end, one scenario per social shape that actually occurs.

Each test drives the real handler over the real deterministic layer with a
scripted model, and asks the question that scenario exists to answer: does the
system tell a conversation from a campaign, loudness from breadth, and silence
from indifference?
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.agents.signal.handler import SIGNAL_TASK_TYPE, SignalWorkerHandler
from src.agents.signal.models import (
    QualitativeLevel,
    SentimentDirection,
    SentimentStrength,
    SignalDataQuality,
    SignalGap,
    SignalNarrative,
    SocialDemandIndication,
)
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import SignalCapabilities
from src.orchestration.worker.models import (
    EvidenceTaskResult,
    TaskFailureReport,
    TaskLease,
    WorkerFailureCategory,
)
from src.orchestration.workflow.models import EvidenceStatus, EvidenceType
from src.reasoning.fake import DeterministicReasoningProvider, ScriptedReply
from src.reasoning.models import ReasoningErrorCategory
from tests.signal.conftest import (
    campaign_set,
    collision_set,
    influencer_set,
    injection_set,
    organic_set,
    stale_set,
    wrong_chain_set,
)
from tests.signal.test_context import read


def reply(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "sentiment_direction": SentimentDirection.POSITIVE.value,
        "sentiment_strength": SentimentStrength.MODERATE.value,
        "social_demand_indication": SocialDemandIndication.MODERATE.value,
        "narrative_tags": [SignalNarrative.UTILITY_OR_PRODUCT.value],
        "manipulation_observations": [],
        "cited_observation_ids": [],
        "summary": "Independent accounts discussed the product with mild approval.",
    }
    payload.update(overrides)
    return payload


def lease_for(task_input, now) -> TaskLease:
    return TaskLease(
        lease_id=uuid4(),
        task_id=task_input.task_id,
        trade_case_id=task_input.trade_case_id,
        role=AgentRole.SIGNAL,
        task_type=SIGNAL_TASK_TYPE,
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=uuid4(),
    )


class Fixed:
    def __init__(self, task_input) -> None:
        self._task_input = task_input

    async def sentiment_context(self, trade_case_id, task_id):
        return self._task_input


class NoSubmit:
    """The submission port a handler must never reach for by itself."""

    @property
    def lease(self):  # pragma: no cover - presence is the point
        raise AssertionError("A handler must not read the lease from the submit port")

    async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
        raise AssertionError("A handler must not submit evidence itself")


async def run(observations, now, provider, **kwargs):
    task_input = await read(observations, now, **kwargs)
    handler = SignalWorkerHandler(provider=provider)
    lease = lease_for(task_input, now)
    outcome = await handler.handle(
        lease, SignalCapabilities(lease=lease, context=Fixed(task_input), submit=NoSubmit())
    )
    return task_input, outcome


# ------------------------------------------------- A: organic positive set


async def test_scenario_a_an_organic_conversation_produces_usable_evidence(now):
    provider = DeterministicReasoningProvider.returning(
        reply(social_demand_indication=SocialDemandIndication.STRONG.value)
    )
    task_input, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    submission = outcome.submission
    assert submission.evidence_type == EvidenceType.SENTIMENT
    assert submission.producer_role == AgentRole.SIGNAL
    assert submission.status == EvidenceStatus.AVAILABLE
    assert submission.payload.assessment == "POSITIVE"

    intelligence = submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.data_quality == SignalDataQuality.USABLE.value
    assert intelligence.organic_breadth == QualitativeLevel.HIGH.value
    assert intelligence.manipulation_concern == QualitativeLevel.VERY_LOW.value
    assert intelligence.social_demand_indication == SocialDemandIndication.STRONG.value
    assert intelligence.metrics.unique_authoring_count == 26
    assert intelligence.metrics.duplicate_cluster_count == 0


async def test_scenario_a_carries_no_trading_authority_of_any_kind(now):
    provider = DeterministicReasoningProvider.returning(reply())
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    rendered = outcome.submission.model_dump_json()
    # Checked as JSON keys: "side" also occurs inside "outside_window_count", and
    # a substring match would pass for the wrong reason.
    for forbidden in ("side", "entry_price", "risk_binding", "approval", "position_size"):
        assert f'"{forbidden}"' not in rendered


# --------------------------------------------------- B: copy-paste campaign


async def test_scenario_b_a_shill_campaign_is_positive_loud_and_not_broad(now):
    """The headline case. Every axis disagrees with the others, and should.

    The language really is positive and there really is a lot of it. What there
    is not is a crowd, and no combination of those first two facts is allowed to
    manufacture one.
    """
    provider = DeterministicReasoningProvider.returning(
        reply(
            sentiment_direction=SentimentDirection.POSITIVE.value,
            sentiment_strength=SentimentStrength.STRONG.value,
            social_demand_indication=SocialDemandIndication.NONE.value,
            narrative_tags=[SignalNarrative.PROMOTIONAL_CALL_TO_ACTION.value],
            summary="One promotional sentence repeated by a small set of accounts.",
        )
    )
    _, outcome = await run(campaign_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    intelligence = outcome.submission.payload.intelligence
    assert intelligence is not None

    assert intelligence.sentiment_direction == SentimentDirection.POSITIVE.value
    assert intelligence.attention_level == QualitativeLevel.HIGH.value
    assert intelligence.organic_breadth == QualitativeLevel.VERY_LOW.value
    assert intelligence.manipulation_concern == QualitativeLevel.HIGH.value
    assert intelligence.social_demand_indication == SocialDemandIndication.NONE.value
    assert intelligence.data_quality == SignalDataQuality.DEGRADED.value
    assert SignalGap.DUPLICATE_DOMINATED.value in intelligence.gaps
    # Fifty-five posts, six things actually said.
    assert intelligence.metrics.observation_count == 55
    assert intelligence.metrics.unique_content_count == 6


async def test_scenario_b_a_confident_model_cannot_rescue_the_campaign(now):
    provider = DeterministicReasoningProvider.returning(
        reply(
            social_demand_indication=SocialDemandIndication.STRONG.value,
            summary="Enormous organic demand from a thriving community.",
        )
    )
    _, outcome = await run(campaign_set(now), now, provider)
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.INVALID_RESULT
    assert outcome.reason_code == "DEMAND_EXCEEDS_MEASURED_BREADTH"


# ------------------------------------------------------- C: one influencer


async def test_scenario_c_one_amplified_voice_is_loud_and_five_people_wide(now):
    provider = DeterministicReasoningProvider.returning(
        reply(social_demand_indication=SocialDemandIndication.WEAK.value)
    )
    _, outcome = await run(influencer_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    intelligence = outcome.submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.attention_level == QualitativeLevel.HIGH.value
    assert intelligence.organic_breadth == QualitativeLevel.LOW.value
    # Fifty participants; five of them wrote anything.
    assert intelligence.metrics.observation_count == 50
    assert intelligence.metrics.unique_author_count == 50
    assert intelligence.metrics.unique_authoring_count == 5
    assert intelligence.metrics.repost_count == 45


# ------------------------------------------------------------- D: no data


async def test_scenario_d_silence_is_insufficient_and_never_neutral(now):
    provider = DeterministicReasoningProvider.returning(reply())
    _, outcome = await run((), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    submission = outcome.submission
    assert submission.status == EvidenceStatus.UNKNOWN
    assert submission.payload.assessment == "UNKNOWN"
    assert SignalGap.NO_OBSERVATIONS.value in submission.reason_codes
    # No sentiment was invented, because none was read.
    intelligence = submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.sentiment_direction is None


async def test_scenario_d_an_empty_feed_is_never_paid_for(now):
    """A model call on nothing can only produce fiction, and costs money."""
    provider = DeterministicReasoningProvider.returning(reply())
    await run((), now, provider)
    assert provider.calls == []


# ------------------------------------------------------------ E: stale set


async def test_scenario_e_a_dead_window_is_stale_not_quiet(now):
    provider = DeterministicReasoningProvider.returning(reply())
    _, outcome = await run(stale_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.STALE
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW.value in outcome.submission.reason_codes
    assert provider.calls == []


async def test_scenario_e_fetching_old_posts_again_cannot_freshen_them(now):
    """The Phase 2D lesson, in social form: collection time proves nothing."""
    provider = DeterministicReasoningProvider.returning(reply())
    later = now + timedelta(hours=3)
    _, outcome = await run(stale_set(now), later, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.STALE


# -------------------------------------------------------- F: symbol clash


async def test_scenario_f_a_ticker_collision_produces_no_sentiment_for_this_token(now):
    provider = DeterministicReasoningProvider.returning(reply())
    task_input, outcome = await run(collision_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.UNKNOWN
    assert task_input.features.excluded_ambiguous_count == 10
    assert SignalGap.AMBIGUOUS_REFERENCES_EXCLUDED.value in outcome.submission.reason_codes
    assert provider.calls == []


# ------------------------------------------------------ G: address binding


async def test_scenario_g_an_exact_address_binds_and_another_chain_does_not(now):
    provider = DeterministicReasoningProvider.returning(reply())
    bound_input, _ = await run(organic_set(now), now, provider)
    assert bound_input.features.strong_binding_count == 26

    provider = DeterministicReasoningProvider.returning(reply())
    wrong_input, outcome = await run(wrong_chain_set(now), now, provider)
    assert wrong_input.features.observation_count == 0
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.UNKNOWN


# ------------------------------------------------------ H: prompt injection


async def test_scenario_h_a_post_telling_the_model_what_to_do_is_only_data(now):
    provider = DeterministicReasoningProvider.returning(reply())
    _, outcome = await run(injection_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.payload.assessment in {"POSITIVE", "NEUTRAL", "NEGATIVE"}

    request = provider.calls[0]
    rendered = repr(request.data)
    # The hostile text reached the model as quoted content, exactly as intended.
    assert "ignore all previous instructions" in rendered
    # And it reached it inside the data channel, never inside the instructions.
    assert "ignore all previous instructions" not in request.instructions.lower()
    assert "untrusted data" in request.instructions


async def test_scenario_h_an_injected_instruction_cannot_widen_the_schema(now):
    """Even a model that complied would have nowhere to put an approval."""
    provider = DeterministicReasoningProvider.returning(reply())
    _, outcome = await run(injection_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.evidence_type == EvidenceType.SENTIMENT
    assert "BUY" not in outcome.submission.model_dump_json()


# ------------------------------------------- I: model contradicts the facts


async def test_scenario_i_invented_sources_are_refused(now):
    provider = DeterministicReasoningProvider.returning(reply(cited_observation_ids=[str(uuid4())]))
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.reason_code == "UNKNOWN_OBSERVATION_REFERENCE"


async def test_scenario_i_contradicted_output_is_never_persisted_at_all(now):
    """Not as available evidence, and not quietly downgraded to unknown either."""
    provider = DeterministicReasoningProvider.returning(
        reply(social_demand_indication=SocialDemandIndication.STRONG.value)
    )
    _, outcome = await run(campaign_set(now), now, provider)
    assert isinstance(outcome, TaskFailureReport)


# ------------------------------------------------------- model unavailable


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ReasoningErrorCategory.PROVIDER_TIMEOUT, WorkerFailureCategory.TRANSIENT),
        (ReasoningErrorCategory.PROVIDER_RATE_LIMIT, WorkerFailureCategory.TRANSIENT),
        (ReasoningErrorCategory.INVALID_MODEL_OUTPUT, WorkerFailureCategory.INVALID_RESULT),
        (
            ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED,
            WorkerFailureCategory.CAPABILITY_DENIED,
        ),
    ],
)
async def test_a_model_failure_is_a_task_failure_and_never_neutral_sentiment(
    now, category, expected
):
    """SIGNAL has no deterministic fallback for what language means.

    ATLAS keeps its verdict when its model is unavailable because the verdict was
    never the model's. Here the reading *is* the output, so an unavailable model
    leaves the requirement unmet and the runtime retries — rather than recording a
    neutral opinion nobody formed.
    """
    provider = DeterministicReasoningProvider.failing(category)
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == expected


async def test_an_unreadable_direction_is_recorded_as_unknown_not_neutral(now):
    provider = DeterministicReasoningProvider.returning(
        reply(
            sentiment_direction=SentimentDirection.UNCLEAR.value,
            sentiment_strength=SentimentStrength.WEAK.value,
            social_demand_indication=SocialDemandIndication.NONE.value,
            summary="The sample is too mixed in tone to read a direction.",
        )
    )
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.UNKNOWN
    assert outcome.submission.payload.assessment == "UNKNOWN"
    assert "SENTIMENT_DIRECTION_UNCLEAR" in outcome.submission.reason_codes


async def test_a_negative_reading_is_valid_evidence_and_not_a_failure(now):
    """Bad news is an observation. Only broken data is a worker failure."""
    provider = DeterministicReasoningProvider.returning(
        reply(
            sentiment_direction=SentimentDirection.NEGATIVE.value,
            social_demand_indication=SocialDemandIndication.NONE.value,
            narrative_tags=[SignalNarrative.CRITICISM_OR_WARNING.value],
            summary="Several accounts raised concerns about the unlock schedule.",
        )
    )
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.status == EvidenceStatus.AVAILABLE
    assert outcome.submission.payload.assessment == "NEGATIVE"


async def test_a_mixed_reading_keeps_its_distinction_in_the_record(now):
    """The legacy field cannot hold MIXED. The intelligence record can."""
    provider = DeterministicReasoningProvider.returning(
        reply(
            sentiment_direction=SentimentDirection.MIXED.value,
            social_demand_indication=SocialDemandIndication.WEAK.value,
        )
    )
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, EvidenceTaskResult)
    assert outcome.submission.payload.assessment == "NEUTRAL"
    intelligence = outcome.submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.sentiment_direction == SentimentDirection.MIXED.value


# --------------------------------------------------------------- plumbing


async def test_a_scripted_reply_that_breaks_the_schema_is_an_invalid_result(now):
    provider = DeterministicReasoningProvider.scripted(
        [ScriptedReply(payload={"sentiment_direction": "MAYBE"})]
    )
    _, outcome = await run(organic_set(now), now, provider)
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.INVALID_RESULT


async def test_the_wrong_capability_is_refused_before_anything_else(now):
    handler = SignalWorkerHandler(provider=DeterministicReasoningProvider.returning(reply()))
    task_input = await read(organic_set(now), now)
    outcome = await handler.handle(lease_for(task_input, now), object())
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.category == WorkerFailureCategory.CAPABILITY_DENIED


async def test_the_result_key_follows_the_input_and_not_the_attempt(now):
    """Two attempts over identical data must resolve to one logical result."""
    first, first_outcome = await run(
        organic_set(now), now, DeterministicReasoningProvider.returning(reply())
    )
    second, second_outcome = await run(
        organic_set(now), now, DeterministicReasoningProvider.returning(reply())
    )
    assert isinstance(first_outcome, EvidenceTaskResult)
    assert isinstance(second_outcome, EvidenceTaskResult)
    assert first.task_id != second.task_id
    assert first_outcome.result_key == second_outcome.result_key
