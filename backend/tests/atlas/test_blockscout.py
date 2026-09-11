"""Blockscout adapter behaviour, proven against exact fixtures of the real schema.

The payload shapes here were taken from live Blockscout v2 responses, reduced to
the smallest form that still exercises the contract. No test performs I/O.
"""

import httpx
import pytest

from src.agents.atlas.models import (
    AtlasSourceFailure,
    HolderCompleteness,
    HolderObservationBasis,
)
from src.agents.atlas.sources.blockscout import (
    BlockscoutConfig,
    BlockscoutContractOriginSource,
    BlockscoutHolderSource,
)
from src.agents.atlas.sources.normalize import concentration
from src.markets.models import Availability
from tests.atlas.conftest import TOKEN
from tests.atlas.fake_http import RecordingRoutes, json_response

HEAD_TIME = "2026-09-09T11:58:00.000000Z"
CREATOR = "0x" + "e7" * 20
CREATION_TX = "0x" + "11" * 32

CONFIG = BlockscoutConfig(
    base_url="https://api.blockscout.test",
    chain_id=4663,
    api_key="proapi_testkey",
    max_pages=2,
)


def metadata(*, address: str = TOKEN, kind: str = "ERC-20") -> dict[str, object]:
    return {
        "address_hash": address,
        "decimals": "18",
        "holders_count": "4200",
        "name": "Fixture",
        "symbol": "FIX",
        "total_supply": str(10**24),
        "type": kind,
    }


def holder_item(index: int, balance: int, *, is_contract: bool = False) -> dict[str, object]:
    return {
        "address": {"hash": "0x" + f"{index:02x}" * 20, "is_contract": is_contract},
        "token_id": None,
        "value": str(balance),
    }


def page(count: int, *, start: int = 1, top: int = 5 * 10**22, next_params: object = None):
    items = [holder_item(start + offset, top - offset * 10**20) for offset in range(count)]
    return {"items": items, "next_page_params": next_params}


def head(height: int = 999_998, timestamp: str = HEAD_TIME) -> list[dict[str, object]]:
    return [{"height": height, "timestamp": timestamp, "hash": "0x" + "ab" * 32}]


class PagingRoutes(RecordingRoutes):
    """Routes that hand each holder page a distinct, strictly lower address range."""

    def __init__(self, cursors) -> None:
        super().__init__({})
        self._cursors = iter(cursors)
        self._index = 0
        self.routes = {
            "/holders": self.holders,
            "/main-page/blocks": lambda request: json_response(head()),
            "/api/v2/tokens/" + TOKEN: lambda request: json_response(metadata()),
        }

    def holders(self, request):
        payload = page(
            12,
            start=1 + self._index * 12,
            top=5 * 10**22 - self._index * 10**22,
            next_params=next(self._cursors),
        )
        self._index += 1
        return json_response(payload)


def routes(**overrides: object) -> RecordingRoutes:
    holders = overrides.get("holders", page(12))
    handlers = {
        "/main-page/blocks": lambda request: json_response(overrides.get("head", head())),
        "/holders": (holders if callable(holders) else (lambda request: json_response(holders))),
        "/api/v2/tokens/" + TOKEN: lambda request: json_response(
            overrides.get("metadata", metadata())
        ),
    }
    return RecordingRoutes(handlers)


def source(recording: RecordingRoutes) -> BlockscoutHolderSource:
    return BlockscoutHolderSource(
        config=CONFIG, chain="robinhood", transport_factory=recording.transport_factory()
    )


async def test_a_single_page_yields_an_ordered_block_anchored_result():
    recording = routes()
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.status == Availability.AVAILABLE
    assert result.completeness == HolderCompleteness.COMPLETE
    assert result.observation_basis == HolderObservationBasis.SOURCE_BLOCK
    assert result.snapshot_block == 999_998
    assert result.holder_count == 4200
    assert result.provider_total_supply_raw == 10**24
    assert [row.balance_raw for row in result.rows] == sorted(
        (row.balance_raw for row in result.rows), reverse=True
    )
    # One metadata read, one indexer head read, one holder page.
    assert result.requests_made == 3


async def test_the_api_key_travels_in_a_header_and_never_in_a_url():
    recording = routes()
    await source(recording).holder_facts("robinhood", TOKEN)
    assert recording.requests
    for request in recording.requests:
        assert request.headers["Authorization"] == "Bearer proapi_testkey"
        assert "proapi_testkey" not in str(request.url)


async def test_the_indexer_head_is_read_before_the_holder_rows():
    """Provenance may only under-claim freshness, never over-claim it."""
    recording = routes()
    await source(recording).holder_facts("robinhood", TOKEN)
    paths = [request.url.path for request in recording.requests]
    assert paths.index("/4663/api/v2/main-page/blocks") < next(
        index for index, path in enumerate(paths) if path.endswith("/holders")
    )


async def test_a_continued_page_set_is_reported_as_a_proven_prefix_only():
    pages = iter(
        [
            page(12, next_params={"value": "1", "address_hash": "0x" + "aa" * 20}),
            page(
                12,
                start=40,
                top=10**22,
                next_params={"value": "2", "address_hash": "0x" + "bb" * 20},
            ),
        ]
    )
    recording = routes(holders=lambda request: json_response(next(pages)))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.status == Availability.AVAILABLE
    # Two pages were allowed and a third was still promised, so coverage is a
    # prefix rather than the whole holder universe.
    assert result.completeness == HolderCompleteness.TOP_N_ONLY
    assert len(result.rows) == 24


async def test_an_out_of_order_page_is_refused_however_well_formed_it_is():
    """The provider order guarantee is what makes a prefix a global top-N.

    Blockscout's implementation orders `desc: value, desc: address_hash` and
    pages with a strictly descending keyset predicate, so a page that arrives
    unordered means the deployment is not keeping the contract the prefix rests
    on. Observed-order validation is not that guarantee — it is the check that
    sits beside it.
    """
    payload = page(12)
    payload["items"] = list(reversed(payload["items"]))
    recording = routes(holders=payload)
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_second_page_holding_a_larger_balance_than_the_first_is_refused():
    """Cross-page monotonicity: page two may only continue strictly downwards."""
    pages = iter(
        [
            page(12, next_params={"value": "1", "address_hash": "0x" + "aa" * 20}),
            # Every row here outweighs the first page, which the keyset predicate
            # makes impossible. A locally ordered page is not enough.
            page(12, start=40, top=9 * 10**22),
        ]
    )
    recording = routes(holders=lambda request: json_response(next(pages)))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_the_holder_list_declares_the_address_the_provider_filters_out():
    """Blockscout removes the zero address from its holder query. Say so."""
    recording = routes()
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.excluded_addresses == ("0x" + "0" * 40,)


async def test_a_provider_filtered_burn_address_withholds_the_burn_adjustment():
    """A burn total that cannot see a burn sink is a lower bound, not a fact.

    Using it as a denominator adjustment would understate concentration, so the
    adjusted figure is withheld even though this holder set is COMPLETE.
    """
    recording = routes()
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.completeness == HolderCompleteness.COMPLETE
    measured = concentration(result.rows, 10**24, result.completeness, result.excluded_addresses)
    assert measured.top10_share > 0
    assert measured.burned_raw is None
    assert measured.burned_share is None
    assert measured.top10_share_excluding_burn is None


async def test_a_repeating_cursor_cannot_loop_forever():
    cursor = {"value": "1", "address_hash": "0x" + "aa" * 20}
    recording = routes(holders=lambda request: json_response(page(12, next_params=cursor)))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    # Two pages is the configured budget, so the loop ends either way; the
    # duplicate addresses in the repeated page are what make it a hard failure.
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_an_empty_page_that_still_promises_more_is_refused():
    recording = routes(
        holders=lambda request: json_response({"items": [], "next_page_params": {"value": "1"}})
    )
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_an_unknown_pagination_parameter_cannot_be_echoed_into_the_next_request():
    recording = routes(
        holders=lambda request: json_response(
            {"items": page(12)["items"], "next_page_params": {"apikey": "stolen"}}
        )
    )
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_page_larger_than_the_provider_cap_is_refused():
    recording = routes(holders=lambda request: json_response(page(201)))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_duplicated_holder_across_pages_is_refused():
    pages = iter(
        [
            page(12, next_params={"value": "1"}),
            page(12),
        ]
    )
    recording = routes(holders=lambda request: json_response(next(pages)))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_response_about_another_token_is_refused():
    recording = routes(metadata=metadata(address="0x" + "99" * 20))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.TOKEN_MISMATCH


async def test_a_non_erc20_token_is_not_this_domain():
    recording = routes(metadata=metadata(kind="ERC-721"))
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.UNSUPPORTED_ENDPOINT


async def test_an_nft_holder_row_is_refused():
    rows = page(12)
    rows["items"][0]["token_id"] = "7"
    recording = routes(holders=rows)
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize(
    "mutation",
    [
        {"address": {"hash": "not-an-address", "is_contract": False}},
        {"value": "-5"},
        {"value": 1.5},
        {"value": None},
    ],
)
async def test_malformed_holder_rows_are_refused(mutation):
    rows = page(12)
    rows["items"][0].update(mutation)
    recording = routes(holders=rows)
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AtlasSourceFailure.NOT_CONFIGURED),
        (402, AtlasSourceFailure.NOT_CONFIGURED),
        (403, AtlasSourceFailure.NOT_CONFIGURED),
        (404, AtlasSourceFailure.UNAVAILABLE),
        (429, AtlasSourceFailure.RATE_LIMIT),
        (500, AtlasSourceFailure.UNAVAILABLE),
        (418, AtlasSourceFailure.INVALID_RESPONSE),
    ],
)
async def test_provider_status_codes_map_to_safe_categories(status, expected):
    recording = RecordingRoutes({"": lambda request: json_response({"error": "no"}, status=status)})
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == expected


async def test_an_html_challenge_page_is_never_mistaken_for_data():
    """The public explorer host answers server-side clients with an HTML challenge."""
    recording = RecordingRoutes(
        {
            "": lambda request: httpx.Response(
                200,
                content=b"<!DOCTYPE html><html>Just a moment...</html>",
                headers={"Content-Type": "text/html; charset=UTF-8"},
            )
        }
    )
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_chain_this_source_does_not_serve_is_explicit_about_it():
    recording = routes()
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.UNSUPPORTED_CHAIN
    assert recording.requests == []


async def test_creation_provenance_is_read_with_its_factory_and_transaction():
    recording = RecordingRoutes(
        {
            "/v2/api": lambda request: json_response(
                {
                    "status": "1",
                    "message": "OK",
                    "result": [
                        {
                            "blockNumber": "900000",
                            "contractAddress": TOKEN,
                            "contractCreator": CREATOR,
                            "contractFactory": "",
                            "timestamp": "1533324504",
                            "txHash": CREATION_TX,
                        }
                    ],
                }
            )
        }
    )
    origin = BlockscoutContractOriginSource(
        config=CONFIG, chain="robinhood", transport_factory=recording.transport_factory()
    )
    facts = await origin.origin_facts("robinhood", TOKEN)
    assert facts.status == Availability.AVAILABLE
    assert facts.creator_address == CREATOR
    assert facts.creation_tx_hash == CREATION_TX
    assert facts.creation_block == 900_000
    assert facts.factory_address is None
    # A provider answering is not verification.
    assert facts.verification.value == "UNVERIFIED"


async def test_the_request_budget_scales_with_the_configured_page_budget():
    """A five-page budget must not be strangled by a fixed request ceiling."""
    config = BlockscoutConfig(
        base_url="https://api.blockscout.test",
        chain_id=4663,
        api_key="proapi_testkey",
        max_pages=5,
    )
    assert config.max_requests == 7
    recording = PagingRoutes([{"value": str(index)} for index in range(1, 5)] + [None])
    source = BlockscoutHolderSource(
        config=config, chain="robinhood", transport_factory=recording.transport_factory()
    )
    result = await source.holder_facts("robinhood", TOKEN)
    assert result.status == Availability.AVAILABLE
    assert result.completeness == HolderCompleteness.COMPLETE
    # Metadata, indexer head, and all five holder pages.
    assert result.requests_made == 7
    assert len(result.rows) == 60


async def test_a_normalization_refusal_stays_a_typed_source_failure():
    """A duplicate that reaches normalization must not escape as a bare exception."""
    from src.agents.atlas.sources.normalize import HolderNormalizationError

    recording = routes()
    source = BlockscoutHolderSource(
        config=CONFIG, chain="robinhood", transport_factory=recording.transport_factory()
    )
    duplicated = page(12)["items"]

    async def collect(transport, token_address):
        raise HolderNormalizationError(AtlasSourceFailure.SUPPLY_INCONSISTENT)

    object.__setattr__(source, "_collect", collect)
    result = await source.holder_facts("robinhood", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.SUPPLY_INCONSISTENT
    assert duplicated
