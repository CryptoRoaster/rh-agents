"""The two narrow reads the V4 census adds to the read-only RPC client.

`call_word` takes exactly one 32-byte argument; `event_logs` reads one
contract's logs under an explicit topic filter over a capped range. Both fail
closed on anything malformed, and a full log page is never read as complete.
"""

import json

import httpx
import pytest

from src.agents.atlas.v4.rpc import RpcV4ChainReads
from src.runtime.models import ErrorCode, RuntimeFailure
from src.runtime.rpc import MAX_EVENT_LOG_SPAN, MAX_EVENT_LOGS, EvmRpcClient

ADDRESS = "0x" + "a0" * 20
TOPIC = "0x" + "dd" * 32
OTHER = "0x" + "ee" * 32


def log(block=5, address=ADDRESS, topics=(TOPIC,), data="0x"):
    return {
        "blockNumber": hex(block),
        "blockHash": "0x" + "01" * 32,
        "transactionHash": "0x" + "02" * 32,
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "address": address,
        "topics": list(topics),
        "data": data,
        "removed": False,
    }


def client(chain_config, settings, result, seen=None):
    def handler(request):
        payload = json.loads(request.content)
        if seen is not None:
            seen.append(payload)
        if payload["method"] == "eth_chainId":
            value = hex(chain_config.chain_id)
        else:
            value = result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": value})

    return EvmRpcClient(chain_config, settings, transport=httpx.MockTransport(handler))


async def test_call_word_sends_selector_and_one_word(chain_config, settings):
    seen: list = []
    rpc = client(chain_config, settings, "0x" + "00" * 31 + "07", seen)
    try:
        await rpc.verify_chain()
        result = await rpc.call_word(ADDRESS, "0x6352211e", "0x" + "00" * 31 + "2a", 9)
        assert result.endswith("07")
        call = seen[-1]
        assert call["method"] == "eth_call"
        assert call["params"] == [
            {"to": ADDRESS, "data": "0x6352211e" + "00" * 31 + "2a"},
            hex(9),
        ]
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "selector, argument",
    [("0x6352211", "0x" + "00" * 32), ("0x6352211e", "0x2a"), ("0x6352211e", "0x" + "GG" * 32)],
)
async def test_call_word_refuses_anything_but_one_word(chain_config, settings, selector, argument):
    rpc = client(chain_config, settings, "0x")
    try:
        await rpc.verify_chain()
        with pytest.raises(RuntimeFailure) as refused:
            await rpc.call_word(ADDRESS, selector, argument, 1)
        assert refused.value.code is ErrorCode.CONFIGURATION
    finally:
        await rpc.close()


async def test_event_logs_filter_and_order(chain_config, settings):
    seen: list = []
    rpc = client(
        chain_config, settings, [log(7, topics=(TOPIC, OTHER)), log(5, topics=(TOPIC, OTHER))], seen
    )
    try:
        await rpc.verify_chain()
        found = await rpc.event_logs(ADDRESS, (TOPIC, None), 1, 10)
        assert [item.block_number for item in found] == [5, 7]
        assert seen[-1]["params"] == [
            {"address": ADDRESS, "topics": [TOPIC, None], "fromBlock": "0x1", "toBlock": "0xa"}
        ]
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "result",
    [
        [log(address="0x" + "b0" * 20)],
        [log(topics=(OTHER,))],
        [log(block=50)],
        "0x",
        [log()] * MAX_EVENT_LOGS,
    ],
)
async def test_event_logs_refuse_what_was_not_asked_or_may_be_cut(chain_config, settings, result):
    rpc = client(chain_config, settings, result)
    try:
        await rpc.verify_chain()
        with pytest.raises(RuntimeFailure) as refused:
            await rpc.event_logs(ADDRESS, (TOPIC,), 1, 10)
        assert refused.value.code is ErrorCode.CONTRACT
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "topics, start, end",
    [
        ((None,), 1, 2),
        ((), 1, 2),
        ((TOPIC,), 5, 4),
        ((TOPIC,), 0, MAX_EVENT_LOG_SPAN),
        ((TOPIC, "0x12"), 1, 2),
    ],
)
async def test_event_logs_refuse_unbounded_or_malformed_filters(
    chain_config, settings, topics, start, end
):
    rpc = client(chain_config, settings, [])
    try:
        await rpc.verify_chain()
        with pytest.raises(RuntimeFailure) as refused:
            await rpc.event_logs(ADDRESS, topics, start, end)
        assert refused.value.code is ErrorCode.CONFIGURATION
    finally:
        await rpc.close()


async def test_the_census_port_adapts_logs(chain_config, settings):
    rpc = client(chain_config, settings, [log(5, data="0x" + "00" * 32)])
    try:
        reads = RpcV4ChainReads(rpc)
        assert await reads.chain_id() == chain_config.chain_id
        (item,) = await reads.logs(ADDRESS, (TOPIC,), 1, 10)
        assert item.block_number == 5 and item.topics == (TOPIC,)
    finally:
        await rpc.close()
