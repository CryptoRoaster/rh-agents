"""The native asset at the KyberSwap boundary, and nowhere else.

Inside this system a chain's native asset is the zero address — in market
identities, asset ids, persistence, the scout and every quote. KyberSwap names
it `0xEeee…EEeE`. The translation happens only in the request this adapter sends
and in the answer it validates; what comes back inward is canonical again.
Nothing is recognised by symbol or name.
"""

import json

import httpx
import pytest

from src.core.clock import FixedClock
from src.markets.kyberswap import KyberSwapQuoteSource
from src.markets.kyberswap.source import KYBER_NATIVE, NATIVE_ASSET, from_provider, to_provider
from src.markets.quotes import QuoteFailure, QuoteUnavailable
from tests.anchor.test_kyberswap import ANCHOR_TIME, NVDA, USDG, body, hop, settings

MEME = "0x" + "1a" * 20


def answer(token_in, token_out, *, legs=None):
    payload = json.loads(body(legs=legs))
    summary = payload["data"]["routeSummary"]
    summary["tokenIn"], summary["tokenOut"] = token_in, token_out
    if legs is None:
        summary["route"] = [[hop(token_in=token_in, token_out=token_out)]]
    return json.dumps(payload)


async def ask(token_in, token_out, text):
    sent: list[httpx.Request] = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, text=text)

    async with KyberSwapQuoteSource(
        settings(), transport=httpx.MockTransport(handle), clock=FixedClock(ANCHOR_TIME)
    ) as source:
        quote = await source.quote_exact_input(
            chain="robinhood",
            network="mainnet",
            token_in=token_in,
            token_out=token_out,
            token_in_decimals=18,
            token_out_decimals=18,
            amount_in=100_000_000,
        )
    return quote, sent[0]


async def test_a_native_payment_asset_is_sent_as_the_sentinel_and_returns_canonical():
    """token/native: the native asset is what is spent."""
    quote, request = await ask(NATIVE_ASSET, MEME, answer(KYBER_NATIVE, MEME))

    assert request.url.params["tokenIn"] == KYBER_NATIVE
    assert request.url.params["tokenOut"] == MEME
    assert quote.token_in == NATIVE_ASSET
    assert quote.route.hops[0].token_in == NATIVE_ASSET
    assert KYBER_NATIVE.lower() not in quote.model_dump_json().lower()


async def test_a_native_asset_bought_is_sent_as_the_sentinel_and_returns_canonical():
    """native/token: the native asset is what is bought."""
    quote, request = await ask(MEME, NATIVE_ASSET, answer(MEME, KYBER_NATIVE))

    assert request.url.params["tokenIn"] == MEME
    assert request.url.params["tokenOut"] == KYBER_NATIVE
    assert quote.token_out == NATIVE_ASSET
    assert quote.route.hops[0].token_out == NATIVE_ASSET


async def test_non_native_addresses_pass_unchanged():
    quote, request = await ask(USDG, NVDA, body())
    assert (request.url.params["tokenIn"], request.url.params["tokenOut"]) == (USDG, NVDA)
    assert (quote.token_in, quote.token_out) == (USDG, NVDA)


async def test_an_answer_for_the_untranslated_address_is_refused():
    """The answer is validated against exactly what was sent."""
    with pytest.raises(QuoteUnavailable) as error:
        await ask(NATIVE_ASSET, MEME, answer(NATIVE_ASSET, MEME))
    assert error.value.failure is QuoteFailure.IDENTITY_MISMATCH


async def test_a_sentinel_answer_for_a_token_that_was_not_native_is_refused():
    with pytest.raises(QuoteUnavailable) as error:
        await ask(USDG, MEME, answer(KYBER_NATIVE, MEME))
    assert error.value.failure is QuoteFailure.IDENTITY_MISMATCH


async def test_any_two_tokens_are_asked_and_no_route_is_the_providers_answer():
    """No allowlist: a MEME/MEME request is sent, and a missing route is NO_ROUTE."""
    no_route = json.dumps({"code": 4008, "message": "route not found"})
    with pytest.raises(QuoteUnavailable) as error:
        await ask(MEME, "0x" + "2b" * 20, no_route)
    assert error.value.failure is QuoteFailure.NO_ROUTE


def test_translation_is_by_address_only():
    assert to_provider(NATIVE_ASSET) == KYBER_NATIVE
    assert to_provider(MEME) == MEME
    assert from_provider(KYBER_NATIVE) == NATIVE_ASSET
    assert from_provider(KYBER_NATIVE.lower()) == NATIVE_ASSET
    assert from_provider(MEME.upper().replace("0X", "0x")) == MEME
    source = open(
        __import__("src.markets.kyberswap.source", fromlist=["x"]).__file__, encoding="utf-8"
    ).read()
    # No asset is recognised by a symbol literal.
    for word in ('"ETH"', '"BNB"', '"WETH"', '"WBNB"', ".symbol"):
        assert word not in source, word
