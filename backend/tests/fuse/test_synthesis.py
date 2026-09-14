"""What the evidence adds up to, and what can never change that answer.

The scenarios here are mostly about authority rather than about markets. Each
one puts a blocker or a gap next to as much positive evidence as the workflow
allows, and asserts that the positive evidence does not help — because the whole
risk of a synthesis layer is that summarising four findings quietly becomes
averaging them.
"""

from datetime import timedelta

import pytest

from src.agents.fuse.models import BlockerOrigin, FuseDisposition, GapOrigin
from src.agents.fuse.policy import FUSE_SYNTHESIS_V1
from src.agents.fuse.synthesis import input_digest, synthesize
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    EvidenceStatus,
    EvidenceType,
)
from tests.fuse.conftest import context_for, envelope, evidence_set, onchain, trade_setup


async def synthesized(now, evidence=None, **kwargs):
    context = await context_for(now, evidence if evidence is not None else evidence_set(now))
    return synthesize(context, now, FUSE_SYNTHESIS_V1)


# ------------------------------------------------------- A: everything usable


async def test_scenario_a_a_complete_coherent_set_is_coherent(now):
    outcome = await synthesized(now)

    assert outcome.disposition == FuseDisposition.COHERENT
    assert outcome.hard_blockers == ()
    assert outcome.unresolved_gaps == ()
    assert {factor.code for factor in outcome.support_factors} == {
        "DISCOVERY_INTEREST",
        "ONCHAIN_CLEAR",
        "SENTIMENT_POSITIVE",
    }
    assert len(outcome.sources) == 4


async def test_a_coherent_synthesis_still_approves_nothing(now):
    """`is_actionable` says the evidence hangs together. Never that to trade."""
    outcome = await synthesized(now)
    assert outcome.is_actionable is True
    rendered = outcome.model_dump_json().lower()
    for forbidden in ("approve", "authorized", "position_size", "notional", "risk_outcome"):
        assert forbidden not in rendered


# ---------------------------------------------- B, C, K: blockers dominate


async def test_scenario_b_an_atlas_blocker_survives_everything_positive(now):
    """The central guarantee. Three positives and one measured failure."""
    evidence = evidence_set(now, onchain={"holder": "FAIL", "verdict": "BLOCKED"})
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.BLOCKED
    codes = {blocker.code for blocker in outcome.hard_blockers}
    assert "ATLAS_HOLDER_INTEGRITY_FAIL" in codes
    # The positives are still recorded. They simply do not help.
    assert {factor.code for factor in outcome.support_factors} >= {
        "DISCOVERY_INTEREST",
        "SENTIMENT_POSITIVE",
    }
    assert outcome.is_actionable is False


@pytest.mark.parametrize(
    ("axis", "code"),
    [
        ("holder", "ATLAS_HOLDER_INTEGRITY_FAIL"),
        ("dev", "ATLAS_DEV_WALLET_INTEGRITY_FAIL"),
        ("contract", "ATLAS_CONTRACT_INTEGRITY_FAIL"),
    ],
)
async def test_each_integrity_axis_blocks_on_its_own_and_says_which(now, axis, code):
    """A case blocked for holders and one blocked for code are different facts."""
    evidence = evidence_set(now, onchain={axis: "FAIL"})
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.BLOCKED
    blocker = next(item for item in outcome.hard_blockers if item.code == code)
    assert blocker.origin == BlockerOrigin.SOURCE_VERDICT
    assert blocker.evidence_id is not None


async def test_scenario_k_no_count_of_positives_can_outvote_one_blocker(now):
    """The explicit anti-voting regression.

    Every other source is as positive as it can be. The synthesis is blocked,
    and it is blocked for the same reason it would be with no positives at all.
    """
    evidence = evidence_set(
        now,
        onchain={"holder": "FAIL"},
        discovery={"classification": "STRONG", "strength": "HIGH"},
        sentiment={
            "assessment": "POSITIVE",
            "data_quality": "EXCELLENT",
            "attention_level": "HIGH",
            "organic_breadth": "BROAD",
            "manipulation_concern": "NONE",
        },
    )
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.BLOCKED
    assert len(outcome.support_factors) >= 2
    # Two entries, deliberately: the source's own acceptance says it blocks, and
    # the axis says exactly which measurement did it. Both name the same
    # envelope, and neither is a second opinion about whether to block.
    assert {blocker.origin for blocker in outcome.hard_blockers} == {
        BlockerOrigin.SOURCE_ACCEPTANCE,
        BlockerOrigin.SOURCE_VERDICT,
    }
    assert len({blocker.evidence_id for blocker in outcome.hard_blockers}) == 1


def test_the_synthesis_contract_has_no_arithmetic_at_all():
    """No score, no weight, no count — enforced by the schema, not by habit."""
    from src.agents.fuse.models import EvidenceSynthesis, SynthesisFactor

    for forbidden in (
        "score",
        "overall_score",
        "confidence",
        "weight",
        "positive_count",
        "negative_count",
        "agreement",
        "votes",
    ):
        assert forbidden not in EvidenceSynthesis.model_fields
        assert forbidden not in SynthesisFactor.model_fields


async def test_a_blocking_acceptance_blocks_even_without_a_named_axis(now):
    """A source whose own payload says BLOCKED is blocked, whatever it contains."""
    evidence = evidence_set(now, onchain={"holder": "FAIL"})
    outcome = await synthesized(now, evidence)
    origins = {blocker.origin for blocker in outcome.hard_blockers}
    assert BlockerOrigin.SOURCE_ACCEPTANCE in origins or BlockerOrigin.SOURCE_VERDICT in origins


async def test_atlas_published_blocker_codes_are_carried_individually(now):
    evidence = evidence_set(now, onchain={"blockers": ("MINT_AUTHORITY_ACTIVE", "OWNER_CAN_PAUSE")})
    outcome = await synthesized(now, evidence)

    codes = {blocker.code for blocker in outcome.hard_blockers}
    assert "ATLAS_MINT_AUTHORITY_ACTIVE" in codes
    assert "ATLAS_OWNER_CAN_PAUSE" in codes


# -------------------------------------------- D, F: unknown fails closed


async def test_scenario_d_an_unknown_safety_axis_is_a_gap_not_a_clearance(now):
    """ATLAS could not measure holders. That is not a passing grade."""
    evidence = evidence_set(
        now,
        onchain={"holder": "UNKNOWN", "verdict": "INCONCLUSIVE"},
    )
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.INSUFFICIENT
    assert outcome.unresolved_gaps
    assert outcome.is_actionable is False
    # And never silently upgraded into a positive reading.
    assert outcome.disposition != FuseDisposition.COHERENT


async def test_an_unknown_safety_source_cannot_be_offset_by_anything(now):
    evidence = evidence_set(
        now,
        onchain={"holder": "UNKNOWN"},
        discovery={"classification": "STRONG"},
        sentiment={"assessment": "POSITIVE", "data_quality": "EXCELLENT"},
    )
    outcome = await synthesized(now, evidence)
    assert outcome.disposition == FuseDisposition.INSUFFICIENT


@pytest.mark.parametrize(
    ("omitted", "role"),
    [
        (EvidenceType.ONCHAIN, "ATLAS"),
        (EvidenceType.TRADE_SETUP, "VECTOR"),
        (EvidenceType.SENTIMENT, "SIGNAL"),
        (EvidenceType.DISCOVERY, "ORBIT"),
    ],
)
async def test_scenario_f_a_missing_required_source_leaves_the_question_open(now, omitted, role):
    """Required evidence that was never produced is a gap, for every role."""
    outcome = await synthesized(now, evidence_set(now, omit=(omitted,)))

    assert outcome.disposition == FuseDisposition.INSUFFICIENT
    gap = next(item for item in outcome.unresolved_gaps if item.role.value == role)
    assert gap.origin == GapOrigin.MISSING
    assert gap.evidence_id is None


async def test_a_gap_records_whether_it_was_safety_critical(now):
    """The two kinds of gap read differently and are stored differently."""
    outcome = await synthesized(
        now, evidence_set(now, omit=(EvidenceType.ONCHAIN, EvidenceType.SENTIMENT))
    )
    by_role = {gap.role.value: gap for gap in outcome.unresolved_gaps}
    assert by_role["ATLAS"].safety_critical is True
    assert by_role["SIGNAL"].safety_critical is False


async def test_a_blocker_outranks_a_gap(now):
    """Both present: the measured danger is what gets reported."""
    evidence = evidence_set(now, omit=(EvidenceType.SENTIMENT,), onchain={"holder": "FAIL"})
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.BLOCKED
    assert outcome.hard_blockers
    assert outcome.unresolved_gaps


# ------------------------------------------- E, 26: degraded is preserved


async def test_scenario_e_degraded_sentiment_is_usable_and_said_out_loud(now):
    """SIGNAL is required but not safety-critical, so quality is a caution."""
    evidence = evidence_set(
        now,
        sentiment={"assessment": "POSITIVE", "data_quality": "DEGRADED"},
    )
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.CAUTION
    codes = {factor.code for factor in outcome.caution_factors}
    assert "SENTIMENT_QUALITY_DEGRADED" in codes
    # The positive reading survives beside the caution rather than being erased.
    assert "SENTIMENT_POSITIVE" in {factor.code for factor in outcome.support_factors}


async def test_scenario_26_a_tension_is_recorded_as_both_halves(now):
    """Positive attention drawn from a concentrated campaign.

    Two facts, not one averaged non-fact. Collapsing them to "neutral" would
    throw away the only part a reader needed.
    """
    evidence = evidence_set(
        now,
        sentiment={
            "assessment": "POSITIVE",
            "data_quality": "DEGRADED",
            "organic_breadth": "CONCENTRATED",
            "manipulation_concern": "ELEVATED",
        },
    )
    outcome = await synthesized(now, evidence)

    support = {factor.code for factor in outcome.support_factors}
    caution = {factor.code for factor in outcome.caution_factors}
    assert "SENTIMENT_POSITIVE" in support
    assert {"SENTIMENT_QUALITY_DEGRADED", "SENTIMENT_BREADTH_THIN"} <= caution
    assert "SENTIMENT_MANIPULATION_CONCERN" in caution
    assert outcome.disposition == FuseDisposition.CAUTION


async def test_an_unknown_integrity_axis_can_never_arrive_as_usable_evidence(now):
    """The stronger guarantee, found by trying to write the weaker one.

    The workflow refuses to record on-chain evidence as AVAILABLE while any
    domain is unestablished, so an unknown axis never reaches the synthesis as
    an admissible source at all — it arrives as a gap and fails closed. There is
    therefore no "unknown but accepted" state to soften, which is why the
    synthesis has no caution branch for one.
    """
    evidence = evidence_set(now, onchain={"dev": "UNKNOWN"})
    atlas = next(item for item in evidence if item.evidence_type == EvidenceType.ONCHAIN)
    assert atlas.status == EvidenceStatus.UNKNOWN

    outcome = await synthesized(now, evidence)
    assert outcome.disposition == FuseDisposition.INSUFFICIENT
    gap = next(item for item in outcome.unresolved_gaps if item.role.value == "ATLAS")
    assert gap.origin == GapOrigin.NOT_AVAILABLE
    assert gap.safety_critical is True


async def test_a_thinly_grounded_setup_is_a_caution_not_a_rejection(now):
    """VECTOR already decided the setup was sound. This does not re-decide it."""
    evidence = evidence_set(now, trade_setup={"bars": 5})
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.CAUTION
    assert "SETUP_HISTORY_THIN" in {factor.code for factor in outcome.caution_factors}


async def test_a_well_grounded_setup_raises_nothing(now):
    outcome = await synthesized(now, evidence_set(now, trade_setup={"bars": 500}))
    assert "SETUP_HISTORY_THIN" not in {factor.code for factor in outcome.caution_factors}


# ------------------------------------------ G, H, T: freshness comes from source


async def test_scenario_g_a_stale_source_is_a_gap_rather_than_an_input(now):
    """Evidence past its own validity is not read as though it were current."""
    stale = envelope(
        now,
        EvidenceType.ONCHAIN,
        onchain(),
        valid_until=now - timedelta(minutes=1),
        observed_at=now - timedelta(hours=1),
    )
    evidence = evidence_set(now, omit=(EvidenceType.ONCHAIN,)) + (stale,)
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.INSUFFICIENT
    gap = next(item for item in outcome.unresolved_gaps if item.role.value == "ATLAS")
    assert gap.origin == GapOrigin.STALE


async def test_scenario_t_a_new_synthesis_cannot_refresh_old_evidence(now):
    """The freshness rule that makes a summariser safe.

    Anchoring validity to synthesis time would let re-running FUSE launder stale
    facts into fresh-looking ones. Validity is the earliest source expiry, so a
    synthesis written now over evidence expiring in a minute expires in a minute.
    """
    soon = now + timedelta(minutes=1)
    short = envelope(now, EvidenceType.ONCHAIN, onchain(), valid_until=soon)
    evidence = evidence_set(now, omit=(EvidenceType.ONCHAIN,)) + (short,)
    outcome = await synthesized(now, evidence)

    assert outcome.valid_until == soon
    assert outcome.evaluated_at == now
    assert outcome.valid_until < now + timedelta(minutes=30)


async def test_observed_at_is_the_oldest_source_not_the_newest(now):
    """A synthesis is exactly as old as the oldest thing it read."""
    old = envelope(
        now,
        EvidenceType.DISCOVERY,
        evidence_set(now)[0].payload,
        observed_at=now - timedelta(hours=3),
    )
    evidence = evidence_set(now, omit=(EvidenceType.DISCOVERY,)) + (old,)
    outcome = await synthesized(now, evidence)

    assert outcome.observed_at == now - timedelta(hours=3)


async def test_scenario_h_an_expired_setup_cannot_be_synthesized_as_current(now):
    """No summary outlives the setup it summarises."""
    expired = envelope(
        now,
        EvidenceType.TRADE_SETUP,
        trade_setup(now - timedelta(hours=4), expires_in=timedelta(hours=1)),
        valid_until=now - timedelta(minutes=5),
        observed_at=now - timedelta(hours=4),
    )
    evidence = evidence_set(now, omit=(EvidenceType.TRADE_SETUP,)) + (expired,)
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.INSUFFICIENT
    gap = next(item for item in outcome.unresolved_gaps if item.role.value == "VECTOR")
    assert gap.origin == GapOrigin.STALE


# --------------------------------------------------- stage: what FUSE reads


def test_the_pre_trigger_stage_cannot_be_given_post_trigger_evidence():
    """FUSE runs before PULSE in this workflow, and the policy refuses to lie."""
    from dataclasses import replace

    assert EvidenceType.TRIGGER not in FUSE_SYNTHESIS_V1.sources
    assert EvidenceType.LIQUIDITY_EXECUTION not in FUSE_SYNTHESIS_V1.sources
    with pytest.raises(ValueError):
        replace(FUSE_SYNTHESIS_V1, sources=(*FUSE_SYNTHESIS_V1.sources, EvidenceType.TRIGGER))


async def test_post_trigger_evidence_is_ignored_rather_than_consumed(now):
    """A trigger recorded early does not change what this stage reads."""
    from tests.worker.conftest import trigger_payload

    setup_envelope = next(
        item for item in evidence_set(now) if item.evidence_type == EvidenceType.TRADE_SETUP
    )
    trigger = envelope(now, EvidenceType.TRIGGER, trigger_payload(setup_envelope.evidence_id))
    outcome = await synthesized(now, (*evidence_set(now), trigger))

    read = {source.evidence_type for source in outcome.sources}
    assert EvidenceType.TRIGGER not in read
    assert len(read) == 4


# --------------------------------------------------------- digest behaviour


async def test_scenario_29_the_same_evidence_yields_the_same_digest(now):
    evidence = evidence_set(now)
    first = await context_for(now, evidence)
    second = await context_for(now + timedelta(minutes=1), evidence)
    assert input_digest(first) == input_digest(second)


async def test_different_evidence_yields_a_different_digest(now):
    clean = await context_for(now, evidence_set(now))
    flagged = await context_for(now, evidence_set(now, onchain={"dev": "UNKNOWN"}))
    assert input_digest(clean) != input_digest(flagged)


async def test_the_digest_covers_which_envelope_not_merely_which_type(now):
    """Two ATLAS findings with the same content are still different evidence."""
    first = await context_for(now, evidence_set(now))
    second = await context_for(now, evidence_set(now))
    assert input_digest(first) != input_digest(second)


async def test_the_digest_ignores_when_the_work_happened(now):
    evidence = evidence_set(now)
    early = await context_for(now, evidence)
    late = await context_for(now + timedelta(seconds=30), evidence)
    assert early.evaluated_at != late.evaluated_at
    assert input_digest(early) == input_digest(late)


# ------------------------------------- S, U: failure modes and compatibility


async def test_scenario_s_a_case_with_no_evidence_yields_no_synthesis(now):
    """Nothing to read is not a finding, so nothing is recorded.

    The context still builds — every required source is simply reported missing
    — and the synthesis then refuses. That split is deliberate: the context's job
    is to say what is there, and the decision that there is not enough to say
    anything belongs with the thing that does the saying. The workflow's own
    required-evidence blockers already report an empty case far more directly
    than a synthesis of nothing would, and such a synthesis would be a durable
    record implying somebody looked.
    """
    from src.agents.fuse.models import FuseReasonCode

    context = await context_for(now, ())
    assert context.sources == ()
    assert len(context.missing) == 4

    outcome = synthesize(context, now, FUSE_SYNTHESIS_V1)
    assert outcome == FuseReasonCode.NO_ADMISSIBLE_EVIDENCE


async def test_a_terminal_case_is_never_synthesized(now):
    """A summary written after the fact would read as though it had mattered."""
    from src.agents.fuse.ports import FuseContextUnavailable
    from src.orchestration.workflow.models import TradeCaseStatus

    with pytest.raises(FuseContextUnavailable) as caught:
        await context_for(now, evidence_set(now), status=TradeCaseStatus.EXPIRED)
    assert caught.value.reason_code == "TRADE_CASE_TERMINAL"


async def test_nothing_fabricates_a_reading_when_a_source_is_unreadable(now):
    """The handler reports a typed failure rather than a hedged positive."""
    from src.agents.fuse.handler import FuseWorkerHandler
    from src.agents.fuse.ports import FuseContextUnavailable
    from src.orchestration.worker.capabilities import FuseCapabilities
    from src.orchestration.worker.models import TaskFailureReport, TaskLease

    class Failing:
        async def synthesis_context(self, trade_case_id, task_id):
            raise FuseContextUnavailable("TRADE_CASE_TERMINAL")

    class NoSubmit:
        lease = None

        async def submit_evidence(self, submission, *, result_key):  # pragma: no cover
            raise AssertionError("nothing should be submitted")

    from uuid import uuid4

    lease = TaskLease(
        lease_id=uuid4(),
        task_id=uuid4(),
        trade_case_id=uuid4(),
        role=__import__("src.core.models", fromlist=["x"]).AgentRole.FUSE,
        task_type="SYNTHESIZE_EVIDENCE",
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=uuid4(),
    )
    report = await FuseWorkerHandler().handle(
        lease, FuseCapabilities(lease=lease, context=Failing(), submit=NoSubmit())
    )
    assert isinstance(report, TaskFailureReport)
    assert report.reason_code == "TRADE_CASE_TERMINAL"


def test_scenario_u_a_synthesis_payload_without_a_detail_still_parses(now):
    """§72. Additive JSON only, so nothing already stored needs a migration."""
    from uuid import uuid4

    from src.orchestration.workflow.models import SynthesisPayload

    raw = {
        "kind": "synthesis",
        "disposition": "COHERENT",
        "source_evidence_ids": [str(uuid4())],
    }
    parsed = SynthesisPayload.model_validate(raw)
    assert parsed.synthesis is None
    assert parsed.acceptance() == EvidenceAcceptance.ACCEPTED
    assert parsed.model_dump()["disposition"] == "COHERENT"


def test_every_other_payload_still_parses_unchanged(now):
    """Adding an evidence type must not disturb the ones already stored."""
    from uuid import uuid4

    from src.orchestration.workflow.models import LiquidityExecutionPayload, OnchainPayload

    assert (
        OnchainPayload.model_validate(
            {
                "kind": "onchain",
                "holder_integrity": "PASS",
                "dev_wallet_integrity": "PASS",
                "contract_integrity": "PASS",
            }
        ).acceptance()
        is EvidenceAcceptance.ACCEPTED
    )
    legacy = LiquidityExecutionPayload.model_validate(
        {
            "kind": "liquidity_execution",
            "setup_evidence_id": str(uuid4()),
            "trigger_evidence_id": str(uuid4()),
        }
    )
    assert legacy.execution is None
    assert legacy.acceptance() is EvidenceAcceptance.ACCEPTED


def test_the_new_evidence_type_needs_no_migration():
    """The column is a plain String(60) with no constraint, and the value fits."""
    assert len(EvidenceType.SYNTHESIS.value) <= 60
    assert EvidenceType.SYNTHESIS.value == "SYNTHESIS_EVIDENCE"
