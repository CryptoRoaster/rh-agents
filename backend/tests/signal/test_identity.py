"""Identity: who counts as one person, and who is allowed to say so.

Two questions that look like plumbing and are not. If two platforms hand the same
identifier string to different people and we merge them, the crowd shrinks and
its concentration rises — both errors point at the same wrong conclusion, that an
honest conversation was a campaign. And if an adapter may assert that an account
speaks for the project, then whoever controls the adapter controls the strongest
binding in the system.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.signal.models import (
    STRONG_BINDING_BASES,
    MarketBindingBasis,
    ObservationKind,
    SignalSource,
    SignalWindow,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1
from src.agents.signal.quality import (
    SignalNormalizationError,
    admit,
    compute_features,
    content_hash,
)
from src.core.numbers import quantize
from tests.signal.conftest import CHAIN, TOKEN, bound, organic_set
from tests.signal.test_quality import features_for, window_for


def cross_source(now, *, author: str, source: SignalSource, label: str, text: str):
    return bound(label, now=now, minutes_ago=10, author=author, text=text, source=source)


def admit_all(observations, now, *, verified=frozenset()):
    return admit(
        observations,
        window=window_for(now),
        chain=CHAIN,
        token_address=TOKEN,
        admissible_bases=SIGNAL_QUALITY_V1.admissible_bases,
        verified_project_authors=verified,
    )


# ------------------------------------------------------ author namespacing


def test_the_same_handle_on_two_platforms_is_two_people(now):
    """User 123 on X and user 123 on Reddit have never met.

    Merging them understates how many voices there are and overstates how
    concentrated they are, which is the exact pair of errors that turns a real
    cross-platform conversation into a measured campaign.
    """
    rows = (
        cross_source(now, author="123", source=SignalSource.X, label="x1", text="DEMO one"),
        cross_source(now, author="123", source=SignalSource.REDDIT, label="r1", text="DEMO two"),
        cross_source(now, author="456", source=SignalSource.X, label="x2", text="DEMO three"),
    )
    features = features_for(rows, now)
    assert features.observation_count == 3
    assert features.unique_author_count == 3
    assert features.unique_authoring_count == 3
    # One third each, not the two thirds that merging a shared string produces.
    assert features.top1_author_share == quantize(Decimal(1) / Decimal(3))


def test_the_same_handle_on_the_same_platform_is_one_person(now):
    rows = tuple(
        cross_source(
            now, author="123", source=SignalSource.X, label=f"x{index}", text=f"DEMO {index}"
        )
        for index in range(4)
    )
    features = features_for(rows, now)
    assert features.observation_count == 4
    assert features.unique_authoring_count == 1
    assert features.top1_author_share == 1


def test_a_display_handle_is_never_the_identity_on_its_own(now):
    """Handles collide across platforms and change over time, so they are scoped."""
    x_alice = cross_source(now, author="alice", source=SignalSource.X, label="a", text="DEMO a")
    reddit_alice = cross_source(
        now, author="alice", source=SignalSource.FARCASTER, label="b", text="DEMO b"
    )
    assert x_alice.author_id == reddit_alice.author_id
    assert x_alice.author_key != reddit_alice.author_key
    assert features_for((x_alice, reddit_alice), now).unique_authoring_count == 2


def test_a_native_post_id_is_namespaced_by_its_source(now):
    """Post 42 exists on every platform, and means something different on each."""
    first = cross_source(now, author="a", source=SignalSource.X, label="42", text="DEMO one")
    second = cross_source(now, author="b", source=SignalSource.REDDIT, label="42", text="DEMO two")
    assert first.source_native_id == second.source_native_id
    assert first.native_key != second.native_key
    assert features_for((first, second), now).observation_count == 2


def test_a_repeated_observation_identifier_is_refused(now):
    """One post counted twice is one voice counted twice."""
    single = bound("dup", now=now, minutes_ago=10, author="a", text="DEMO once")
    with pytest.raises(SignalNormalizationError) as error:
        admit_all((single, single), now)
    assert error.value.reason_code == "DUPLICATE_OBSERVATION_ID"


def test_per_source_breakdown_counts_authors_within_its_own_namespace(now):
    rows = (
        cross_source(now, author="1", source=SignalSource.X, label="x1", text="DEMO one"),
        cross_source(now, author="1", source=SignalSource.REDDIT, label="r1", text="DEMO two"),
    )
    features = features_for(rows, now)
    assert {entry.source for entry in features.sources} == {SignalSource.X, SignalSource.REDDIT}
    assert all(entry.unique_author_count == 1 for entry in features.sources)


def test_the_sample_diversifies_on_the_namespaced_identity(now):
    """Two platforms sharing a handle must both get a slot, not one between them."""
    from src.agents.signal.context import sample

    rows = (
        cross_source(now, author="1", source=SignalSource.X, label="x1", text="DEMO one"),
        cross_source(now, author="1", source=SignalSource.REDDIT, label="r1", text="DEMO two"),
    )
    features = features_for(rows, now)
    chosen = sample(admit_all(rows, now).admitted, features, 10)
    assert len(chosen) == 2
    assert len({item.author_key for item in chosen}) == 2


# --------------------------------------- content similarity is not identity


def test_the_same_text_on_two_platforms_is_one_campaign_and_two_authors(now):
    """Cross-source duplication is a real finding. It is not a merged identity.

    A campaign paying accounts on several platforms should show up as one cluster
    of copied text — and still as the several distinct accounts that posted it.
    """
    text = "🚀 DEMO is the next 100x, do not miss it!"
    rows = (
        cross_source(now, author="1", source=SignalSource.X, label="x1", text=text),
        cross_source(now, author="1", source=SignalSource.REDDIT, label="r1", text=text),
        cross_source(now, author="2", source=SignalSource.FARCASTER, label="f1", text=text),
    )
    features = features_for(rows, now)
    assert len(features.duplicate_clusters) == 1
    cluster = features.duplicate_clusters[0]
    assert cluster.content_hash == content_hash(text)
    assert cluster.observation_count == 3
    # Three namespaced identities behind one piece of text.
    assert cluster.author_count == 3
    assert features.unique_authoring_count == 3


# -------------------------------------------- the verified-project boundary


def test_an_adapter_cannot_award_itself_the_project_voice(now):
    """A provider label is not verification, and neither is a convincing URL."""
    claims = tuple(
        bound(
            f"vp-{index}",
            now=now,
            minutes_ago=10 + index,
            author=f"a{index}",
            text=f"DEMO {index}",
        ).model_copy(
            update={
                "binding_basis": MarketBindingBasis.VERIFIED_PROJECT_LINK,
                "binding_address": None,
                "binding_chain": None,
            }
        )
        for index in range(6)
    )
    admission = admit_all(claims, now)
    assert admission.admitted == ()
    assert admission.unbound == 6


def test_a_trusted_project_identity_is_what_makes_the_binding_strong(now):
    """The path exists and is structurally supported; only its source is gated."""
    claims = tuple(
        bound(
            f"vp-{index}",
            now=now,
            minutes_ago=10 + index,
            author=f"a{index}",
            text=f"DEMO {index}",
        ).model_copy(
            update={
                "binding_basis": MarketBindingBasis.VERIFIED_PROJECT_LINK,
                "binding_address": None,
                "binding_chain": None,
            }
        )
        for index in range(6)
    )
    trusted = frozenset(item.author_key for item in claims)
    admission = admit_all(claims, now, verified=trusted)
    features = compute_features(
        admission,
        window=window_for(now),
        burst_interval=SIGNAL_QUALITY_V1.burst_interval,
    )
    assert features.observation_count == 6
    assert features.strong_binding_count == 6
    assert MarketBindingBasis.VERIFIED_PROJECT_LINK in STRONG_BINDING_BASES


def test_a_trusted_identity_on_one_platform_does_not_transfer_to_another(now):
    """The registry is namespaced too, or it would be a handle-squatting hole."""
    claim = bound("vp", now=now, minutes_ago=10, author="team", text="DEMO update").model_copy(
        update={
            "source": SignalSource.FARCASTER,
            "binding_basis": MarketBindingBasis.VERIFIED_PROJECT_LINK,
            "binding_address": None,
            "binding_chain": None,
        }
    )
    assert admit_all((claim,), now, verified=frozenset({"X:team"})).admitted == ()
    assert len(admit_all((claim,), now, verified=frozenset({"FARCASTER:team"})).admitted) == 1


def test_the_collector_ships_with_no_trusted_project_identities(now):
    """No registry exists in this repository, so nothing can claim the strong path."""
    from src.agents.signal.context import SignalContextReader

    reader = SignalContextReader.__dataclass_fields__["verified_project_authors"]
    assert reader.default == frozenset()


# ------------------------------------ the model cannot promote a binding


async def test_a_model_cannot_turn_an_ambiguous_post_into_a_bound_one(now):
    """Binding is input provenance, decided before the model is ever called.

    There is no field on the assessment through which a model could claim an
    observation is "definitely this token", and the ambiguous posts never reach
    it in the first place.
    """
    from src.agents.signal.models import SignalAssessment
    from tests.signal.conftest import collision_set
    from tests.signal.test_context import read

    task_input = await read(collision_set(now), now)
    assert task_input.features.observation_count == 0
    assert task_input.representatives == ()
    for forbidden in ("binding_basis", "binding", "token_address", "observations"):
        assert forbidden not in SignalAssessment.model_fields


async def test_a_confident_model_cannot_cite_an_excluded_ambiguous_post(now):
    from uuid import uuid4

    from src.agents.signal.validation import SignalValidationError, validate_assessment
    from tests.signal.conftest import collision_set
    from tests.signal.test_context import read
    from tests.signal.test_validation import assessment

    task_input = await read((*organic_set(now), *collision_set(now)), now)
    excluded = collision_set(now)[0].observation_id
    assert excluded not in task_input.representative_ids
    with pytest.raises(SignalValidationError):
        validate_assessment(assessment(cited_observation_ids=(excluded,)), task_input)
    assert uuid4() not in task_input.representative_ids


# ------------------------------------------- weak bindings stay visible


async def test_weak_bindings_stay_weak_all_the_way_to_the_model(now):
    from tests.signal.test_context import read

    weak = tuple(
        item.model_copy(
            update={
                "binding_basis": MarketBindingBasis.UNIQUE_SYMBOL_WITH_CONTEXT,
                "binding_address": None,
                "binding_chain": None,
            }
        )
        for item in organic_set(now)
    )
    task_input = await read(weak, now)
    assert task_input.features.strong_binding_count == 0
    assert task_input.features.weak_binding_count == 26
    assert all(
        item.binding_basis == MarketBindingBasis.UNIQUE_SYMBOL_WITH_CONTEXT
        for item in task_input.representatives
    )


def test_a_repost_still_counts_towards_attention_while_authoring_nothing(now):
    """The distinction the repost fix must not have destroyed.

    Amplification is real activity and stays in the attention count. What it is
    not is a second opinion, so it contributes no authored breadth. Whether an
    amplifying account is itself worth modelling — a coordinated retweet ring, say
    — is a metric this phase does not have, and does not pretend to.
    """
    original = bound("orig", now=now, minutes_ago=30, author="writer", text="DEMO is interesting")
    shares = tuple(
        bound(
            f"share-{index}",
            now=now,
            minutes_ago=29 - index,
            author=f"amplifier-{index}",
            text="DEMO is interesting",
            kind=ObservationKind.REPOST,
            referenced=original.observation_id,
        )
        for index in range(9)
    )
    features = features_for((original, *shares), now)
    assert features.observation_count == 10
    assert features.repost_count == 9
    assert features.unique_author_count == 10
    assert features.unique_authoring_count == 1
    assert features.top1_author_share == 1


def test_the_window_is_the_only_thing_source_time_is_judged_against(now):
    edge = bound("edge", now=now, minutes_ago=1, author="a", text="DEMO now")
    window = SignalWindow(start=now - timedelta(minutes=30), end=now)
    assert window.contains(edge.created_at)
    assert edge.received_at > edge.created_at
