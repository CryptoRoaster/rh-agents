"""The KyberSwap adapter, against the response shape the live API actually returns.

The fixtures below are the real shape, captured during this phase's provider
research: `routeSummary` with base-unit amounts and a `route` of legs, a
`routerAddress`, a provider timestamp, and — importantly — no calldata.

The `routerAddress` is present in the fixtures on purpose even though the
adapter does not parse it. It is the contract a swap would be sent to, and the
tests below assert that it does not survive into a quote.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from src.core.clock import FixedClock
from src.core.config import Settings
from src.markets.kyberswap import CHAIN_SLUGS, KyberSwapQuoteSource
from src.markets.quotes import QuoteFailure, QuoteUnavailable

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NVDA = "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
ANCHOR_TIME = datetime(2026, 9, 13, 12, tzinfo=UTC)


def settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        **overrides,
    )


def hop(token_in=USDG, token_out=NVDA, amount_in="100000000", amount_out="463447000000000000"):
    """One leg, with the provider-internal blobs the live API also sends."""
    return {
        "pool": "0x" + "11" * 20,
        "exchange": "uniswap-v4-fee",
        "tokenIn": token_in,
        "tokenOut": token_out,
        "swapAmount": amount_in,
        "amountOut": amount_out,
        "poolType": "uniswap-v4",
        # Execution machinery the provider sends for its own builder. Nothing
        # here may travel onward.
        "extra": {"HookSwapInfo": {}, "_cs": "18359255153402957107"},
        "poolExtra": {
            "blockNumber": 62130537,
            "hookAddress": "0x" + "cc" * 20,
            "hookData": "0xdeadbeef",
            "permit2Addr": "0x" + "dd" * 20,
        },
    }


def body(*, code=0, legs=None, amount_in="100000000", amount_out="463447000000000000", **summary):
    payload = {
        "code": code,
        "message": "successfully",
        "requestId": "abc",
        "data": {
            "routerAddress": "0x" + "ab" * 20,
            "routeSummary": {
                "tokenIn": USDG,
                "tokenOut": NVDA,
                "amountIn": amount_in,
                "amountOut": amount_out,
                "amountInUsd": "100.0",
                "amountOutUsd": "100.01",
                "gas": "300000",
                "gasUsd": "0.23",
                "route": legs
                if legs is not None
                else [[hop(amount_in=amount_in, amount_out=amount_out)]],
                "timestamp": int(ANCHOR_TIME.timestamp()),
                "routeID": "r-1",
                "checksum": "c-1",
                **summary,
            },
        },
    }
    return json.dumps(payload)


async def quote(text=None, *, status=200, chain="robinhood", amount_in=100_000_000, capture=None):
    def handle(request):
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, text=text if text is not None else body())

    async with KyberSwapQuoteSource(
        settings(), transport=httpx.MockTransport(handle), clock=FixedClock(ANCHOR_TIME)
    ) as source:
        return await source.quote_exact_input(
            chain=chain,
            network="mainnet",
            token_in=USDG,
            token_out=NVDA,
            token_in_decimals=6,
            token_out_decimals=18,
            amount_in=amount_in,
        )


# ------------------------------------------------------- a good quote


async def test_a_route_becomes_a_typed_quote():
    result = await quote()
    assert result.provider == "kyberswap"
    assert result.chain == "robinhood"
    assert result.amount_in == 100_000_000
    assert result.amount_out == 463_447_000_000_000_000
    assert result.token_in == USDG and result.token_out == NVDA
    assert result.quoted_at == ANCHOR_TIME
    # The endpoint states no block, so none is claimed.
    assert result.source_block_number is None
    assert result.provider_price_impact_bps is None


async def test_the_effective_price_uses_the_supplied_decimals():
    """A hundred dollars of a six-decimal asset, not a hundred trillion."""
    result = await quote()
    assert result.amount_in_human == Decimal("100")
    assert result.effective_price() is not None
    # 100 USDG for 0.463447 NVDA is about 215.77 a share.
    assert Decimal("215") < result.effective_price() < Decimal("217")


async def test_the_request_names_exactly_the_assets_and_amount():
    capture: list[httpx.Request] = []
    await quote(capture=capture)
    request = capture[-1]
    assert request.url.path == "/robinhood/api/v1/routes"
    assert request.url.params["tokenIn"] == USDG
    assert request.url.params["tokenOut"] == NVDA
    assert request.url.params["amountIn"] == "100000000"
    assert "authorization" not in request.headers


# ------------------------------------------ no calldata reaches the system


async def test_provider_execution_machinery_is_discarded():
    """Scenario U. Hook data and permit addresses must not survive the boundary.

    The quote endpoint returns no transaction, but its route legs carry the
    material a builder would use. None of it travels onward: ANCHOR is not the
    executor and must not carry the means to become one.
    """
    result = await quote()
    rendered = result.model_dump_json()
    for forbidden in (
        "hookData",
        "hookAddress",
        "permit2Addr",
        "poolExtra",
        "extra",
        "deadbeef",
        "_cs",
        "calldata",
    ):
        assert forbidden not in rendered
    leg = result.route.hops[0]
    assert set(leg.model_dump()) == {
        "venue",
        "pool",
        "token_in",
        "token_out",
        "amount_in",
        "amount_out",
    }


async def test_a_transaction_payload_would_not_survive_either():
    """Even if the provider started returning one, nothing reads it."""
    payload = json.loads(body())
    payload["data"]["transaction"] = {
        "to": "0x" + "ee" * 20,
        "data": "0xdeadbeefcafe",
        "value": "0",
    }
    result = await quote(json.dumps(payload))
    rendered = result.model_dump_json()
    assert "deadbeefcafe" not in rendered
    assert "transaction" not in rendered


async def test_the_router_address_does_not_survive_into_a_quote():
    """The destination of a swap is a `to`, and it is discarded like the rest.

    Found by the opt-in live smoke rather than by reading the documentation: the
    quote endpoint publishes no calldata but does publish the contract a swap
    would be sent to. Nothing downstream ever read it, so keeping it would have
    been a liability that bought nothing. The route names its router instead.
    """
    payload = json.loads(body())
    address = payload["data"]["routerAddress"]
    assert address.startswith("0x")

    result = await quote(json.dumps(payload))
    rendered = result.model_dump_json()
    assert address.lower() not in rendered.lower()
    assert "routerAddress" not in rendered
    assert result.route.router == "kyberswap"


# ------------------------------------------------- market facts vs outages


@pytest.mark.parametrize("code", [4005, 4008, 4011])
async def test_a_no_route_code_is_a_market_fact_even_though_it_arrives_as_a_400(code):
    """The classification the opt-in live smoke corrected.

    This provider states a refusal as HTTP 400 with the reason in the body, so a
    status-only rule would have made every unroutable pair look like an outage —
    and, worse, would have made a later retry look like the market recovering.
    The typed code decides; the status only says a body is worth reading.
    """
    payload = json.dumps({"code": code, "message": "route not found", "data": None})
    with pytest.raises(QuoteUnavailable) as error:
        await quote(payload, status=400)
    assert error.value.failure == QuoteFailure.NO_ROUTE
    assert error.value.is_market_fact is True


async def test_an_unrecognised_refusal_is_not_promoted_to_a_market_fact():
    """A 400 this adapter does not understand says nothing about liquidity."""
    payload = json.dumps({"code": 4999, "message": "something new", "data": None})
    with pytest.raises(QuoteUnavailable) as error:
        await quote(payload, status=400)
    assert error.value.failure == QuoteFailure.INVALID_RESPONSE
    assert error.value.is_market_fact is False


async def test_an_unreadable_refusal_is_not_promoted_to_a_market_fact():
    with pytest.raises(QuoteUnavailable) as error:
        await quote("<html>Bad Request</html>", status=400)
    assert error.value.failure == QuoteFailure.INVALID_RESPONSE
    assert error.value.is_market_fact is False


async def test_an_empty_route_is_a_market_fact():
    with pytest.raises(QuoteUnavailable) as error:
        await quote(body(legs=[]))
    assert error.value.failure == QuoteFailure.NO_ROUTE


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (429, QuoteFailure.RATE_LIMITED),
        (500, QuoteFailure.PROVIDER_UNAVAILABLE),
        (503, QuoteFailure.PROVIDER_UNAVAILABLE),
        (403, QuoteFailure.PROVIDER_UNAVAILABLE),
        (404, QuoteFailure.INVALID_RESPONSE),
        (418, QuoteFailure.INVALID_RESPONSE),
    ],
)
async def test_a_provider_failure_is_never_an_empty_market(status, failure):
    """Scenario F. A rate limit is weather, not a measurement."""
    with pytest.raises(QuoteUnavailable) as error:
        await quote(status=status)
    assert error.value.failure == failure
    assert error.value.is_market_fact is False


async def test_a_timeout_is_an_absence_of_evidence():
    def handle(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with KyberSwapQuoteSource(
        settings(), transport=httpx.MockTransport(handle), clock=FixedClock(ANCHOR_TIME)
    ) as source:
        with pytest.raises(QuoteUnavailable) as error:
            await source.quote_exact_input(
                chain="bsc",
                network="mainnet",
                token_in=USDG,
                token_out=NVDA,
                token_in_decimals=6,
                token_out_decimals=18,
                amount_in=1,
            )
    assert error.value.failure == QuoteFailure.TIMEOUT


@pytest.mark.parametrize("text", ["not json", "[]", '{"code":0}'])
async def test_a_malformed_response_is_refused(text):
    with pytest.raises(QuoteUnavailable) as error:
        await quote(text)
    assert error.value.failure == QuoteFailure.INVALID_RESPONSE


# ------------------------------------------------------------ identity


async def test_a_quote_for_another_asset_is_refused():
    payload = json.loads(body())
    payload["data"]["routeSummary"]["tokenOut"] = "0x" + "ff" * 20
    with pytest.raises(QuoteUnavailable) as error:
        await quote(json.dumps(payload))
    assert error.value.failure == QuoteFailure.IDENTITY_MISMATCH


async def test_a_quote_for_another_amount_is_refused():
    """The offer must be for the size that was asked about."""
    with pytest.raises(QuoteUnavailable) as error:
        await quote(body(amount_in="999"))
    assert error.value.failure == QuoteFailure.IDENTITY_MISMATCH


async def test_a_route_that_ends_elsewhere_is_refused():
    """The last leg must deliver the asset being bought."""
    payload = json.loads(body())
    payload["data"]["routeSummary"]["route"] = [[hop(token_out="0x" + "ff" * 20)]]
    with pytest.raises(QuoteUnavailable) as error:
        await quote(json.dumps(payload))
    assert error.value.failure == QuoteFailure.INVALID_RESPONSE


@pytest.mark.parametrize("chain", ["ethereum", "polygon", "solana"])
async def test_an_unsupported_chain_is_refused_before_any_request(chain):
    with pytest.raises(QuoteUnavailable) as error:
        await quote(chain=chain)
    assert error.value.failure == QuoteFailure.UNSUPPORTED_CHAIN


def test_only_the_two_supported_chains_are_mapped():
    """Verified live on both. No chain is included on a claim of generic EVM support."""
    assert set(CHAIN_SLUGS) == {"robinhood", "bsc"}


# --------------------------------------------------------- configuration


def test_the_provider_needs_no_credential():
    configured = settings()
    assert configured.execution_quote_provider == "disabled"
    rendered = repr(Settings.model_fields).lower()
    assert "kyberswap_api_key" not in rendered
    assert "kyberswap_secret" not in rendered


def test_the_client_trusts_no_environment_and_follows_no_redirect():
    import inspect

    source_text = inspect.getsource(KyberSwapQuoteSource.__init__)
    assert "trust_env=False" in source_text
    assert "follow_redirects=False" in source_text
