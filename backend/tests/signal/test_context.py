"""Assembly, sampling and the input digest.

Two properties matter here beyond correctness. The digest must identify the
*input* and nothing else, so re-reading an unchanged feed produces an identical
fingerprint while one new relevant post changes it. And the sample must be
reproducible and diversity-first, because a sample chosen by popularity would let
the loudest account decide what the model reads.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.agents.signal.context import (
    SignalContextReader,
    reasoning_payload,
    signal_document,
    signal_input_digest,
    token_address_of,
)
from src.agents.signal.fake import DeterministicSignalSource
from src.agents.signal.models import ObservationKind, SignalDataQuality
from src.agents.signal.policy import SIGNAL_QUALITY_V1
from src.core.clock import FixedClock
from tests.signal.conftest import (
    CHAIN,
    TOKEN,
    StubCases,
    StubTradeCase,
    bound,
    campaign_set,
    influencer_set,
    market_identity,
    organic_set,
    source_for,
    stale_set,
)


async def read(observations, now, *, cases=None, max_model=25, source=None):
    reader = SignalContextReader(
        cases=cases or StubCases(StubTradeCase(market_identity())),
        source=source or source_for(observations),
        max_model_observations=max_model,
        clock=FixedClock(now),
    )
    return await reader.sentiment_context(uuid4(), uuid4())


# -------------------------------------------------------------- the digest


async def test_the_same_posts_fingerprint_identically_however_often_they_are_read(now):
    """Re-fetching is not evidence that anything changed, and must not look like it."""
    observations = organic_set(now)
    first = await read(observations, now)
    later = await read(observations, now)
    assert signal_input_digest(first) == signal_input_digest(later)


async def test_one_new_relevant_post_changes_the_fingerprint(now):
    base = organic_set(now)
    extra = bound("extra", now=now, minutes_ago=5, author="newcomer", text="DEMO looks fine to me")
    assert signal_input_digest(await read(base, now)) != signal_input_digest(
        await read((*base, extra), now)
    )


async def test_excluded_posts_change_no_metric_of_the_set_that_was_analysed(now):
    """Dropped posts must not move a measurement, and must still be recorded.

    The fingerprint deliberately does change: how much a provider returned and
    how much of it was unusable is part of what happened, and the gap codes are
    derived from exactly those counts. What must not change is anything measured
    about the posts that were actually admitted.
    """
    base = await read(organic_set(now), now)
    with_noise = await read((*organic_set(now), *stale_set(now)), now)
    for field in ("observation_count", "unique_authoring_count", "duplicate_share"):
        assert getattr(base.features, field) == getattr(with_noise.features, field)
    assert with_noise.features.excluded_outside_window_count == 10
    assert signal_input_digest(base) != signal_input_digest(with_noise)


async def test_the_digest_covers_the_sample_the_model_was_actually_shown(now):
    wide = await read(organic_set(now), now, max_model=25)
    narrow = await read(organic_set(now), now, max_model=5)
    assert wide.features == narrow.features
    # Same metrics, different sample: the fingerprint must distinguish them.
    assert signal_input_digest(wide) != signal_input_digest(narrow)


async def test_no_post_text_reaches_the_fingerprinted_document(now):
    """Representatives are identified by hash, not by copying someone's writing."""
    task_input = await read(campaign_set(now), now)
    document = signal_document(task_input, 21600)
    rendered = repr(document)
    assert "Buy now before it moons" not in rendered
    assert task_input.representatives[0].content_hash in rendered


async def test_fetch_receipts_never_enter_the_document(now):
    task_input = await read(organic_set(now), now)
    document = repr(signal_document(task_input, 21600))
    for observation in organic_set(now)[:3]:
        assert observation.received_at.isoformat() not in document


# -------------------------------------------------------------- the sample


async def test_the_sample_is_identical_across_runs(now):
    first = await read(campaign_set(now), now)
    second = await read(campaign_set(now), now)
    assert [item.observation_id for item in first.representatives] == [
        item.observation_id for item in second.representatives
    ]
    assert [item.selection_reason for item in first.representatives] == [
        item.selection_reason for item in second.representatives
    ]


async def test_arrival_order_cannot_change_the_sample(now):
    forward = await read(organic_set(now), now, max_model=8)
    reversed_order = await read(tuple(reversed(organic_set(now))), now, max_model=8)
    assert {item.observation_id for item in forward.representatives} == {
        item.observation_id for item in reversed_order.representatives
    }


async def test_a_campaign_is_represented_by_its_text_once_not_fifty_times(now):
    task_input = await read(campaign_set(now), now, max_model=8)
    reasons = [item.selection_reason for item in task_input.representatives]
    assert reasons[0] == "DUPLICATE_CLUSTER"
    hashes = [item.content_hash for item in task_input.representatives]
    assert len(set(hashes)) == len(hashes)


async def test_the_sample_widens_across_authors_before_it_deepens(now):
    task_input = await read(organic_set(now), now, max_model=6)
    authors = [item.author_id for item in task_input.representatives]
    assert len(set(authors)) == len(authors)


async def test_engagement_never_decides_what_the_model_reads(now):
    """The influencer post is the loudest thing in the set by a wide margin.

    It is included because it is the only original, not because of its numbers,
    and the four replies that disagree with it are included alongside.
    """
    task_input = await read(influencer_set(now), now, max_model=5)
    kinds = {item.kind for item in task_input.representatives}
    assert ObservationKind.REPLY in kinds
    assert ObservationKind.REPOST not in kinds


async def test_a_share_is_never_offered_to_the_model_as_an_opinion(now):
    task_input = await read(influencer_set(now), now)
    assert all(item.kind != ObservationKind.REPOST for item in task_input.representatives)


async def test_the_sample_respects_its_bound(now):
    task_input = await read(organic_set(now), now, max_model=5)
    assert len(task_input.representatives) == 5
    assert len(task_input.excerpts) == 5


async def test_fifty_copies_of_one_sentence_yield_a_sample_of_one(now):
    """The sample is as large as there was something to read, and no larger."""
    copies = tuple(
        bound(f"copy-{index}", now=now, minutes_ago=10, author=f"a-{index}", text="DEMO 100x now")
        for index in range(50)
    )
    task_input = await read(copies, now, max_model=25)
    assert len(task_input.representatives) == 1
    assert task_input.features.observation_count == 50


# ------------------------------------------------------------- the payload


async def test_the_prompt_payload_quotes_data_and_carries_no_instructions(now):
    task_input = await read(organic_set(now), now)
    payload = reasoning_payload(task_input)
    assert set(payload) == {"social_observations"}
    document = payload["social_observations"]
    assert isinstance(document, dict)
    assert "structure" in document and "metrics" in document


async def test_excerpts_reach_the_model_and_not_the_digest(now):
    task_input = await read(organic_set(now), now)
    payload = reasoning_payload(task_input)
    document = payload["social_observations"]
    assert isinstance(document, dict)
    entries = document["representatives"]
    assert isinstance(entries, list)
    assert all("text" in entry for entry in entries)
    assert all(
        "text" not in entry for entry in signal_document(task_input, 21600)["representatives"]
    )  # type: ignore[union-attr]


# ------------------------------------------------------------ the collector


async def test_a_source_failure_is_an_explicit_absence_not_a_quiet_market(now):
    task_input = await read((), now, source=DeterministicSignalSource.unavailable())
    assert task_input.structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert task_input.features.received_count == 0


async def test_the_window_handed_to_the_source_comes_from_policy(now):
    source = source_for(organic_set(now))
    await read(organic_set(now), now, source=source)
    chain, pair_id, window = source.calls[0]
    assert chain == CHAIN
    assert window.end == now
    assert window.end - window.start == SIGNAL_QUALITY_V1.window
    assert pair_id


async def test_a_provider_returning_more_than_the_bound_is_truncated(now):
    flood = tuple(
        bound(f"flood-{index}", now=now, minutes_ago=10, author=f"a-{index}", text=f"DEMO {index}")
        for index in range(120)
    )
    reader = SignalContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        source=source_for(flood),
        max_observations=50,
        clock=FixedClock(now),
    )
    task_input = await reader.sentiment_context(uuid4(), uuid4())
    assert task_input.features.received_count == 50


@pytest.mark.parametrize(
    ("asset_id", "expected"),
    [
        (f"robinhood:mainnet:{TOKEN}", TOKEN),
        ("robinhood:mainnet:0x" + "A1" * 20, TOKEN),
        ("robinhood:mainnet:0x" + "0" * 40, None),
        ("robinhood:mainnet:notanaddress", None),
        ("robinhood:mainnet:0x" + "ab" * 32, None),
    ],
)
def test_only_a_real_token_address_is_accepted_as_one(asset_id, expected):
    assert token_address_of(asset_id) == expected


async def test_the_evidence_it_would_supersede_is_read_and_nothing_else(now, market):
    """Only SIGNAL's own slot is read; no other role's findings reach the worker."""
    cases = StubCases(StubTradeCase(market))
    task_input = await read(organic_set(now), now, cases=cases)
    assert task_input.supersedes_evidence_id is None
    assert task_input.evaluated_at == now
    assert task_input.features.window.end - task_input.features.window.start == timedelta(hours=6)
