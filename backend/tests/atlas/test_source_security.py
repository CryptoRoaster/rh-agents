"""Where the provider boundary sits, and what may never cross it.

Adapters are infrastructure. A worker receives one read port with one method and
never a URL, a credential or a client, and nothing a provider returns can widen
that. These tests pin the properties that keep it true.
"""

import inspect

import httpx
import pytest

from src.agents.atlas.context import (
    AtlasContextReader,
    atlas_snapshot_digest,
    snapshot_document,
)
from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.sources.http import SourceRequestError, SourceTransport
from tests.atlas.fake_http import RecordingRoutes, json_response
from tests.atlas.test_data_enablement import blockscout_routes, build, robinhood_builder

SECRETS = ("proapi_testkey", "moralis_testkey", "etherscan_testkey", "api_key", "apikey")


def test_the_read_port_a_worker_receives_has_exactly_one_method():
    methods = {
        name
        for name, value in inspect.getmembers(AtlasContextReader, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == {"onchain_context"}


async def test_no_credential_survives_into_the_fact_document_or_the_digest(now):
    snapshot = await build(robinhood_builder(now), now)
    document = repr(snapshot_document(snapshot))
    for secret in SECRETS:
        assert secret not in document
        assert secret not in repr(snapshot)
    # The digest is over facts, not over how many requests they cost.
    assert "requests_made" not in document
    assert "latency" not in document
    assert isinstance(atlas_snapshot_digest(snapshot), str)


async def test_the_snapshot_carries_no_client_transport_or_url(now):
    snapshot = await build(robinhood_builder(now), now)
    flattened = repr(snapshot.model_dump())
    assert "http" not in flattened.lower()
    assert "client" not in flattened.lower()


async def test_a_transport_refuses_redirects_and_ignores_ambient_proxy_configuration():
    """A redirect could carry a credential to a host nobody configured."""
    transport = SourceTransport(base_url="https://provider.invalid")
    try:
        assert transport._client.follow_redirects is False
        assert transport._client.trust_env is False
    finally:
        await transport.aclose()


async def test_a_request_budget_bounds_one_assessment():
    recording = RecordingRoutes({"": lambda request: json_response({"ok": True})})
    transport = SourceTransport(
        base_url="https://provider.invalid",
        max_requests=2,
        transport=httpx.MockTransport(recording),
    )
    try:
        await transport.get_json("a", {})
        await transport.get_json("b", {})
        with pytest.raises(SourceRequestError) as error:
            await transport.get_json("c", {})
        assert error.value.failure == AtlasSourceFailure.INCOMPLETE_RESULT
    finally:
        await transport.aclose()
    assert len(recording.requests) == 2


async def test_an_oversized_response_is_refused_rather_than_buffered():
    huge = {"items": ["x" * 1000 for _ in range(100)]}
    recording = RecordingRoutes({"": lambda request: json_response(huge)})
    transport = SourceTransport(
        base_url="https://provider.invalid",
        max_response_bytes=500,
        transport=httpx.MockTransport(recording),
    )
    try:
        with pytest.raises(SourceRequestError) as error:
            await transport.get_json("a", {})
        assert error.value.failure == AtlasSourceFailure.INVALID_RESPONSE
    finally:
        await transport.aclose()


async def test_a_failure_never_carries_provider_detail():
    error = SourceRequestError(AtlasSourceFailure.RATE_LIMIT)
    assert str(error) == "RATE_LIMIT"
    assert "https" not in repr(error)


async def test_a_provider_cannot_redirect_the_next_request_to_its_own_host(now):
    """Only documented cursor keys are echoed back, and never a URL."""
    recording = blockscout_routes(now=now)
    recording.routes["/holders"] = lambda request: json_response(
        {
            "items": [],
            "next_page_params": {"value": "1", "url": "https://evil.invalid/steal"},
        }
    )
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.failure == AtlasSourceFailure.INVALID_RESPONSE
    assert all("evil.invalid" not in str(item.url) for item in recording.requests)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx.ReadTimeout("slow"), AtlasSourceFailure.TIMEOUT),
        (httpx.ConnectError("refused"), AtlasSourceFailure.UNAVAILABLE),
        (httpx.RemoteProtocolError("broken"), AtlasSourceFailure.UNAVAILABLE),
    ],
)
async def test_transport_failures_map_to_safe_categories(raised, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    transport = SourceTransport(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(SourceRequestError) as error:
            await transport.get_json("a", {})
        assert error.value.failure == expected
    finally:
        await transport.aclose()


async def test_a_body_that_is_not_json_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{nope", headers={"Content-Type": "application/json"})

    transport = SourceTransport(
        base_url="https://provider.invalid", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(SourceRequestError) as error:
            await transport.get_json("a", {})
        assert error.value.failure == AtlasSourceFailure.INVALID_RESPONSE
    finally:
        await transport.aclose()


async def test_the_production_transports_are_built_with_their_documented_auth():
    """Constructing a real transport performs no request and leaks no key."""
    from src.agents.atlas.sources.blockscout import BlockscoutConfig
    from src.agents.atlas.sources.blockscout import transport_for as blockscout_transport
    from src.agents.atlas.sources.etherscan import EtherscanConfig
    from src.agents.atlas.sources.etherscan import transport_for as etherscan_transport
    from src.agents.atlas.sources.moralis import MoralisConfig
    from src.agents.atlas.sources.moralis import transport_for as moralis_transport

    blockscout = blockscout_transport(
        BlockscoutConfig(base_url="https://api.blockscout.test", chain_id=4663, api_key="k")
    )
    moralis = moralis_transport(
        MoralisConfig(base_url="https://moralis.test/api/v2.2", api_key="k")
    )
    etherscan = etherscan_transport(
        EtherscanConfig(base_url="https://etherscan.test", chain_id=56, api_key="k")
    )
    try:
        assert blockscout._client.headers["Authorization"] == "Bearer k"
        assert moralis._client.headers["X-API-Key"] == "k"
        # Etherscan needs the key in the query string, so it is never a header.
        assert "authorization" not in {name.lower() for name in etherscan._client.headers}
        assert blockscout.requests_made == 0
    finally:
        for transport in (blockscout, moralis, etherscan):
            await transport.aclose()
