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
    assert to_provider("0x" + "e" * 40) == KYBER_NATIVE
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


# ------------------------------------------- the stored 0xeeee… native alias

# GeckoTerminal names Robinhood's native asset in some pools by this address,
# and existing market identities store it as recorded. It is KyberSwap's own
# sentinel, lowercased — so the answer uses it too, and must come back as it.
EEEE = "0x" + "e" * 40


async def test_a_stored_eeee_payment_asset_round_trips_as_itself():
    """MEME/EEEE: the reproduction of the #61 regression."""
    quote, request = await ask(EEEE, MEME, answer(KYBER_NATIVE, MEME))

    assert request.url.params["tokenIn"] == KYBER_NATIVE
    assert quote.token_in == EEEE
    assert quote.route.hops[0].token_in == EEEE
    assert quote.token_out == MEME


async def test_a_stored_eeee_asset_bought_round_trips_as_itself():
    """EEEE/TOKEN: the native alias is what is bought."""
    quote, request = await ask(MEME, EEEE, answer(MEME, KYBER_NATIVE))

    assert request.url.params["tokenOut"] == KYBER_NATIVE
    assert quote.token_out == EEEE
    assert quote.route.hops[-1].token_out == EEEE


async def test_a_zero_address_case_never_sees_the_eeee_alias():
    """The #61 behaviour, unchanged: zero in, zero out, route included."""
    spent, _ = await ask(NATIVE_ASSET, MEME, answer(KYBER_NATIVE, MEME))
    bought, _ = await ask(MEME, NATIVE_ASSET, answer(MEME, KYBER_NATIVE))

    assert spent.token_in == spent.route.hops[0].token_in == NATIVE_ASSET
    assert bought.token_out == bought.route.hops[-1].token_out == NATIVE_ASSET
    for quote in (spent, bought):
        assert EEEE not in quote.model_dump_json().lower()


@pytest.mark.parametrize("native", [NATIVE_ASSET, EEEE])
async def test_a_zero_address_answer_for_a_sentinel_request_is_refused(native):
    """Validated against what was sent — the sentinel — before any translation."""
    with pytest.raises(QuoteUnavailable) as error:
        await ask(native, MEME, answer(NATIVE_ASSET, MEME))
    assert error.value.failure is QuoteFailure.IDENTITY_MISMATCH


async def test_an_unrelated_token_in_the_answer_is_refused():
    with pytest.raises(QuoteUnavailable) as error:
        await ask(EEEE, MEME, answer(KYBER_NATIVE, "0x" + "99" * 20))
    assert error.value.failure is QuoteFailure.IDENTITY_MISMATCH


@pytest.mark.parametrize(("token_in", "token_out"), [(NATIVE_ASSET, EEEE), (EEEE, NATIVE_ASSET)])
async def test_native_for_native_is_refused_before_the_provider(token_in, token_out):
    """Two spellings of one asset: never sent, never a route."""
    sent: list[httpx.Request] = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, text=body())

    async with KyberSwapQuoteSource(
        settings(), transport=httpx.MockTransport(handle), clock=FixedClock(ANCHOR_TIME)
    ) as source:
        with pytest.raises(QuoteUnavailable) as error:
            await source.quote_exact_input(
                chain="robinhood",
                network="mainnet",
                token_in=token_in,
                token_out=token_out,
                token_in_decimals=18,
                token_out_decimals=18,
                amount_in=100_000_000,
            )
    assert error.value.failure is QuoteFailure.IDENTITY_MISMATCH
    assert sent == []


async def test_a_native_intermediate_hop_between_non_native_endpoints_is_the_zero_address():
    legs = [
        [hop(token_in=USDG, token_out=KYBER_NATIVE), hop(token_in=KYBER_NATIVE, token_out=NVDA)]
    ]
    quote, _ = await ask(USDG, NVDA, answer(USDG, NVDA, legs=legs))

    assert quote.route.hops[0].token_out == NATIVE_ASSET
    assert quote.route.hops[1].token_in == NATIVE_ASSET
    assert (quote.token_in, quote.token_out) == (USDG, NVDA)


async def test_a_native_intermediate_hop_takes_the_callers_alias_when_an_endpoint_is_native():
    """One native asset per trade, spelled the way the caller spelled it."""
    legs = [[hop(token_in=KYBER_NATIVE, token_out=USDG), hop(token_in=USDG, token_out=MEME)]]
    quote, _ = await ask(EEEE, MEME, answer(KYBER_NATIVE, MEME, legs=legs))
    assert quote.route.hops[0].token_in == EEEE


async def test_meme_for_meme_is_untouched():
    other = "0x" + "2b" * 20
    quote, request = await ask(MEME, other, answer(MEME, other))
    assert (request.url.params["tokenIn"], request.url.params["tokenOut"]) == (MEME, other)
    assert (quote.token_in, quote.token_out) == (MEME, other)
    assert quote.route.hops[0].token_in == MEME and quote.route.hops[-1].token_out == other
