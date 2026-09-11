"""The deterministic layer: counting, deduplication and market binding.

These tests are the load-bearing ones. Everything SIGNAL claims about breadth and
manipulation rests on this arithmetic, and unlike the model's reading, arithmetic
is supposed to be exactly right.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.signal.models import (
    MarketBindingBasis,
    ObservationKind,
    SignalWindow,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1
from src.agents.signal.quality import (
    CONTENT_HASH_ALGORITHM,
    admit,
    compute_features,
    content_hash,
    normalize_content,
)
from tests.signal.conftest import (
    CAMPAIGN_TEXT,
    CHAIN,
    TOKEN,
    bound,
    campaign_set,
    collision_set,
    influencer_set,
    organic_set,
    stale_set,
    wrong_chain_set,
)

BASES = SIGNAL_QUALITY_V1.admissible_bases


def window_for(now):
    return SignalWindow(start=now - SIGNAL_QUALITY_V1.window, end=now)


def features_for(observations, now, *, chain=CHAIN, token=TOKEN):
    admission = admit(
        observations,
        window=window_for(now),
        chain=chain,
        token_address=token,
        admissible_bases=BASES,
    )
    return compute_features(
        admission, window=window_for(now), burst_interval=SIGNAL_QUALITY_V1.burst_interval
    )


# ------------------------------------------------------------- normalization


def test_a_unique_tracking_link_does_not_make_a_copy_a_new_opinion():
    """The cheapest way to defeat deduplication, and the one campaigns use."""
    first = f"{CAMPAIGN_TEXT} https://promo.example/1"
    second = f"{CAMPAIGN_TEXT} https://promo.example/99999"
    assert content_hash(first) == content_hash(second)


def test_case_and_whitespace_variants_hash_together():
    assert content_hash("Buy   DEMO now") == content_hash("buy demo now")


def test_invisible_unicode_variants_hash_together():
    """Compatibility forms look identical to a reader and differ byte for byte."""
    assert content_hash("ＤＥＭＯ is great") == content_hash("DEMO is great")


def test_normalization_never_destroys_the_address_that_identifies_the_asset():
    """A link may go. The contract address is often the only real identifier."""
    normalized = normalize_content(f"Check {TOKEN} at https://scan.example/{TOKEN}")
    assert TOKEN in normalized


def test_genuinely_different_posts_do_not_collide():
    assert content_hash("DEMO fees are low") != content_hash("DEMO fees are high")


def test_the_hash_algorithm_is_versioned_on_the_record(now):
    assert features_for(organic_set(now), now).content_hash_algorithm == CONTENT_HASH_ALGORITHM


# ------------------------------------------------------------------ binding


def test_an_exact_address_on_the_right_chain_binds_strongly(now):
    features = features_for(organic_set(now), now)
    assert features.strong_binding_count == features.observation_count
    assert features.weak_binding_count == 0


def test_the_same_address_on_another_chain_is_another_contract(now):
    """A hex string is not an identity across chains, and is refused as one."""
    features = features_for(wrong_chain_set(now), now)
    assert features.observation_count == 0
    assert features.excluded_unbound_count == 10


def test_an_address_that_is_not_this_token_is_refused(now):
    features = features_for(organic_set(now), now, token="0x" + "99" * 20)
    assert features.observation_count == 0
    assert features.excluded_unbound_count == 26


def test_a_bare_ticker_never_carries_sentiment_into_this_case(now):
    """Symbol collisions are the reason a popular name cannot bind a market."""
    features = features_for(collision_set(now), now)
    assert features.observation_count == 0
    assert features.excluded_ambiguous_count == 10


def test_a_symbol_with_context_is_admitted_but_counted_as_the_weaker_binding(now):
    contextual = tuple(
        item.model_copy(
            update={
                "binding_basis": MarketBindingBasis.UNIQUE_SYMBOL_WITH_CONTEXT,
                "binding_address": None,
                "binding_chain": None,
            }
        )
        for item in organic_set(now)
    )
    features = features_for(contextual, now)
    assert features.observation_count == 26
    assert features.strong_binding_count == 0
    assert features.weak_binding_count == 26


def test_a_market_without_a_usable_address_cannot_reach_a_strong_binding(now):
    features = features_for(organic_set(now), now, token=None)
    assert features.observation_count == 0


# ------------------------------------------------------------------- window


def test_the_window_is_judged_on_source_time_not_on_when_we_fetched(now):
    """Every fixture was received after it was written; only writing time counts."""
    stale = stale_set(now)
    assert all(item.received_at > item.created_at for item in stale)
    features = features_for(stale, now)
    assert features.observation_count == 0
    assert features.excluded_outside_window_count == 10


def test_a_post_on_the_window_boundary_is_inside_it(now):
    edge = bound(
        "edge",
        now=now,
        minutes_ago=int(SIGNAL_QUALITY_V1.window.total_seconds() // 60),
        author="edge-author",
        text="DEMO at the boundary",
    )
    assert features_for((edge,), now).observation_count == 1


def test_a_post_from_the_future_is_outside_the_window(now):
    ahead = bound("ahead", now=now, minutes_ago=-5, author="ahead", text="DEMO later")
    assert features_for((ahead,), now).observation_count == 0


# ------------------------------------------------------- counting and shares


def test_one_account_posting_three_hundred_times_is_still_one_author(now):
    flood = tuple(
        bound(f"flood-{index}", now=now, minutes_ago=10, author="one", text=f"DEMO note {index}")
        for index in range(300)
    )
    features = features_for(flood, now)
    assert features.observation_count == 300
    assert features.unique_authoring_count == 1
    assert features.top1_author_share == Decimal(1)


def test_reposts_are_attention_and_never_authored_positions(now):
    features = features_for(influencer_set(now), now)
    assert features.observation_count == 50
    assert features.repost_count == 45
    # Fifty participants, five of whom actually wrote something.
    assert features.unique_author_count == 50
    assert features.unique_authoring_count == 5


def test_a_copy_campaign_collapses_into_a_single_cluster(now):
    features = features_for(campaign_set(now), now)
    assert features.original_count == 55
    assert features.duplicate_clusters[0].observation_count == 50
    assert features.largest_duplicate_cluster_share > Decimal("0.9")
    assert features.unique_content_count == 6


def test_a_real_conversation_has_no_duplicate_clusters(now):
    features = features_for(organic_set(now), now)
    assert features.duplicate_clusters == ()
    assert features.duplicate_share == Decimal(0)
    assert features.unique_content_count == features.original_count


def test_a_cluster_posted_inside_one_interval_registers_as_a_burst(now):
    features = features_for(campaign_set(now), now)
    assert features.burst_share > Decimal("0.5")


def test_the_same_text_spread_over_hours_is_not_a_burst(now):
    spread = tuple(
        bound(
            f"spread-{index}",
            now=now,
            minutes_ago=10 + index * 45,
            author=f"author-{index}",
            text="DEMO keeps shipping, worth a look.",
        )
        for index in range(5)
    )
    features = features_for(spread, now)
    assert features.duplicate_share == Decimal(1)
    assert features.burst_share == Decimal(0)


def test_shares_are_exact_decimals_and_never_floats(now):
    features = features_for(campaign_set(now), now)
    for share in (
        features.duplicate_share,
        features.largest_duplicate_cluster_share,
        features.top1_author_share,
        features.top5_author_share,
        features.burst_share,
    ):
        assert isinstance(share, Decimal)


@pytest.mark.parametrize("builder", [organic_set, campaign_set, influencer_set])
def test_source_provenance_survives_normalization(now, builder):
    features = features_for(builder(now), now)
    assert features.sources
    assert sum(entry.observation_count for entry in features.sources) == features.observation_count


def test_the_newest_admissible_post_anchors_freshness(now):
    features = features_for(organic_set(now), now)
    assert features.latest_observation_at == now - timedelta(minutes=10)
    assert features.oldest_observation_at is not None
    assert features.oldest_observation_at < features.latest_observation_at


def test_an_empty_answer_yields_empty_counts_rather_than_invented_ones(now):
    features = features_for((), now)
    assert features.observation_count == 0
    assert features.latest_observation_at is None
    assert features.top1_author_share == Decimal(0)


def test_replies_are_authored_content_and_count_towards_breadth(now):
    replies = tuple(
        bound(
            f"reply-{index}",
            now=now,
            minutes_ago=10 + index,
            author=f"replier-{index}",
            text=f"Answering the DEMO thread, point {index}",
            kind=ObservationKind.REPLY,
            referenced=organic_set(now)[0].observation_id,
        )
        for index in range(4)
    )
    features = features_for(replies, now)
    assert features.reply_count == 4
    assert features.unique_authoring_count == 4
