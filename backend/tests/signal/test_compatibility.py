"""Compatibility, workflow consequence, and the guards that stop bad inference.

Three separate questions this file answers explicitly rather than by implication.

Evidence written before Phase 2F must still parse, because an audit record that
becomes unreadable when the code moves on is not an audit record.

A required prerequisite that vanishes must not leave a case looking executable.
SIGNAL is not in the risk digest, which is correct and is *not* the same as
saying it stops mattering once risk has spoken.

And the deterministic guards must bound an over-confident model without ever
manufacturing a conclusion themselves — a cap is not a generator.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.signal.models import QualitativeLevel, SocialDemandIndication
from src.agents.signal.policy import SIGNAL_QUALITY_V1, assess_structure, exceeds_ceiling
from src.core.models import AgentRole
from src.orchestration.workflow.engine import TradeCaseEvaluator, risk_input_digest
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
    SentimentPayload,
    TradeCaseStatus,
)
from tests.signal.conftest import bound, campaign_set, organic_set
from tests.signal.test_quality import features_for

# Exactly the shape a sentiment envelope had before Phase 2F existed.
LEGACY_PAYLOAD = {"kind": "sentiment", "assessment": "POSITIVE"}


# ------------------------------------------------- legacy payload reading


@pytest.mark.parametrize("assessment", ["POSITIVE", "NEUTRAL", "NEGATIVE", "UNKNOWN"])
def test_sentiment_evidence_written_before_this_phase_still_parses(assessment):
    """The additive field must not turn historical rows into parse failures."""
    payload = SentimentPayload.model_validate({**LEGACY_PAYLOAD, "assessment": assessment})
    assert payload.assessment == assessment
    assert payload.intelligence is None
    assert payload.acceptance() == EvidenceAcceptance.ACCEPTED


def test_a_legacy_payload_round_trips_through_serialization():
    payload = SentimentPayload.model_validate(LEGACY_PAYLOAD)
    revived = SentimentPayload.model_validate_json(payload.model_dump_json())
    assert revived == payload


def test_absent_intelligence_is_distinguishable_from_empty_intelligence(now):
    """A record from before the deterministic layer is not a record of zero data.

    ``None`` means nobody measured; a present record with zero observations means
    somebody looked and found nothing. Collapsing the two would let an old row
    read as a measured silence.
    """
    legacy = SentimentPayload.model_validate(LEGACY_PAYLOAD)
    assert legacy.intelligence is None

    from src.agents.signal.handler import _metrics

    measured = SentimentPayload.model_validate(
        {
            **LEGACY_PAYLOAD,
            "assessment": "UNKNOWN",
            "intelligence": {
                "policy_version": SIGNAL_QUALITY_V1.version,
                "data_quality": "INSUFFICIENT",
                "attention_level": "VERY_LOW",
                "organic_breadth": "VERY_LOW",
                "manipulation_concern": "VERY_LOW",
                "gaps": ["NO_OBSERVATIONS"],
                "metrics": _metrics(features_for((), now), 21600).model_dump(mode="json"),
                "input_digest": "0" * 64,
            },
        }
    )
    assert measured.intelligence is not None
    assert measured.intelligence.metrics.observation_count == 0


def test_the_payload_stays_additive_rather_than_version_bumped():
    """No schema bump and no migration, because nothing existing changed shape.

    ``schema_version`` lives on the envelope and still reads 1. The new record is
    an optional field on a payload that is already serialized data, so old rows
    need no rewriting and new rows carry their own policy and prompt versions for
    anything that later needs to tell them apart.
    """
    from src.orchestration.workflow.models import EvidenceSubmission

    assert EvidenceSubmission.model_fields["schema_version"].default == 1
    payload = SentimentPayload.model_validate(LEGACY_PAYLOAD)
    assert "intelligence" in SentimentPayload.model_fields
    assert SentimentPayload.model_fields["intelligence"].default is None
    assert payload.model_dump()["intelligence"] is None


# ------------------------------------- required, and what that costs later


def test_sentiment_is_absent_from_the_risk_snapshot_by_construction():
    """Not a claim that sentiment stops mattering — a claim about which digest.

    The risk snapshot fingerprints the deterministic safety inputs SENTINEL is
    authorized against. Sentiment is a required analytical prerequisite rather
    than one of those, so it gates the workflow without being able to revoke an
    authorization, and the next test proves the gate still closes.
    """
    policy = TradeCaseEvaluator().policy
    requirement = policy.requirement(EvidenceType.SENTIMENT)
    assert requirement.required is True
    assert requirement.safety_critical is False
    assert requirement.before_trigger is True
    assert EvidenceType.SENTIMENT not in policy.safety_types


async def test_a_required_prerequisite_cannot_vanish_while_the_case_looks_executable(
    worker_db, now, trace
):
    """The coherence question the audit asks, answered against the real evaluator.

    SIGNAL is deliberately outside ``risk_input_digest``, so a fresh reading does
    not revoke a risk binding. That is not the same as sentiment becoming
    irrelevant: it is a required pre-trigger prerequisite, and the evaluator
    re-checks every one of those on each pass. When the reading goes stale the
    case leaves its authorized status through the evaluator rather than through
    the digest — which is exactly what the engine documents, and why a prerequisite
    cannot quietly disappear underneath an executable-looking case.
    """
    from uuid import uuid4

    from tests.signal.conftest import market_identity
    from tests.signal.test_workflow import build_stack, run_signal, sentiment_evidence

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    # A long-lived case, so the reading outlives its own validity well before the
    # case outlives its expiry and the test measures the thing it means to.
    trade_case = await runtime.cases.open_trade_case(
        market_identity(),
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key="signal-expiry",
        expires_at=now + timedelta(days=1),
    )
    await run_signal(runtime, reader, "signal-expiry-worker")

    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.AVAILABLE

    # An hour on, the reading has outlived its validity window.
    later = envelope.valid_until + timedelta(minutes=1)
    assert envelope.effective_status(later) == EvidenceStatus.STALE

    evaluation = TradeCaseEvaluator().evaluate(
        await runtime.cases.get_trade_case(trade_case.id),
        await runtime.cases.evidence(trade_case.id),
        None,
        later,
    )
    assert evaluation.status == TradeCaseStatus.EVIDENCE_PENDING
    assert any(blocker.role == AgentRole.SIGNAL for blocker in evaluation.blockers)


async def test_a_stale_reading_does_not_change_the_risk_digest(worker_db, now, trace):
    """Workflow eligibility and risk-binding validity are different questions."""
    from tests.signal.test_workflow import build_stack, open_case, run_signal

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now, organic_set(now))
    trade_case = await open_case(runtime.cases, now, trace, "signal-digest-stable")
    await run_signal(runtime, reader, "signal-digest-stable-worker")

    evidence = await runtime.cases.evidence(trade_case.id)
    current = {item.evidence_type: item for item in evidence if item.supersedes_id is None}
    before = risk_input_digest(trade_case, current)
    without_sentiment = {
        key: value for key, value in current.items() if key != EvidenceType.SENTIMENT
    }
    assert before == risk_input_digest(trade_case, without_sentiment)


# ------------------------------------------ degraded stays degraded


async def test_accepted_evidence_still_says_the_data_was_degraded(now):
    """``ACCEPTED`` is about the requirement, never a claim the data was healthy."""
    from src.reasoning.fake import DeterministicReasoningProvider
    from tests.signal.test_scenarios import reply, run

    _, outcome = await run(
        campaign_set(now),
        now,
        DeterministicReasoningProvider.returning(
            reply(social_demand_indication="NONE", summary="Repeated promotional text.")
        ),
    )
    submission = outcome.submission
    assert submission.payload.acceptance() == EvidenceAcceptance.ACCEPTED
    intelligence = submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.data_quality == "DEGRADED"
    assert intelligence.manipulation_concern == "HIGH"


# -------------------------------------------- the small-sample guard


@pytest.mark.parametrize("authors", [1, 2, 3, 4, 5, 6])
def test_a_sample_too_small_to_be_concentrated_is_never_flagged_as_concentrated(now, authors):
    """With few authors the top-five share is forced high by arithmetic alone.

    Six people writing once each produce 0.83 whatever they said. A threshold
    that fired there would mark every small honest conversation as dominated, so
    the term applies only where an even distribution would sit below it.
    """
    rows = tuple(
        bound(
            f"even-{index}",
            now=now,
            minutes_ago=10 + index,
            author=f"a{index}",
            text=f"DEMO {index}",
        )
        for index in range(authors)
    )
    features = features_for(rows, now)
    assert features.unique_authoring_count == authors
    structure = assess_structure(features, SIGNAL_QUALITY_V1)
    if authors <= 2:
        # One or two voices cross the top-one threshold trivially — and such a set
        # is already insufficient, so nothing downstream ever acts on it.
        assert structure.data_quality.value == "INSUFFICIENT"
    else:
        assert structure.manipulation_concern == QualitativeLevel.VERY_LOW


def test_a_single_authored_observation_degrades_confidence_rather_than_accusing(now):
    """One post is not evidence of manipulation; it is an absence of evidence."""
    single = (bound("solo", now=now, minutes_ago=10, author="only", text="DEMO exists"),)
    features = features_for(single, now)
    assert features.top1_author_share == Decimal(1)
    structure = assess_structure(features, SIGNAL_QUALITY_V1)
    assert structure.data_quality.value == "INSUFFICIENT"
    # Nothing is interpreted from it at all, so the concern cannot be acted on.
    assert structure.organic_breadth == QualitativeLevel.VERY_LOW


def test_no_share_divides_by_zero_on_an_empty_set(now):
    features = features_for((), now)
    for share in (
        features.top1_author_share,
        features.top5_author_share,
        features.duplicate_share,
        features.burst_share,
    ):
        assert share == Decimal(0)
        assert isinstance(share, Decimal)


def test_the_guard_boundary_is_derived_from_the_threshold_itself(now):
    """Seven authors is where 5/N first falls below 0.80 and the term can mean something."""
    threshold = SIGNAL_QUALITY_V1.top5_author_share_elevated
    assert Decimal(5) / Decimal(6) >= threshold
    assert Decimal(5) / Decimal(7) < threshold


# --------------------------------------------------- the demand ceiling


def test_the_ceiling_bounds_a_claim_and_never_produces_one(now):
    """Breadth caps demand. It does not create it.

    A wide, entirely factual discussion in which nobody expresses interest must
    stay at no demand indication — the ceiling permits more, and permission is
    not evidence.
    """
    features = features_for(organic_set(now), now)
    structure = assess_structure(features, SIGNAL_QUALITY_V1)
    assert structure.organic_breadth == QualitativeLevel.HIGH
    assert SIGNAL_QUALITY_V1.demand_ceiling(structure.organic_breadth) == (
        SocialDemandIndication.STRONG
    )
    # Everything at or below the ceiling is permitted, including nothing at all.
    for indication in SocialDemandIndication:
        assert not exceeds_ceiling(indication, structure.organic_breadth, SIGNAL_QUALITY_V1)


async def test_a_broad_conversation_without_demand_language_yields_no_demand(now):
    from src.reasoning.fake import DeterministicReasoningProvider
    from tests.signal.test_scenarios import reply, run

    _, outcome = await run(
        organic_set(now),
        now,
        DeterministicReasoningProvider.returning(
            reply(
                social_demand_indication="NONE",
                narrative_tags=["UTILITY_OR_PRODUCT"],
                summary="Mostly technical discussion; nobody expressed an intent to hold.",
            )
        ),
    )
    intelligence = outcome.submission.payload.intelligence
    assert intelligence is not None
    assert intelligence.organic_breadth == "HIGH"
    assert intelligence.social_demand_indication == "NONE"


def test_a_narrow_set_caps_an_optimistic_claim_in_the_other_direction(now):
    structure = assess_structure(features_for(campaign_set(now), now), SIGNAL_QUALITY_V1)
    assert structure.organic_breadth == QualitativeLevel.VERY_LOW
    assert exceeds_ceiling(
        SocialDemandIndication.WEAK, structure.organic_breadth, SIGNAL_QUALITY_V1
    )
    assert not exceeds_ceiling(
        SocialDemandIndication.NONE, structure.organic_breadth, SIGNAL_QUALITY_V1
    )


# --------------------------------------- the fake source is test-only


def test_no_production_path_constructs_the_deterministic_source():
    """A synthetic feed must never be able to carry a real case forward."""
    import subprocess

    hits = subprocess.run(
        ["git", "grep", "-l", "DeterministicSignalSource", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    )
    # It exists in exactly one place, and that place is the file named "fake".
    assert hits.stdout.split() == ["backend/src/agents/signal/fake.py"]


def test_nothing_wires_a_signal_worker_at_startup():
    import subprocess

    for symbol in ("SignalWorkerHandler", "SignalContextReader", "signal_worker_enabled"):
        hits = subprocess.run(
            ["git", "grep", "-l", symbol, "--", "backend/src/api/", "backend/src/runtime/"],
            capture_output=True,
            text=True,
            cwd="..",
        )
        assert hits.stdout.strip() == "", f"{symbol} is reachable from a startup path"


def test_the_fake_source_is_never_a_configured_provider_option():
    from typing import get_args

    from src.core.config import Settings

    rendered = repr(Settings.model_fields)
    assert "fake-social" not in rendered
    assert "DeterministicSignalSource" not in rendered
    # Phase 2G added a real provider. The fixture source is still not among the
    # things a deployment can select, which is the invariant that matters.
    assert set(get_args(Settings.model_fields["signal_social_provider"].annotation)) == {
        "disabled",
        "neynar",
    }
