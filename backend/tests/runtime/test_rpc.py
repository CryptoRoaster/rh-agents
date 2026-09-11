import json
import logging
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from src.runtime.models import ErrorCode, Head, Log, RuntimeFailure, SubscriptionSpec, quantity
from src.runtime.rpc import EvmRpcClient, decode


@pytest.mark.parametrize(
    "value", [None, True, 56, 1.5, "0x", "0x00", "56", "0xgg", "0x8000000000000000"]
)
def test_quantity_rejects(value):
    with pytest.raises(RuntimeFailure):
        quantity(value)


def test_exact_json():
    assert decode('{"value":0.00000000000000000000000000000123}')["value"] == Decimal("1.23e-30")


@pytest.mark.parametrize("body", ["{", '{"a":1,"a":2}', '{"a":NaN}', "x" * 2_000_001])
def test_json_rejects(body):
    with pytest.raises(RuntimeFailure):
        decode(body)


async def test_chain_verified(chain_config, settings):
    methods = []

    def handler(req):
        payload = json.loads(req.content)
        methods.append(payload["method"])
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": hex(chain_config.chain_id if payload["method"] == "eth_chainId" else 123),
            },
        )

    rpc = EvmRpcClient(chain_config, settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeFailure):
            await rpc.block_number()
        assert await rpc.verify_chain() == chain_config.chain_id
        assert await rpc.block_number() == 123
        assert methods == ["eth_chainId", "eth_blockNumber"]
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, ErrorCode.CHAIN_ID_MISMATCH),
        ({"jsonrpc": "2.0", "id": 2, "result": "0x38"}, ErrorCode.CONTRACT),
        ({"jsonrpc": "2.0", "id": True, "result": "0x38"}, ErrorCode.CONTRACT),
        ({"jsonrpc": "2.0", "id": 1, "result": True}, ErrorCode.CONTRACT),
        ({"jsonrpc": "2.0", "id": 1, "error": {"message": "secret-token"}}, ErrorCode.RPC_ERROR),
        ({"id": 1, "result": "0x38"}, ErrorCode.CONTRACT),
    ],
)
async def test_fail_closed(chain_config, settings, payload, code):
    rpc = EvmRpcClient(
        chain_config,
        settings,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(RuntimeFailure) as caught:
            await rpc.verify_chain()
        assert caught.value.code == code
        assert "secret-token" not in str(caught.value)
        assert not rpc.verified
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "status,attempts,code",
    [
        (400, 1, ErrorCode.CLIENT),
        (401, 1, ErrorCode.AUTHENTICATION),
        (403, 1, ErrorCode.AUTHENTICATION),
        (429, 3, ErrorCode.RATE_LIMITED),
        (500, 3, ErrorCode.UNAVAILABLE),
        (503, 3, ErrorCode.UNAVAILABLE),
        (501, 1, ErrorCode.UNAVAILABLE),
    ],
)
async def test_http_retries(chain_config, settings, status, attempts, code, caplog):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    caplog.set_level(logging.DEBUG)
    rpc = EvmRpcClient(
        chain_config,
        settings,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, headers={"Retry-After": "9999999999"}, text="secret-token"
            )
        ),
        sleep=sleep,
    )
    try:
        with pytest.raises(RuntimeFailure) as caught:
            await rpc.verify_chain()
        assert caught.value.code == code
        assert rpc.attempts == attempts
        assert waits == [settings.evm_max_retry_delay_seconds] * (attempts - 1)
        assert "secret-token" not in caplog.text + str(caught.value)
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "exception,code",
    [(httpx.ReadTimeout, ErrorCode.TIMEOUT), (httpx.ConnectError, ErrorCode.CONNECTIVITY)],
)
async def test_network_retries(chain_config, settings, exception, code):
    def handler(req):
        raise exception("secret-token", request=req)

    async def sleep(_):
        pass

    rpc = EvmRpcClient(chain_config, settings, transport=httpx.MockTransport(handler), sleep=sleep)
    try:
        with pytest.raises(RuntimeFailure) as caught:
            await rpc.verify_chain()
        assert caught.value.code == code and rpc.attempts == 3
        assert "secret-token" not in str(caught.value)
    finally:
        await rpc.close()


@pytest.mark.parametrize(
    "addresses,topics",
    [
        ([], ["0x" + "11" * 32]),
        (["0x" + "11" * 32], ["0x" + "11" * 32]),
        (["0x" + "00" * 20], ["0x" + "11" * 32]),
        (["0x" + "11" * 20], [None]),
        (["0x" + "11" * 20], ["0x11"]),
    ],
)
def test_subscription_allowlist(addresses, topics):
    with pytest.raises(ValidationError):
        SubscriptionSpec(chain="bsc", addresses=addresses, topics=topics, decoder="test")


@pytest.mark.parametrize("schema", [Head, Log])
def test_invalid_records(schema):
    with pytest.raises(RuntimeFailure):
        schema.from_rpc({})


@pytest.mark.parametrize(
    "url",
    [
        "secret-token",
        "ftp://example.test",
        "https://",
        "https://example.test:999999",
        "https://example.test/path with space",
    ],
)
def test_config_rejects_malformed_secret_url(chain_config, url):
    from src.runtime.models import ChainConfig

    values = chain_config.model_dump()
    values["http_url"] = url
    with pytest.raises(ValidationError) as caught:
        ChainConfig.model_validate(values)
    assert url not in str(caught.value).split("For further information")[0]
    assert "input_value" not in str(caught.value)


async def test_block_and_logs_contract(chain_config, settings):
    address = "0x" + "11" * 20
    topic = "0x" + "22" * 32
    block_hash = "0x" + "33" * 32

    def handler(request):
        req = json.loads(request.content)
        if req["method"] == "eth_chainId":
            result = hex(chain_config.chain_id)
        elif req["method"] == "eth_getBlockByNumber":
            assert req["params"] == ["0xa", False]
            result = {
                "number": "0xa",
                "hash": block_hash,
                "parentHash": "0x" + "44" * 32,
                "timestamp": "0x1",
            }
        else:
            assert req["method"] == "eth_getLogs"
            assert req["params"][0]["address"] == [address]
            result = [
                {
                    "blockNumber": "0xa",
                    "blockHash": block_hash,
                    "transactionHash": "0x" + "55" * 32,
                    "transactionIndex": "0x0",
                    "logIndex": "0x0",
                    "address": address,
                    "topics": [topic],
                    "data": "0x",
                    "removed": False,
                }
            ]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req["id"], "result": result})

    rpc = EvmRpcClient(chain_config, settings, transport=httpx.MockTransport(handler))
    try:
        await rpc.verify_chain()
        assert (await rpc.block(10)).hash == block_hash
        spec = SubscriptionSpec(
            chain=chain_config.chain, addresses=(address,), topics=(topic,), decoder="test"
        )
        assert len(await rpc.logs(spec, 10, 10)) == 1
        with pytest.raises(RuntimeFailure):
            await rpc.logs(spec, 10, 10000)
    finally:
        await rpc.close()
