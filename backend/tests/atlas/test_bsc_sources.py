"""BNB Smart Chain fact sources: Moralis holders and Etherscan V2 creation.

Blockscout does not index BNB Smart Chain, so chain 56 is served by different
vendors. What matters is that the normalized facts are identical in domain
semantics — the vendor only survives in provenance.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.agents.atlas.models import (
    AtlasSourceFailure,
    HolderCompleteness,
    HolderObservationBasis,
)
from src.agents.atlas.sources.etherscan import EtherscanConfig, EtherscanContractOriginSource
from src.agents.atlas.sources.http import SourceRequestError
from src.agents.atlas.sources.moralis import MoralisConfig, MoralisHolderSource
from src.core.clock import FixedClock
from src.markets.models import Availability
from tests.atlas.conftest import TOKEN
from tests.atlas.fake_http import RecordingRoutes, json_response

OBSERVED = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
CREATOR = "0x" + "e7" * 20
CREATION_TX = "0x" + "11" * 32

MORALIS = MoralisConfig(
    base_url="https://deep-index.moralis.test/api/v2.2",
    api_key="moralis_testkey",
    max_pages=2,
    page_size=100,
)
ETHERSCAN = EtherscanConfig(
    base_url="https://api.etherscan.test", chain_id=56, api_key="etherscan_testkey"
)


def owner(index: int, balance: int, *, percentage: float = 1.0) -> dict[str, object]:
    return {
        "owner_address": "0x" + f"{index:02x}" * 20,
        "balance": str(balance),
        "balance_formatted": "1.0",
        "is_contract": False,
        "percentage_relative_to_total_supply": percentage,
    }


def owners(count: int, *, start: int = 1, top: int = 5 * 10**22, cursor: object = None):
    return {
        "page": 0,
        "page_size": 100,
        "cursor": cursor,
        "total_supply": str(10**24),
        "result": [owner(start + offset, top - offset * 10**20) for offset in range(count)],
    }


def source(recording: RecordingRoutes) -> MoralisHolderSource:
    return MoralisHolderSource(
        config=MORALIS,
        chain="bsc",
        clock=FixedClock(OBSERVED),
        transport_factory=recording.transport_factory(),
    )


async def test_holder_rows_normalize_to_the_same_shape_as_the_other_chain():
    recording = RecordingRoutes({"/owners": lambda request: json_response(owners(12))})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.status == Availability.AVAILABLE
    assert result.completeness == HolderCompleteness.COMPLETE
    assert result.chain == "bsc"
    assert result.token_address == TOKEN
    assert [row.balance_raw for row in result.rows][0] == 5 * 10**22
    assert result.provider_total_supply_raw == 10**24


async def test_provenance_is_the_weaker_response_anchor_and_says_so():
    """Moralis names no block, so the fact records reduced assurance explicitly."""
    recording = RecordingRoutes({"/owners": lambda request: json_response(owners(12))})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.observation_basis == HolderObservationBasis.RESPONSE_TIME
    assert result.snapshot_block is None
    assert result.snapshot_timestamp == OBSERVED
    # Holder count is not supplied, and is never guessed from the page length.
    assert result.holder_count is None


async def test_the_documented_descending_order_is_requested_and_the_key_stays_in_a_header():
    recording = RecordingRoutes({"/owners": lambda request: json_response(owners(12))})
    await source(recording).holder_facts("bsc", TOKEN)
    request = recording.requests[0]
    assert request.url.params["order"] == "DESC"
    assert request.url.params["chain"] == "0x38"
    assert request.headers["X-API-Key"] == "moralis_testkey"
    assert "moralis_testkey" not in str(request.url)


async def test_a_cursor_continues_the_page_set_within_the_budget():
    pages = iter([owners(12, cursor="next-one"), owners(12, start=40, top=10**22, cursor="two")])
    recording = RecordingRoutes({"/owners": lambda request: json_response(next(pages))})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.completeness == HolderCompleteness.TOP_N_ONLY
    assert len(result.rows) == 24
    assert recording.requests[1].url.params["cursor"] == "next-one"


async def test_every_page_states_the_order_instead_of_trusting_the_default():
    """`order` defaults to DESC, which is exactly why it is sent explicitly.

    A documented default is a vendor's choice to change; the prefix guarantee
    rests on the parameter we actually sent, on the continued page as much as on
    the first.
    """
    pages = iter([owners(12, cursor="next-one"), owners(12, start=40, top=10**22)])
    recording = RecordingRoutes({"/owners": lambda request: json_response(next(pages))})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.status == Availability.AVAILABLE
    assert len(recording.requests) == 2
    for request in recording.requests:
        assert request.url.params["order"] == "DESC"
        assert request.url.params["chain"] == "0x38"
    assert recording.requests[1].url.params["cursor"] == "next-one"


async def test_a_second_receipt_of_the_same_snapshot_stays_response_time_only():
    """The Phase 2D freshness defect, in the shape this provider can take.

    Moralis names no block and no indexer timestamp, so a second fetch twenty
    minutes later yields a second *receipt* and nothing more. The test can prove
    what the basis is called, never that the indexed state moved — that is the
    limitation, stated rather than papered over.
    """
    later = OBSERVED + timedelta(minutes=20)
    payload = owners(12)
    recording = RecordingRoutes({"/owners": lambda request: json_response(payload)})
    first = await source(recording).holder_facts("bsc", TOKEN)
    second = await MoralisHolderSource(
        config=MORALIS,
        chain="bsc",
        clock=FixedClock(later),
        transport_factory=recording.transport_factory(),
    ).holder_facts("bsc", TOKEN)

    assert [row.balance_raw for row in first.rows] == [row.balance_raw for row in second.rows]
    for result in (first, second):
        assert result.observation_basis == HolderObservationBasis.RESPONSE_TIME
        # No block and no indexer snapshot time is invented to fill the gap.
        assert result.snapshot_block is None
    assert first.snapshot_timestamp == OBSERVED
    assert second.snapshot_timestamp == later


async def test_a_repeating_cursor_is_refused():
    recording = RecordingRoutes(
        {"/owners": lambda request: json_response(owners(12, cursor="same"))}
    )
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_an_unordered_page_means_the_documented_contract_was_not_honoured():
    payload = owners(12)
    payload["result"] = list(reversed(payload["result"]))
    recording = RecordingRoutes({"/owners": lambda request: json_response(payload)})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize(
    "mutation",
    [{"owner_address": "0xnothex"}, {"balance": "-1"}, {"balance": 2.5}, {"is_contract": "yes"}],
)
async def test_malformed_owner_rows_are_refused(mutation):
    payload = owners(12)
    payload["result"][0].update(mutation)
    recording = RecordingRoutes({"/owners": lambda request: json_response(payload)})
    result = await source(recording).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_chain_this_source_does_not_serve_is_explicit_about_it():
    recording = RecordingRoutes({"/owners": lambda request: json_response(owners(12))})
    result = await source(recording).holder_facts("robinhood", TOKEN)
    assert result.failure == AtlasSourceFailure.UNSUPPORTED_CHAIN
    assert recording.requests == []


def creation_payload(*, address: str = TOKEN, factory: str = "", status: str = "1"):
    return {
        "status": status,
        "message": "OK",
        "result": [
            {
                "blockNumber": "34000000",
                "contractAddress": address,
                "contractCreator": CREATOR,
                "contractFactory": factory,
                "timestamp": "1700000000",
                "txHash": CREATION_TX,
            }
        ],
    }


def origin(recording: RecordingRoutes) -> EtherscanContractOriginSource:
    return EtherscanContractOriginSource(
        config=ETHERSCAN, chain="bsc", transport_factory=recording.transport_factory()
    )


async def test_bsc_creation_is_read_through_the_shared_parser():
    recording = RecordingRoutes({"/v2/api": lambda request: json_response(creation_payload())})
    facts = await origin(recording).origin_facts("bsc", TOKEN)
    assert facts.status == Availability.AVAILABLE
    assert facts.creator_address == CREATOR
    assert facts.creation_block == 34_000_000
    assert recording.requests[0].url.params["chainid"] == "56"


async def test_a_factory_deployment_records_the_factory_separately():
    factory = "0x" + "cc" * 20
    recording = RecordingRoutes(
        {"/v2/api": lambda request: json_response(creation_payload(factory=factory))}
    )
    facts = await origin(recording).origin_facts("bsc", TOKEN)
    assert facts.factory_address == factory
    # Being deployed by a factory is a fact, not yet an interpretation.
    assert facts.creator_is_contract is None


async def test_creation_data_about_another_contract_is_refused():
    recording = RecordingRoutes(
        {"/v2/api": lambda request: json_response(creation_payload(address="0x" + "99" * 20))}
    )
    facts = await origin(recording).origin_facts("bsc", TOKEN)
    assert facts.status == Availability.UNAVAILABLE
    assert facts.failure == AtlasSourceFailure.TOKEN_MISMATCH


async def test_a_wellformed_no_data_answer_is_an_absent_fact_not_a_broken_source():
    recording = RecordingRoutes(
        {
            "/v2/api": lambda request: json_response(
                {"status": "0", "message": "NOTOK", "result": []}
            )
        }
    )
    facts = await origin(recording).origin_facts("bsc", TOKEN)
    assert facts.failure == AtlasSourceFailure.UNAVAILABLE


async def test_a_query_string_key_never_reaches_a_failure_message():
    """Etherscan requires the key in the URL, so nothing may echo the URL back."""
    recording = RecordingRoutes(
        {"/v2/api": lambda request: json_response({"error": "nope"}, status=429)}
    )
    facts = await origin(recording).origin_facts("bsc", TOKEN)
    assert facts.failure == AtlasSourceFailure.RATE_LIMIT
    assert str(SourceRequestError(facts.failure)) == "RATE_LIMIT"
    assert "etherscan_testkey" not in repr(facts)
