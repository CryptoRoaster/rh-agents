"""The Neynar adapter, proven against fixtures of the real response schema.

Two things are under test that matter more than the plumbing. The request must
ask for an unranked, unpersonalized, unfiltered slice of the public timeline —
because every one of those knobs would let the provider pre-select what SIGNAL
concludes. And a cast returned by a search for our address must not become a cast
*about* our token: the query cannot be evidence for its own premise.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.agents.signal.models import (
    CollectionCoverage,
    MarketBindingBasis,
    ObservationKind,
    SignalSource,
    SignalWindow,
)
from src.agents.signal.ports import SignalSourceUnavailable
from src.agents.signal.sources.neynar import (
    QUERY_PLAN_VERSION,
    NeynarConfig,
    NeynarSignalSource,
)
from src.core.clock import FixedClock
from tests.signal.conftest import CHAIN, TOKEN
from tests.signal.fake_http import RecordingRoutes, json_response, pages

OTHER_TOKEN = "0x" + "99" * 20
RECEIVED = datetime(2026, 9, 9, 12, tzinfo=UTC)

CONFIG = NeynarConfig(
    base_url="https://api.neynar.test",
    api_key="neynar_testkey",
    max_pages=2,
    page_size=50,
)


def cast(
    index: int,
    *,
    text: str,
    fid: int = 1000,
    minutes_ago: int = 30,
    parent: str | None = None,
    likes: int | None = 3,
    recasts: int | None = 1,
    replies: int | None = 0,
) -> dict[str, object]:
    """One cast in the shape the documented schema returns."""
    return {
        "object": "cast",
        "hash": "0x" + f"{index:02x}" * 20,
        "thread_hash": "0x" + f"{index:02x}" * 20,
        "parent_hash": parent,
        "parent_url": None,
        "root_parent_url": None,
        "parent_author": {"fid": None},
        "author": {
            "object": "user",
            "fid": fid,
            "username": f"user{fid}",
            "display_name": f"User {fid}",
            "score": 0.9,
            "experimental": {"neynar_user_score": 0.9},
        },
        "text": text,
        "timestamp": (RECEIVED - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
        "embeds": [],
        "reactions": {"likes_count": likes, "recasts_count": recasts},
        "replies": {"count": replies},
        "channel": None,
        "mentioned_profiles": [],
    }


def page(casts: list[dict[str, object]], cursor: str | None = None) -> dict[str, object]:
    return {"result": {"casts": casts, "next": {"cursor": cursor}}}


def window() -> SignalWindow:
    return SignalWindow(start=RECEIVED - timedelta(hours=6), end=RECEIVED)


def source(recording: RecordingRoutes) -> NeynarSignalSource:
    return NeynarSignalSource(
        config=CONFIG,
        transport_factory=recording.transport_factory(),
        clock=FixedClock(RECEIVED),
    )


async def collect(handler, *, token=TOKEN):
    recording = RecordingRoutes(handler)
    collected = await source(recording).observations(
        chain=CHAIN, pair_id=f"{CHAIN}:mainnet:pool", token_address=token, window=window()
    )
    return recording, collected.observations


# ------------------------------------------------------ request construction


async def test_the_search_is_literal_chronological_and_time_bounded():
    """Everything the provider could use to pre-select is stated, not defaulted."""
    recording, _ = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    request = recording.requests[0]
    assert request.url.path == "/v2/farcaster/cast/search"
    assert request.url.params["mode"] == "literal"
    assert request.url.params["sort_type"] == "desc_chron"
    assert request.url.params["limit"] == "50"
    query = request.url.params["q"]
    assert f'"{TOKEN}"' in query
    assert "after:2026-09-09T06:00:00" in query
    assert "before:2026-09-09T12:00:00" in query


async def test_no_viewer_personalizes_the_result_set():
    """A viewer's mutes and blocks would decide what SIGNAL is allowed to see."""
    recording, _ = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    assert "viewer_fid" not in recording.requests[0].url.params


async def test_no_provider_side_spam_filtering_is_requested():
    """SIGNAL measures campaigns. A provider that removes them first removes the evidence."""
    recording, _ = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    request = recording.requests[0]
    for knob in ("x-neynar-experimental", "X-Neynar-Experimental"):
        assert knob not in request.headers
    for parameter in ("priority_mode", "spam_filter", "filter", "quality"):
        assert parameter not in request.url.params


async def test_the_api_key_travels_in_a_header_and_never_in_a_url():
    recording, _ = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    for request in recording.requests:
        assert request.headers["x-api-key"] == "neynar_testkey"
        assert "neynar_testkey" not in str(request.url)


def test_the_query_plan_is_versioned_and_bounded():
    plan = NeynarSignalSource(config=CONFIG).query_plan(TOKEN, "DEMO")
    assert QUERY_PLAN_VERSION == "signal-neynar-query-v1"
    assert [item.name for item in plan] == ["CONTRACT_ADDRESS", "SYMBOL"]


@pytest.mark.parametrize(
    "symbol",
    [
        "A|B",  # a disjunction
        "-DEMO",  # a negation
        "DEMO*",  # a prefix wildcard
        'DEMO"',  # a quote that would close the phrase
        "after:2020",  # a rewritten time bound
        "(DEMO)",  # grouping
        "DEMO~2",  # fuzziness
        "a",  # too short to be a ticker
        "D" * 32,  # too long
        "",
    ],
)
def test_a_symbol_that_could_rewrite_the_query_is_never_sent(symbol):
    """Neynar's search language gives these characters meaning.

    A token called ``A|B`` must not silently become a disjunction, and one called
    ``after:2020`` must not move the window it is being searched in.
    """
    plan = NeynarSignalSource(config=CONFIG).query_plan(None, symbol)
    assert plan == ()


def test_a_project_name_query_is_deliberately_absent():
    """Names are ambiguous and no binding rule could consume one safely."""
    plan = NeynarSignalSource(config=CONFIG).query_plan(TOKEN, "DEMO")
    assert all(item.name != "PROJECT_NAME" for item in plan)


# ------------------------------------------------------------ normalization


async def test_a_cast_becomes_one_normalized_farcaster_observation():
    _, observations = await collect(
        pages(page([cast(7, text=f"BSC token {TOKEN} looks solid", fid=4242, minutes_ago=12)]))
    )
    assert len(observations) == 1
    item = observations[0]
    assert item.source == SignalSource.FARCASTER
    assert item.source_native_id == "0x" + "07" * 20
    # The numeric FID, never the rentable username or the colliding display name.
    assert item.author_id == "4242"
    assert item.author_key == "FARCASTER:4242"
    assert item.kind == ObservationKind.ORIGINAL
    assert item.created_at == RECEIVED - timedelta(minutes=12)
    assert item.received_at == RECEIVED
    assert item.provider == "neynar"


async def test_source_time_and_receipt_time_stay_separate():
    """A cast written an hour ago is an hour old however recently it was fetched."""
    _, observations = await collect(pages(page([cast(1, text=f"gm {TOKEN}", minutes_ago=60)])))
    item = observations[0]
    assert item.created_at == RECEIVED - timedelta(minutes=60)
    assert item.received_at == RECEIVED
    assert item.created_at < item.received_at


async def test_a_reply_is_an_authored_position_and_not_a_share():
    parent = "0x" + "ff" * 20
    _, observations = await collect(
        pages(page([cast(2, text=f"disagree about {TOKEN}", parent=parent)]))
    )
    item = observations[0]
    assert item.kind == ObservationKind.REPLY
    assert item.referenced_observation_id is not None


async def test_engagement_counts_are_carried_and_never_become_authors():
    """Five hundred recasts are one post. Inventing identities would invent a crowd."""
    _, observations = await collect(
        pages(page([cast(3, text=f"gm {TOKEN}", likes=900, recasts=500, replies=40)]))
    )
    assert len(observations) == 1
    engagement = observations[0].engagement
    assert engagement is not None
    assert engagement.reposts == 500
    assert engagement.likes == 900
    assert observations[0].kind == ObservationKind.ORIGINAL


async def test_an_absent_engagement_count_stays_absent():
    _, observations = await collect(
        pages(page([cast(4, text=f"gm {TOKEN}", likes=None, recasts=None, replies=None)]))
    )
    engagement = observations[0].engagement
    assert engagement is not None
    assert engagement.likes is None and engagement.reposts is None


async def test_the_provider_user_score_reaches_nothing():
    """It filters nothing and is carried nowhere, by construction."""
    _, observations = await collect(pages(page([cast(5, text=f"gm {TOKEN}")])))
    rendered = observations[0].model_dump_json()
    assert "neynar_user_score" not in rendered
    assert "0.9" not in rendered


async def test_over_long_provider_text_is_truncated_visibly():
    """A silently shortened post could read as a different sentiment."""
    long_text = f"{TOKEN} " + "very bullish indeed " * 60
    _, observations = await collect(pages(page([cast(6, text=long_text)])))
    content = observations[0].content
    assert len(content) <= 600
    assert content.endswith("…[truncated]")


# ------------------------------------------- binding is decided from content


async def test_an_address_with_chain_context_binds_strongly():
    """Scenario A. The cast itself says which chain, so the binding can be exact."""
    _, observations = await collect(pages(page([cast(1, text=f"Robinhood Chain gem {TOKEN}")])))
    item = observations[0]
    assert item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_EXACT
    assert item.binding_address == TOKEN
    assert item.binding_chain == "robinhood"


async def test_an_address_without_chain_context_is_never_bound_to_our_chain():
    """Scenario B, and the reason this whole phase needed an audit.

    The search asked for our address on our TradeCase. The cast contains it. That
    is not evidence of which chain it means, and manufacturing one from the
    TradeCase would make the binding prove itself.
    """
    _, observations = await collect(pages(page([cast(1, text=f"aped into {TOKEN} lfg")])))
    item = observations[0]
    assert item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED
    assert item.binding_address == TOKEN
    assert item.binding_chain is None


async def test_an_address_with_another_chains_context_is_not_ours():
    """Scenario C. The same hex string, explicitly placed somewhere else."""
    _, observations = await collect(
        pages(page([cast(1, text=f"BNB Smart Chain listing for {TOKEN}")]))
    )
    item = observations[0]
    assert item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_EXACT
    assert item.binding_chain == "bsc"
    # Phase 2F compares that against the TradeCase chain and refuses it there.


async def test_a_cast_without_our_address_is_unresolved():
    """Scenario D. Being returned by a search is not being about the asset."""
    _, observations = await collect(pages(page([cast(1, text="$DEMO is going crazy")])))
    item = observations[0]
    assert item.binding_basis == MarketBindingBasis.UNRESOLVED
    assert item.binding_address is None


async def test_a_cast_naming_a_different_address_is_unresolved():
    _, observations = await collect(
        pages(page([cast(1, text=f"Robinhood Chain gem {OTHER_TOKEN}")]))
    )
    assert observations[0].binding_basis == MarketBindingBasis.UNRESOLVED


async def test_no_cast_can_claim_the_project_voice():
    """An embed URL is not a verified project link, whatever it points at."""
    entry = cast(1, text=f"official update {TOKEN} https://demo-project.example")
    entry["embeds"] = [{"url": "https://demo-project.example"}]
    _, observations = await collect(pages(page([entry])))
    assert observations[0].binding_basis != MarketBindingBasis.VERIFIED_PROJECT_LINK


# --------------------------------------------------------------- pagination


async def test_a_single_page_ends_the_query_class():
    recording, observations = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    assert len(observations) == 1
    assert len(recording.requests) == 1


async def test_a_cursor_continues_within_the_page_budget():
    recording, observations = await collect(
        pages(
            page([cast(1, text=f"gm {TOKEN}")], cursor="next-one"),
            page([cast(2, text=f"gm again {TOKEN}")]),
        )
    )
    assert len(observations) == 2
    assert len(recording.requests) == 2
    assert recording.requests[1].url.params["cursor"] == "next-one"


async def test_a_repeating_cursor_cannot_loop_forever():
    """Scenario I."""
    with pytest.raises(SignalSourceUnavailable) as error:
        await collect(pages(page([cast(1, text=f"gm {TOKEN}")], cursor="same")))
    assert error.value.reason_code == "PAGINATION_INCONSISTENT"


async def test_an_empty_page_that_still_promises_more_is_refused():
    with pytest.raises(SignalSourceUnavailable) as error:
        await collect(pages(page([], cursor="more")))
    assert error.value.reason_code == "PAGINATION_INCONSISTENT"


async def test_the_page_budget_bounds_one_assessment():
    """A provider that always promises another page cannot spend an account.

    Each page carries a fresh cursor, so nothing here is a loop the cycle check
    would catch. Only the budget stops it.
    """
    served = 0

    def always_more(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return json_response(page([cast(served, text=f"gm {TOKEN}")], cursor=f"cursor-{served}"))

    recording = RecordingRoutes(always_more)
    collected = await source(recording).observations(
        chain=CHAIN, pair_id="p", token_address=TOKEN, window=window()
    )
    assert len(recording.requests) == CONFIG.max_pages
    assert len(collected.observations) == CONFIG.max_pages
    # And it says so: the provider had more and we stopped reading.
    assert collected.coverage == CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET


async def test_a_page_larger_than_the_provider_cap_is_refused():
    oversized = page([cast(index, text=f"gm {TOKEN}") for index in range(101)])
    with pytest.raises(SignalSourceUnavailable) as error:
        await collect(pages(oversized))
    assert error.value.reason_code == "INVALID_RESPONSE"


# ------------------------------------------------------ schema and failures


@pytest.mark.parametrize(
    "mutation",
    [
        {"hash": "not-a-hash"},
        {"hash": None},
        {"text": 42},
        {"timestamp": "yesterday"},
        {"timestamp": "2026-09-09T12:00:00"},
        {"author": {"fid": 0}},
        {"author": {"fid": "1000"}},
        {"author": None},
        {"reactions": {"likes_count": -1}},
        {"reactions": {"likes_count": 1.5}},
    ],
)
async def test_a_malformed_cast_is_refused_rather_than_coerced(mutation):
    entry = cast(1, text=f"gm {TOKEN}")
    entry.update(mutation)
    with pytest.raises(SignalSourceUnavailable) as error:
        await collect(pages(page([entry])))
    assert error.value.reason_code == "INVALID_RESPONSE"


@pytest.mark.parametrize("payload", [{"result": {}}, {"casts": []}, [], "casts", {"result": []}])
async def test_a_malformed_envelope_is_refused(payload):
    with pytest.raises(SignalSourceUnavailable):
        await collect(pages(payload))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "UNAUTHORIZED"),
        (403, "UNAUTHORIZED"),
        (429, "RATE_LIMIT"),
        (500, "UNAVAILABLE"),
        (503, "UNAVAILABLE"),
        (418, "INVALID_RESPONSE"),
    ],
)
async def test_provider_status_codes_map_to_safe_categories(status, expected):
    recording = RecordingRoutes(lambda request: json_response({"message": "nope"}, status=status))
    with pytest.raises(SignalSourceUnavailable) as error:
        await source(recording).observations(
            chain=CHAIN, pair_id="p", token_address=TOKEN, window=window()
        )
    assert error.value.reason_code == expected


async def test_a_timeout_is_a_typed_failure_and_not_an_empty_feed():
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    recording = RecordingRoutes(raise_timeout)
    with pytest.raises(SignalSourceUnavailable) as error:
        await source(recording).observations(
            chain=CHAIN, pair_id="p", token_address=TOKEN, window=window()
        )
    assert error.value.reason_code == "TIMEOUT"


async def test_an_html_body_is_never_mistaken_for_data():
    recording = RecordingRoutes(
        lambda request: httpx.Response(
            200, content=b"<html>rate limited</html>", headers={"Content-Type": "text/html"}
        )
    )
    with pytest.raises(SignalSourceUnavailable) as error:
        await source(recording).observations(
            chain=CHAIN, pair_id="p", token_address=TOKEN, window=window()
        )
    assert error.value.reason_code == "INVALID_RESPONSE"


async def test_a_failure_never_carries_provider_detail():
    recording = RecordingRoutes(
        lambda request: json_response({"message": "key sk-secret-value is invalid"}, status=401)
    )
    with pytest.raises(SignalSourceUnavailable) as error:
        await source(recording).observations(
            chain=CHAIN, pair_id="p", token_address=TOKEN, window=window()
        )
    assert "sk-secret-value" not in str(error.value)
    assert "neynar_testkey" not in str(error.value)


# ----------------------------------------------------------------- dedupe


async def test_the_same_cast_found_twice_is_one_observation():
    """Scenario E. Two discovery routes, one post, one author, one opinion."""
    duplicate = cast(1, text=f"Robinhood Chain {TOKEN}")
    _, observations = await collect(pages(page([duplicate, duplicate])))
    assert len(observations) == 1


async def test_distinct_casts_stay_distinct():
    _, observations = await collect(
        pages(page([cast(1, text=f"gm {TOKEN}"), cast(2, text=f"gn {TOKEN}")]))
    )
    assert len(observations) == 2
    assert len({item.observation_id for item in observations}) == 2


async def test_a_cast_hash_is_namespaced_by_its_source():
    """Post identifiers only mean something inside their own platform."""
    from src.agents.signal.fake import observation_id

    _, observations = await collect(pages(page([cast(1, text=f"gm {TOKEN}")])))
    native = observations[0].source_native_id
    assert observations[0].observation_id == observation_id(native, SignalSource.FARCASTER)
    assert observations[0].observation_id != observation_id(native, SignalSource.X)
