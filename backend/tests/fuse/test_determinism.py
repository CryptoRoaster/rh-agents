"""The synthesis is a function of its inputs, and of nothing else.

Everything here would be untestable if the synthesis were a model call, and all
of it is cheap because it is not. A derived view that changed between two reads
of the same evidence would be impossible to audit: nobody could say whether a
difference in two stored syntheses meant the case had changed or merely that it
had been looked at twice.
"""

import itertools
from datetime import timedelta

import pytest

from src.agents.fuse.models import FuseDisposition, GapOrigin
from src.agents.fuse.policy import FUSE_SYNTHESIS_V1
from src.agents.fuse.synthesis import input_digest, synthesize
from src.orchestration.workflow.models import EvidenceType
from tests.fuse.conftest import context_for, evidence_set


async def synthesized(now, evidence):
    return synthesize(await context_for(now, evidence), now, FUSE_SYNTHESIS_V1)


# --------------------------------------- 6, 27: order cannot reach the output


async def test_scenario_6_query_order_cannot_change_the_digest(now):
    """Twenty-four orderings of four sources, one digest.

    A database returns rows in whatever order it likes, and nothing that order
    decides may reach durable evidence.
    """
    evidence = evidence_set(now)
    digests = set()
    for permutation in itertools.permutations(evidence):
        digests.add(input_digest(await context_for(now, permutation)))
    assert len(digests) == 1


async def test_scenario_27_query_order_cannot_change_the_synthesis(now):
    """Not only the digest: the whole derived output is order-independent."""
    evidence = evidence_set(now, onchain={"holder": "FAIL"}, sentiment={"data_quality": "DEGRADED"})
    rendered = set()
    for permutation in itertools.permutations(evidence):
        outcome = synthesize(await context_for(now, permutation), now, FUSE_SYNTHESIS_V1)
        rendered.add(outcome.model_dump_json())
    assert len(rendered) == 1


async def test_the_source_list_is_in_canonical_policy_order(now):
    """Stable and meaningful, rather than whatever arrived first."""
    for permutation in itertools.permutations(evidence_set(now)):
        outcome = synthesize(await context_for(now, permutation), now, FUSE_SYNTHESIS_V1)
        assert [source.evidence_type for source in outcome.sources] == list(
            FUSE_SYNTHESIS_V1.sources
        )


async def test_findings_are_ordered_stably_across_permutations(now):
    """§28. No output churn from set iteration."""
    evidence = evidence_set(
        now,
        onchain={"holder": "FAIL", "blockers": ("MINT_AUTHORITY_ACTIVE", "OWNER_CAN_PAUSE")},
        sentiment={"data_quality": "DEGRADED", "organic_breadth": "CONCENTRATED"},
    )
    orders = set()
    for permutation in itertools.permutations(evidence):
        outcome = synthesize(await context_for(now, permutation), now, FUSE_SYNTHESIS_V1)
        orders.add(tuple(item.code for item in outcome.hard_blockers))
        orders.add(tuple(item.code for item in outcome.caution_factors))
    # Two stable tuples — one per list — not one per permutation.
    assert len(orders) == 2


async def test_scenario_28_no_factor_is_reported_twice(now):
    """One observation per thing observed, however many fields expose it."""
    evidence = evidence_set(
        now,
        onchain={"holder": "FAIL", "blockers": ("HOLDER_CONCENTRATION",)},
        sentiment={
            "assessment": "POSITIVE",
            "data_quality": "DEGRADED",
            "organic_breadth": "CONCENTRATED",
            "manipulation_concern": "ELEVATED",
            "gaps": ("WINDOW_TRUNCATED",),
        },
    )
    outcome = await synthesized(now, evidence)
    for group in (
        outcome.hard_blockers,
        outcome.unresolved_gaps,
        outcome.support_factors,
        outcome.caution_factors,
    ):
        identities = [(item.code, item.evidence_id) for item in group]
        assert len(identities) == len(set(identities))


async def test_the_same_evidence_twice_gives_the_same_fingerprint(now):
    """Replay is a replay, never a second opinion."""
    from src.agents.fuse.handler import synthesis_fingerprint

    evidence = evidence_set(now)
    first_context = await context_for(now, evidence)
    second_context = await context_for(now + timedelta(seconds=42), evidence)
    first = synthesize(first_context, now, FUSE_SYNTHESIS_V1)
    second = synthesize(second_context, now + timedelta(seconds=42), FUSE_SYNTHESIS_V1)

    assert synthesis_fingerprint(
        first_context, first, FUSE_SYNTHESIS_V1.version
    ) == synthesis_fingerprint(second_context, second, FUSE_SYNTHESIS_V1.version)


# --------------------------------- 12, 13, 14: blockers beside gaps


async def test_scenario_13_a_blocked_case_still_names_what_is_missing(now):
    """Both facts are true, so both are recorded.

    A consumer that only learned "blocked" would not know a source was also
    absent; one that only learned "insufficient" might conclude no definitive
    problem had been found. Neither reading is available here.
    """
    evidence = evidence_set(now, omit=(EvidenceType.SENTIMENT,), onchain={"holder": "FAIL"})
    outcome = await synthesized(now, evidence)

    assert outcome.disposition == FuseDisposition.BLOCKED
    assert [item.code for item in outcome.hard_blockers]
    gap = next(item for item in outcome.unresolved_gaps if item.role.value == "SIGNAL")
    assert gap.origin == GapOrigin.MISSING


async def test_scenario_14_a_blocker_is_never_hidden_behind_a_gap(now):
    """§14. `hard_blockers != []` is answerable without reading prose.

    A future COMMANDER tests a list, not a disposition string and not a
    sentence. The structured output makes a known blocker explicit whatever the
    disposition happens to be.
    """
    evidence = evidence_set(
        now,
        omit=(EvidenceType.SENTIMENT, EvidenceType.DISCOVERY),
        onchain={"holder": "FAIL"},
    )
    outcome = await synthesized(now, evidence)

    assert outcome.hard_blockers != ()
    assert len(outcome.unresolved_gaps) == 2
    assert outcome.disposition == FuseDisposition.BLOCKED
    # Every blocker names its source, so nothing has to be inferred from text.
    for blocker in outcome.hard_blockers:
        assert blocker.evidence_id is not None
        assert blocker.role.value == "ATLAS"


async def test_precedence_is_blockers_then_gaps_then_caution(now):
    """Stated as a table so the choice is deliberate rather than incidental."""
    cases = [
        ({"onchain": {"holder": "FAIL"}}, {}, FuseDisposition.BLOCKED),
        ({}, {"omit": (EvidenceType.SENTIMENT,)}, FuseDisposition.INSUFFICIENT),
        ({"sentiment": {"data_quality": "DEGRADED"}}, {}, FuseDisposition.CAUTION),
        ({}, {}, FuseDisposition.COHERENT),
    ]
    for overrides, kwargs, expected in cases:
        outcome = await synthesized(now, evidence_set(now, **kwargs, **overrides))
        assert outcome.disposition == expected, (overrides, kwargs)


async def test_scenario_15_a_degraded_signal_never_becomes_a_blocker(now):
    """SIGNAL is required but not safety-critical, and stays that way."""
    evidence = evidence_set(
        now,
        sentiment={
            "assessment": "NEGATIVE",
            "data_quality": "POOR",
            "manipulation_concern": "SEVERE",
            "organic_breadth": "SINGLE_SOURCE",
        },
    )
    outcome = await synthesized(now, evidence)

    assert outcome.hard_blockers == ()
    assert outcome.disposition == FuseDisposition.CAUTION
    assert len(outcome.caution_factors) >= 3


# ------------------------------------------- 24, 25: the input set is closed


async def test_scenario_24_two_equally_current_sources_of_one_type_fail_closed(now):
    """A worker is never asked to pick between two current envelopes.

    Two live envelopes of one type mean the stored history is inconsistent.
    Resolving that by arrival order would make the synthesis depend on the
    database's mood, so it fails as an integrity error instead.
    """
    from src.orchestration.workflow.models import WorkflowFailure

    doubled = (
        *evidence_set(now),
        *evidence_set(
            now,
            omit=(
                EvidenceType.DISCOVERY,
                EvidenceType.SENTIMENT,
                EvidenceType.TRADE_SETUP,
            ),
        ),
    )
    with pytest.raises(WorkflowFailure):
        await context_for(now, doubled)


async def test_scenario_25_an_unexpected_evidence_type_is_never_absorbed(now):
    """A future evidence type does not silently join a v1 synthesis."""
    from tests.fuse.conftest import envelope
    from tests.worker.conftest import trigger_payload

    setup = next(
        item for item in evidence_set(now) if item.evidence_type == EvidenceType.TRADE_SETUP
    )
    extra = envelope(now, EvidenceType.TRIGGER, trigger_payload(setup.evidence_id))
    context = await context_for(now, (*evidence_set(now), extra))

    read = {source.reference.evidence_type for source in context.sources}
    assert read == set(FUSE_SYNTHESIS_V1.sources)
    assert EvidenceType.TRIGGER not in read
    assert context.policy_version == "fuse-synthesis-v1"
