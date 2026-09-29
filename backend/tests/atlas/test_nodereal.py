"""NodeReal: BNB Smart Chain holder facts from two documented JSON-RPC methods.

`nr_getTokenHolders` with an explicit `topN`, and `nr_getTokenHolderCount`.
Everything a provider answers is checked rather than trusted: the envelope, the
order, the rows, the count. No provider percentage or supply is used, no block
or timestamp is invented, and the key — part of the documented URL path — never
reaches a result or a failure.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from src.agents.atlas.context import AtlasSnapshotBuilder
from src.agents.atlas.models import (
    AtlasSourceFailure,
    HolderCompleteness,
    HolderObservationBasis,
)
from src.agents.atlas.sources.nodereal import NodeRealConfig, NodeRealHolderSource
from src.agents.atlas.sources.routing import RoutedHolderSource
from src.core.clock import FixedClock
from src.markets.models import Availability
from tests.atlas.conftest import (
    TOKEN,
    StubContracts,
    StubOrigins,
    builder_for,
    chain_snapshot,
    contract_facts,
    market_identity,
)
from tests.atlas.conftest import origin_facts as origins
from tests.atlas.fake_http import RecordingRoutes, json_response

OBSERVED = datetime(2026, 9, 29, 7, tzinfo=UTC)
KEY = "nodereal_testkey"
CONFIG = NodeRealConfig(base_url="https://bsc-mainnet.nodereal.test/v1", api_key=KEY, top_n=20)


def holder(index: int, balance: int) -> dict[str, str]:
    return {"accountAddress": "0x" + f"{index:02x}" * 20, "tokenBalance": hex(balance)}


def holders(count: int, *, top: int = 5 * 10**22, step: int = 10**20):
    return [holder(index + 1, top - index * step) for index in range(count)]


class NodeReal:
    """A JSON-RPC stand-in answering by method, recording every call."""

    def __init__(self, details, count, *, envelope=None) -> None:
        self.details = details
        self.count = count
        self.envelope = envelope or {}
        self.calls: list[dict] = []
        self.routes = RecordingRoutes({f"/{KEY}": self.answer})

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        result = (
            {"pageKey": "", "details": self.details}
            if body["method"] == "nr_getTokenHolders"
            else self.count
        )
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        payload.update(self.envelope.get(body["method"], {}))
        return json_response(payload)


def source(fake: NodeReal) -> NodeRealHolderSource:
    return NodeRealHolderSource(
        config=CONFIG,
        clock=FixedClock(OBSERVED),
        transport_factory=fake.routes.transport_factory(),
    )


# ------------------------------------------------------- available


async def test_sorted_top_n_and_a_count_are_a_top_n_fact():
    fake = NodeReal(holders(20), hex(4200))
    result = await source(fake).holder_facts("bsc", TOKEN)
    assert result.status == Availability.AVAILABLE, result
    assert result.completeness == HolderCompleteness.TOP_N_ONLY
    assert (result.chain, result.token_address) == ("bsc", TOKEN)
    assert result.holder_count == 4200
    assert result.rows[0].balance_raw == 5 * 10**22
    assert len(result.rows) == 20
    # Honest provenance: the receipt, no block, no provider supply.
    assert result.observation_basis == HolderObservationBasis.RESPONSE_TIME
    assert result.snapshot_timestamp == OBSERVED
    assert result.snapshot_block is None
    assert result.provider_total_supply_raw is None
    assert result.requests_made == 2


async def test_the_documented_methods_ask_for_top_n_explicitly():
    fake = NodeReal(holders(20), hex(4200))
    await source(fake).holder_facts("bsc", TOKEN)
    page, count = fake.calls
    assert page["method"] == "nr_getTokenHolders"
    assert page["params"] == [TOKEN, hex(20), "", hex(20)]
    assert count == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "nr_getTokenHolderCount",
        "params": [TOKEN],
    }
    # The key is where NodeReal documents it: the path, sent to that host only.
    assert all(request.url.path.endswith(f"/{KEY}") for request in fake.routes.requests)
    assert all(request.method == "POST" for request in fake.routes.requests)


async def test_a_count_equal_to_the_rows_proves_the_complete_set():
    fake = NodeReal(holders(7), hex(7))
    result = await source(fake).holder_facts("bsc", TOKEN)
    assert result.status == Availability.AVAILABLE
    assert result.completeness == HolderCompleteness.COMPLETE
    assert result.holder_count == 7


async def test_a_bsc_holder_read_flows_through_the_atlas_builder():
    """Normalised against the on-chain supply the builder reads, like every chain."""
    fake = NodeReal(holders(20, top=5 * 10**22, step=10**21), hex(4200))
    bsc = market_identity("bsc")

    builder = builder_for(OBSERVED, chain=chain_snapshot(OBSERVED, chain="bsc", chain_id=56))
    builder = type(builder)(
        contracts=builder.contracts,
        holders=RoutedHolderSource({"bsc": source(fake)}),
        origins=builder.origins,
        clock=FixedClock(OBSERVED),
    )

    snapshot = await builder.build(uuid4(), uuid4(), bsc)
    assert snapshot.holders.status == Availability.AVAILABLE
    assert snapshot.holders.holder_count == 4200
    assert snapshot.holders.top10_share is not None
    assert snapshot.holders.excluded_addresses == ()


# ------------------------------------------------------- fail closed


async def test_fewer_than_ten_rows_that_are_not_the_whole_set_fail_closed():
    fake = NodeReal(holders(9), hex(4200))
    result = await source(fake).holder_facts("bsc", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.INCOMPLETE_RESULT
    assert result.rows == ()


async def test_an_unordered_list_fails_closed():
    rows = holders(20)
    rows[3], rows[4] = rows[4], rows[3]
    result = await source(NodeReal(rows, hex(4200))).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_count_below_the_rows_fails_closed():
    result = await source(NodeReal(holders(20), hex(12))).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize("chain", ["robinhood", "ethereum"])
async def test_another_chain_fails_closed_without_a_request(chain):
    fake = NodeReal(holders(20), hex(4200))
    result = await source(fake).holder_facts(chain, TOKEN)
    assert result.failure == AtlasSourceFailure.UNSUPPORTED_CHAIN
    assert fake.calls == []


async def test_a_malformed_token_fails_closed_without_a_request():
    fake = NodeReal(holders(20), hex(4200))
    result = await source(fake).holder_facts("bsc", "not-an-address")
    assert result.failure == AtlasSourceFailure.TOKEN_MISMATCH
    assert fake.calls == []


async def test_rows_for_another_token_are_refused_by_the_builder():
    """The source answers for what it was asked; the builder checks the answer."""

    class OtherToken:
        async def holder_facts(self, chain, token_address):
            result = await source(NodeReal(holders(20), hex(4200))).holder_facts(chain, TOKEN)
            return result.model_copy(update={"token_address": "0x" + "ee" * 20})

    builder = AtlasSnapshotBuilder(
        contracts=StubContracts(
            chain_snapshot(OBSERVED, chain="bsc", chain_id=56), contract_facts()
        ),
        holders=OtherToken(),
        origins=StubOrigins(origins()),
        clock=FixedClock(OBSERVED),
    )

    snapshot = await builder.build(uuid4(), uuid4(), market_identity("bsc"))
    assert snapshot.holders.status == Availability.UNAVAILABLE
    assert snapshot.holders.failure == AtlasSourceFailure.TOKEN_MISMATCH


@pytest.mark.parametrize(
    "envelope,failure",
    [
        ({"nr_getTokenHolders": {"id": 99}}, AtlasSourceFailure.INVALID_RESPONSE),
        ({"nr_getTokenHolders": {"jsonrpc": "1.0"}}, AtlasSourceFailure.INVALID_RESPONSE),
        (
            {"nr_getTokenHolderCount": {"error": {"code": -32000, "message": "limit"}}},
            AtlasSourceFailure.UNAVAILABLE,
        ),
    ],
    ids=["foreign-id", "wrong-version", "provider-error"],
)
async def test_an_envelope_that_is_not_this_answer_fails_closed(envelope, failure):
    result = await source(NodeReal(holders(20), hex(4200), envelope=envelope)).holder_facts(
        "bsc", TOKEN
    )
    assert result.failure == failure


@pytest.mark.parametrize(
    "mutation",
    [
        {"tokenBalance": "12"},
        {"tokenBalance": 12},
        {"tokenBalance": "0x" + "f" * 65},
        {"accountAddress": "0x1234"},
    ],
    ids=["decimal-string", "number", "over-uint256", "short-address"],
)
async def test_malformed_rows_fail_closed(mutation):
    rows = holders(20)
    rows[0] = {**rows[0], **mutation}
    result = await source(NodeReal(rows, hex(4200))).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_a_duplicate_holder_fails_closed():
    rows = holders(20)
    rows[1] = {**rows[1], "accountAddress": rows[0]["accountAddress"]}
    result = await source(NodeReal(rows, hex(4200))).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_the_key_never_reaches_a_result_or_a_failure():
    def refuse(request):
        return httpx.Response(403, content=b"{}", headers={"Content-Type": "application/json"})

    fake = NodeReal(holders(20), hex(4200))
    fake.routes.routes = {f"/{KEY}": refuse}
    result = await source(fake).holder_facts("bsc", TOKEN)
    assert result.failure == AtlasSourceFailure.NOT_CONFIGURED
    assert KEY not in result.model_dump_json()


def test_top_n_stays_within_the_documented_page():
    with pytest.raises(ValueError):
        NodeRealConfig(base_url="https://bsc-mainnet.nodereal.test/v1", api_key=KEY, top_n=101)
    with pytest.raises(ValueError):
        NodeRealConfig(base_url="https://bsc-mainnet.nodereal.test/v1", api_key=KEY, top_n=9)


# ------------------------------------------------------- configuration


def settings(**overrides):
    from src.core.config import Settings

    return Settings(
        _env_file=None, database_url="postgresql+asyncpg:///rh_agents_test?host=/tmp", **overrides
    )


def test_nodereal_is_selectable_for_bsc_and_routes_there_alone():
    from src.agents.atlas.sources.blockscout import BlockscoutHolderSource
    from src.agents.atlas.sources.factory import holder_sources

    routed = holder_sources(
        settings(
            atlas_bsc_holder_provider="nodereal",
            nodereal_api_key="k",
            atlas_rh_holder_provider="blockscout",
            blockscout_api_key="k",
        )
    )
    assert isinstance(routed.sources["bsc"], NodeRealHolderSource)
    assert routed.sources["bsc"].config.top_n == 50
    # Robinhood's routing is untouched.
    assert isinstance(routed.sources["robinhood"], BlockscoutHolderSource)


def test_moralis_is_unchanged_and_not_the_default():
    from src.agents.atlas.sources.factory import holder_sources
    from src.agents.atlas.sources.moralis import MoralisHolderSource

    assert settings().atlas_bsc_holder_provider == "disabled"
    routed = holder_sources(settings(atlas_bsc_holder_provider="moralis", moralis_api_key="k"))
    assert isinstance(routed.sources["bsc"], MoralisHolderSource)


def test_a_missing_nodereal_key_is_refused_with_a_stable_code():
    from pydantic import ValidationError

    from src.core.config import ATLAS_PROVIDER_KEY_MISSING, settings_refusal

    with pytest.raises(ValidationError) as caught:
        settings(atlas_bsc_holder_provider="nodereal")
    messages = [str(item.get("msg", "")) for item in caught.value.errors()]
    assert settings_refusal(messages) == ATLAS_PROVIDER_KEY_MISSING


@pytest.mark.parametrize(
    "url",
    [
        "https://eth-mainnet.nodereal.io/v1",
        "http://bsc-mainnet.nodereal.io/v1",
        "https://bsc-mainnet.nodereal.io/v1/secret?x=1",
    ],
)
def test_the_nodereal_origin_is_the_bsc_host_only(url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(nodereal_base_url=url)
