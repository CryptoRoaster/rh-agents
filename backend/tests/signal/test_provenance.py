"""Where each fact came from, and what it is therefore entitled to claim.

Four provenance questions, each with a way of going quietly wrong:

* a post's age must come from when it was written, never from when we fetched it;
* the asset a search is about must come from the TradeCase's canonical base
  asset, never from a pool side chosen by convention;
* a matching address on the wrong chain must not become a match;
* and a result set we stopped reading must not be reported as one that ended.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.agents.signal.context import SignalContextReader, signal_input_digest, token_address_of
from src.agents.signal.models import (
    CollectionCoverage,
    MarketBindingBasis,
    SignalDataQuality,
    SignalGap,
    SignalWindow,
)
from src.agents.signal.sources.neynar import NeynarSignalSource
from src.core.clock import FixedClock
from src.markets.models import MarketIdentity
from tests.signal.conftest import CHAIN, QUOTE, TOKEN, StubCases, StubTradeCase, market_identity
from tests.signal.fake_http import RecordingRoutes, json_response, pages
from tests.signal.test_neynar import CONFIG, cast, page

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


async def read(handler, *, now=NOW, market=None, max_observations=500):
    recording = RecordingRoutes(handler)
    reader = SignalContextReader(
        cases=StubCases(StubTradeCase(market or market_identity())),
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=recording.transport_factory(), clock=FixedClock(now)
        ),
        max_observations=max_observations,
        clock=FixedClock(now),
    )
    return recording, await reader.sentiment_context(uuid4(), uuid4())


def bound_cast(index: int, *, minutes_ago: int, fid: int = 5000) -> dict[str, object]:
    return cast(
        index,
        text=f"Robinhood Chain {TOKEN} — note {index}",
        fid=fid,
        minutes_ago=minutes_ago,
    )


# ------------------------------------------------- source time vs fetch time


async def test_a_cast_written_an_hour_ago_is_an_hour_old(now=NOW):
    """Scenario A. The provider's timestamp is the age, and our clock is not."""
    _, task_input = await read(
        pages(page([bound_cast(index, minutes_ago=60) for index in range(6)]))
    )
    latest = task_input.features.latest_observation_at
    assert latest == NOW - timedelta(hours=1)
    assert task_input.evaluated_at == NOW


async def test_a_cast_outside_the_window_is_excluded_however_recently_fetched():
    """Scenario B. Five hours of collection cannot move a nine-hour-old post."""
    stale = [bound_cast(index, minutes_ago=9 * 60) for index in range(6)]
    _, task_input = await read(pages(page(stale)))
    assert task_input.features.observation_count == 0
    assert task_input.features.excluded_outside_window_count == 6
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW in task_input.structure.gaps


async def test_fetching_the_same_cast_later_does_not_refresh_it():
    """Scenario C. The source timestamp is a property of the post, not the read."""
    casts = [bound_cast(index, minutes_ago=60) for index in range(6)]

    async def normalized(at: datetime):
        recording = RecordingRoutes(pages(page(casts)))
        collected = await NeynarSignalSource(
            config=CONFIG, transport_factory=recording.transport_factory(), clock=FixedClock(at)
        ).observations(
            chain=CHAIN,
            pair_id="p",
            token_address=TOKEN,
            window=SignalWindow(start=at - timedelta(hours=6), end=at),
        )
        return collected.observations[0]

    early = await normalized(NOW)
    late = await normalized(NOW + timedelta(hours=6))
    # The post's own timestamp is a property of the post. Only the receipt moved.
    assert early.created_at == late.created_at == NOW - timedelta(hours=1)
    assert late.received_at == NOW + timedelta(hours=6)

    # And six hours on, that moment has fallen out of the window — which a fresh
    # round-trip cannot rescue.
    _, first = await read(pages(page(casts)))
    _, second = await read(pages(page(casts)), now=NOW + timedelta(hours=6))
    assert first.features.observation_count == 6
    assert second.features.observation_count == 0
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW in second.structure.gaps


async def test_the_digest_ignores_when_we_collected(monkeypatch):
    """Scenario D. Receipt time is audit metadata and not part of the input."""
    casts = [bound_cast(index, minutes_ago=30) for index in range(6)]
    _, first = await read(pages(page(casts)))
    _, later = await read(pages(page(casts)), now=NOW + timedelta(minutes=3))
    assert first.features.latest_observation_at == later.features.latest_observation_at
    assert signal_input_digest(first) == signal_input_digest(later)


async def test_received_at_is_recorded_and_is_never_the_age():
    """Both times exist, and only one of them is freshness."""
    recording = RecordingRoutes(pages(page([bound_cast(1, minutes_ago=45)])))
    collected = await NeynarSignalSource(
        config=CONFIG, transport_factory=recording.transport_factory(), clock=FixedClock(NOW)
    ).observations(chain=CHAIN, pair_id="p", token_address=TOKEN, window=_window())
    item = collected.observations[0]
    assert item.created_at == NOW - timedelta(minutes=45)
    assert item.received_at == NOW
    assert item.created_at < item.received_at


def _window() -> SignalWindow:
    return SignalWindow(start=NOW - timedelta(hours=6), end=NOW)


# ------------------------------------------------------- the target asset


def test_the_searched_address_is_the_canonical_base_asset():
    """The domain names the roles. SIGNAL does not choose between pool sides.

    ``MarketIdentity`` carries ``base_asset_id`` and ``quote_asset_id`` as
    distinct canonical fields, so which asset a TradeCase is about is already
    settled upstream. Nothing here inspects a pool, and nothing picks the
    non-native or non-wrapped side by convention.
    """
    market = market_identity()
    assert token_address_of(market.base_asset_id) == TOKEN
    assert token_address_of(market.quote_asset_id) == QUOTE.lower()
    assert TOKEN != QUOTE.lower()


async def test_the_quote_asset_is_never_what_gets_searched():
    recording, _ = await read(pages(page([bound_cast(1, minutes_ago=30)])))
    query = recording.requests[0].url.params["q"]
    assert TOKEN in query
    assert QUOTE.lower() not in query


async def test_the_pool_identity_is_never_mistaken_for_the_token():
    """The pair identifies a pool, and nobody writes about a pool address."""
    market = market_identity()
    pool = market.pair_id.rsplit(":", 1)[-1]
    recording, _ = await read(pages(page([bound_cast(1, minutes_ago=30)])), market=market)
    query = recording.requests[0].url.params["q"]
    assert pool not in query


async def test_a_pool_of_two_ordinary_tokens_still_has_one_answer():
    """Neither side is native or wrapped, so no convention could have decided."""
    first = "0x" + "11" * 20
    second = "0x" + "22" * 20
    market = MarketIdentity(
        provider="geckoterminal",
        chain=CHAIN,
        network="mainnet",
        pair_id=f"{CHAIN}:mainnet:contract_address:{'0x' + 'e5' * 20}",
        base_asset_id=f"{CHAIN}:mainnet:{first}",
        quote_asset_id=f"{CHAIN}:mainnet:{second}",
        venue="uniswap-v3",
        is_fixture=False,
    )
    recording, _ = await read(pages(page([bound_cast(1, minutes_ago=30)])), market=market)
    query = recording.requests[0].url.params["q"]
    assert first in query
    assert second not in query


async def test_swapping_the_recorded_sides_changes_the_target_deliberately():
    """Because the role is the domain's, not a guess, reversing it is meaningful.

    A provider that recorded the pair the other way round is describing a
    different base asset, and SIGNAL follows the recorded identity rather than
    second-guessing it. Anything else would mean SIGNAL deciding what a TradeCase
    is about.
    """
    first = "0x" + "11" * 20
    second = "0x" + "22" * 20
    reversed_market = MarketIdentity(
        provider="geckoterminal",
        chain=CHAIN,
        network="mainnet",
        pair_id=f"{CHAIN}:mainnet:contract_address:{'0x' + 'e5' * 20}",
        base_asset_id=f"{CHAIN}:mainnet:{second}",
        quote_asset_id=f"{CHAIN}:mainnet:{first}",
        venue="uniswap-v3",
        is_fixture=False,
    )
    recording, _ = await read(pages(page([bound_cast(1, minutes_ago=30)])), market=reversed_market)
    assert second in recording.requests[0].url.params["q"]


async def test_a_market_without_a_usable_address_makes_no_request_at_all():
    """No asset to search for means no search, not a guess and not a call."""
    market = MarketIdentity(
        provider="geckoterminal",
        chain=CHAIN,
        network="mainnet",
        pair_id=f"{CHAIN}:mainnet:contract_address:{'0x' + 'e5' * 20}",
        base_asset_id=f"{CHAIN}:mainnet:not-an-evm-address",
        quote_asset_id=f"{CHAIN}:mainnet:{QUOTE}",
        venue="uniswap-v3",
        is_fixture=False,
    )
    recording, task_input = await read(pages(page([])), market=market)
    assert recording.requests == []
    assert task_input.token_address is None
    assert task_input.structure.data_quality == SignalDataQuality.INSUFFICIENT


# ------------------------------------------------ cross-chain collision


@pytest.mark.parametrize(
    ("context", "chain"),
    [("BNB Smart Chain", "bsc"), ("Robinhood Chain", "robinhood")],
)
async def test_a_matching_address_on_the_wrong_chain_is_not_a_match(context, chain):
    """Twenty matching bytes are not an identity without a matching chain.

    The same deployer and nonce reproduce an address on every EVM chain, so this
    is cheap for anyone who wants our reading to be about their token.
    """
    market = market_identity(chain="robinhood")
    casts = [
        cast(index, text=f"{context} listing {TOKEN} #{index}", fid=6000 + index)
        for index in range(8)
    ]
    _, task_input = await read(pages(page(casts)), market=market)
    if chain == "robinhood":
        assert task_input.features.observation_count == 8
        assert task_input.features.strong_binding_count == 8
    else:
        assert task_input.features.observation_count == 0
        assert task_input.features.excluded_unbound_count == 8


async def test_a_denied_chain_never_becomes_a_positive_binding():
    """ "not on BSC" names a chain and claims the opposite of being on it."""
    casts = [
        cast(index, text=f"this token is not on BSC, {TOKEN}", fid=7000 + index)
        for index in range(8)
    ]
    _, task_input = await read(pages(page(casts)))
    assert task_input.features.observation_count == 8
    # Admitted as an unscoped reference, never as a chain-bound one.
    assert task_input.features.strong_binding_count == 0
    assert all(
        item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED
        for item in task_input.representatives
    )


# ------------------------------------------------------------- coverage


async def test_an_exhausted_provider_stream_is_reported_as_exhausted():
    casts = [bound_cast(index, minutes_ago=30, fid=5000 + index) for index in range(6)]
    _, task_input = await read(pages(page(casts)))
    assert task_input.features.coverage == CollectionCoverage.PROVIDER_RESULTS_EXHAUSTED
    assert SignalGap.COLLECTION_TRUNCATED not in task_input.structure.gaps


async def test_a_stream_we_stopped_reading_is_never_reported_as_complete():
    """The list looks the same. The evidence behind it does not."""
    served = 0

    def always_more(request):
        nonlocal served
        served += 1
        return json_response(
            page(
                [bound_cast(served, minutes_ago=20 + served, fid=5000 + served)],
                cursor=f"cursor-{served}",
            )
        )

    _, task_input = await read(always_more)
    assert task_input.features.coverage == CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET
    assert SignalGap.COLLECTION_TRUNCATED in task_input.structure.gaps


async def test_a_truncated_sample_is_usable_and_never_clean():
    """Still interpretable — and never a clean read of the window it describes."""
    served = 0

    def always_more(request):
        nonlocal served
        served += 1
        return json_response(
            page(
                [
                    bound_cast(
                        served * 10 + offset,
                        minutes_ago=20 + offset,
                        fid=6000 + served * 10 + offset,
                    )
                    for offset in range(6)
                ],
                cursor=f"cursor-{served}",
            )
        )

    _, task_input = await read(always_more)
    assert task_input.features.observation_count == 12
    assert task_input.structure.data_quality == SignalDataQuality.DEGRADED


async def test_a_local_observation_ceiling_also_counts_as_truncation():
    """However the boundary was chosen, it was ours and not the provider's."""
    casts = [bound_cast(index, minutes_ago=20 + index, fid=5000 + index) for index in range(40)]
    _, task_input = await read(pages(page(casts)), max_observations=10)
    assert task_input.features.received_count == 10
    assert task_input.features.coverage == CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET


# ---------------------------------------------------- the request budget


def test_the_budget_follows_the_plan_that_actually_runs():
    """One reachable query class is one class's worth of pages.

    Reserving budget for a search nobody makes would overstate the cost of every
    assessment, and a hard-coded multiplier would keep doing so after the plan
    changed.
    """
    assert CONFIG.max_requests(1) == CONFIG.max_pages
    assert CONFIG.max_requests(2) == CONFIG.max_pages * 2
    # A degenerate plan still leaves room for the one call it would make.
    assert CONFIG.max_requests(0) == CONFIG.max_pages


async def test_one_query_class_spends_at_most_its_own_pages():
    served = 0

    def always_more(request):
        nonlocal served
        served += 1
        return json_response(
            page([bound_cast(served, minutes_ago=30, fid=5000 + served)], cursor=f"c{served}")
        )

    recording, _ = await read(always_more)
    assert len(recording.requests) == CONFIG.max_pages
