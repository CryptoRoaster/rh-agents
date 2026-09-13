"""Provider contract-change detector for executable quotes. Never run by default.

Nothing here is part of the runtime path and nothing here decides anything. The
adapter's behaviour is fixed by the unit tests against a stubbed transport; this
file exists so that if KyberSwap changes what it returns, it fails loudly in a
place an operator ran on purpose rather than quietly in a live assessment.

Enabled only by ``RH_AGENTS_LIVE_QUOTE_SMOKE=1``. The public tier needs no
credential, so there is no secret here and nothing to leak. The opt-in exists
because these are real requests against somebody else's rate limit.

**Read-only, and deliberately unable to be otherwise.** ``GET /routes`` returns
amounts and a route. Building a transaction is a separate ``POST /route/build``
that this system does not call from anywhere, and the adapter has no method that
could. Nothing here signs, broadcasts, persists a response, or writes any state.

Four facts are checked, because all four are assumptions the adapter makes and
none of them are guaranteed by the provider's documentation:

1. Both supported chains answer. Most aggregators cover BSC and not Robinhood.
2. The route's endpoints are the tokens that were asked for, not something the
   router substituted.
3. Larger orders cost more per unit. If they did not, the ladder would be
   measuring noise rather than depth.
4. The response carries no calldata field the adapter might later be tempted to
   read.

Budget: at most six requests per chain.
"""

import os
from decimal import Decimal

import httpx
import pytest

from src.agents.anchor.assessment import execution_deviation_bps
from src.core.config import Settings
from src.markets.kyberswap.source import CHAIN_SLUGS, KyberSwapQuoteSource
from src.markets.quotes import QuoteFailure, QuoteUnavailable, to_base_units

LIVE = os.environ.get("RH_AGENTS_LIVE_QUOTE_SMOKE") == "1"

DATABASE = "postgresql+asyncpg://test_user@localhost/test_database"

# Pairs confirmed to route during the Phase 2J execution-liquidity research.
# Decimals are stated, never inferred: reading them wrong is what made an early
# probe look like the provider was quoting nonsense.
PAIRS = {
    "robinhood": {
        "token_in": os.environ.get(
            "RH_QUOTE_TOKEN_IN", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
        ),
        "token_in_decimals": int(os.environ.get("RH_QUOTE_TOKEN_IN_DECIMALS", "6")),
        "token_out": os.environ.get(
            "RH_QUOTE_TOKEN_OUT", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
        ),
        "token_out_decimals": int(os.environ.get("RH_QUOTE_TOKEN_OUT_DECIMALS", "18")),
    },
    "bsc": {
        "token_in": os.environ.get(
            "BSC_QUOTE_TOKEN_IN", "0x55d398326f99059ff775485246999027b3197955"
        ),
        "token_in_decimals": int(os.environ.get("BSC_QUOTE_TOKEN_IN_DECIMALS", "18")),
        "token_out": os.environ.get(
            "BSC_QUOTE_TOKEN_OUT", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
        ),
        "token_out_decimals": int(os.environ.get("BSC_QUOTE_TOKEN_OUT_DECIMALS", "18")),
    },
}

pytestmark = pytest.mark.skipif(not LIVE, reason="RH_AGENTS_LIVE_QUOTE_SMOKE is not enabled")


def settings() -> Settings:
    return Settings(_env_file=None, database_url=DATABASE)


async def quote_for(source: KyberSwapQuoteSource, chain: str, notional: Decimal):
    pair = PAIRS[chain]
    return await source.quote_exact_input(
        chain=chain,
        network="mainnet",
        token_in=pair["token_in"],
        token_out=pair["token_out"],
        token_in_decimals=pair["token_in_decimals"],
        token_out_decimals=pair["token_out_decimals"],
        amount_in=to_base_units(notional, pair["token_in_decimals"]),
    )


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_the_chain_answers_with_a_route_between_the_tokens_asked_for(chain):
    async with KyberSwapQuoteSource(settings()) as source:
        quote = await quote_for(source, chain, Decimal(100))

    assert quote.amount_out > 0
    assert quote.provider == "kyberswap"
    assert quote.chain == chain
    # The identity check that would be most expensive to miss: a route ending in
    # a different asset buys a different asset.
    assert quote.route.hops[0].token_in == quote.token_in
    assert quote.route.hops[-1].token_out == quote.token_out
    assert quote.route.hop_count >= 1
    assert quote.effective_price() is not None
    print(
        f"\n{chain}: 100 -> {quote.amount_out_human} via {quote.route.hop_count} hop(s) "
        f"across {sorted(quote.route.venues)}, block {quote.source_block_number}, "
        f"provider impact {quote.provider_price_impact_bps}"
    )


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_a_larger_order_costs_more_per_unit_than_a_small_one(chain):
    """Depth is real, and this is what makes a ladder mean anything.

    If a million-dollar order priced exactly like a hundred-dollar one, the
    ladder would be measuring the provider's rounding rather than the market's
    capacity, and every capacity figure it produced would be fiction.
    """
    async with KyberSwapQuoteSource(settings()) as source:
        small = await quote_for(source, chain, Decimal(100))
        large = await quote_for(source, chain, Decimal(1_000_000))

    small_price = small.effective_price()
    large_price = large.effective_price()
    assert small_price is not None and large_price is not None
    assert large_price > small_price, (chain, small_price, large_price)

    deviation = execution_deviation_bps(large_price, small_price)
    print(f"\n{chain}: 1,000,000 deviates {deviation} bps from the 100 reference price")
    # Not an assertion about the market, which may legitimately be thin. It is
    # an assertion that the figure is a figure and not an artefact.
    assert Decimal(0) < deviation < Decimal(10_000)


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_the_quote_endpoint_returns_no_transaction_to_sign(chain):
    """The property that keeps this adapter from being one edit from an executor.

    Checked against the raw payload rather than the parsed quote, because the
    parsed quote cannot carry calldata by construction — which would make the
    check vacuous exactly where it needs to be real.
    """
    pair = PAIRS[chain]
    async with KyberSwapQuoteSource(settings()) as source:
        raw = await source._get(  # noqa: SLF001 - inspecting the wire payload is the point
            f"/{CHAIN_SLUGS[chain]}/api/v1/routes",
            {
                "tokenIn": pair["token_in"],
                "tokenOut": pair["token_out"],
                "amountIn": str(to_base_units(Decimal(100), pair["token_in_decimals"])),
            },
        )
        quote = await quote_for(source, chain, Decimal(100))

    assert isinstance(raw, dict)
    envelope = raw.get("data")
    assert isinstance(envelope, dict)
    # `data` is the envelope's own key, so a substring search over the body says
    # nothing. The question is whether the *payload* carries an executable
    # transaction: a destination, encoded input, and a value to send.
    for forbidden in ("to", "data", "value", "transaction", "encodedSwapData", "rawTransaction"):
        assert forbidden not in envelope, f"{chain} quote payload carries {forbidden}"

    # It does publish the address a swap would be sent to. That is why the
    # adapter does not parse the field — recorded here so that if it ever stops
    # being published, this assumption is re-examined rather than assumed.
    assert "routerAddress" in envelope
    assert envelope["routerAddress"].lower() not in quote.model_dump_json().lower()
    assert quote.route.router == "kyberswap"

    # Block numbers exist, but only inside the blobs this adapter never reads,
    # and they disagree with each other inside a single hop. Recorded here so
    # that if a coherent top-level block ever appears, this is where we notice.
    hop = envelope["routeSummary"]["route"][0][0]
    assert "blockNumber" not in hop
    assert quote.source_block_number is None


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_the_provider_publishes_no_price_impact_and_one_usd_valuation(chain):
    """Two assumptions the assessment depends on, neither documented.

    If a `priceImpact` field ever appears, the impact bound stops being
    unexercised and the adapter must normalise its unit rather than pass it
    through. If `amountInUsd` disappears, the valuation cross-check silently
    stops happening. Both are worth failing loudly over.
    """
    async with KyberSwapQuoteSource(settings()) as source:
        quote = await quote_for(source, chain, Decimal(100))

    assert quote.provider_price_impact_bps is None
    assert quote.provider_amount_in_usd is not None
    intended = Decimal(100)
    skew = abs(quote.provider_amount_in_usd - intended) / intended * Decimal(10000)
    print(f"\n{chain}: provider valued $100 at {quote.provider_amount_in_usd} ({skew:.1f} bps)")
    assert skew < Decimal(500)


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_an_unserved_chain_is_a_capability_loss_not_an_empty_market(chain):
    """What must happen if the aggregator ever drops a chain we depend on.

    Robinhood is the one to watch. Reporting no liquidity for a chain that
    simply stopped being served would be a claim about the market made from a
    fact about our integration.
    """
    async with KyberSwapQuoteSource(settings()) as source:
        source._client.base_url = httpx.URL(  # noqa: SLF001 - exercising a 404 path
            "https://aggregator-api.kyberswap.com"
        )
        with pytest.raises(QuoteUnavailable) as raised:
            await source._get("/no-such-chain/api/v1/routes", {})  # noqa: SLF001

    assert raised.value.failure == QuoteFailure.UNSUPPORTED_CHAIN
    assert raised.value.is_market_fact is False


@pytest.mark.parametrize("chain", sorted(CHAIN_SLUGS))
async def test_an_impossible_pair_is_a_market_fact_and_not_an_outage(chain):
    """The distinction the whole failure taxonomy exists for.

    A pair with no route must arrive as a statement about the market. If it
    arrived as a provider failure, a genuinely unroutable asset would look like
    an outage and a retry would look like recovery.
    """
    pair = PAIRS[chain]
    async with KyberSwapQuoteSource(settings()) as source:
        with pytest.raises(QuoteUnavailable) as raised:
            await source.quote_exact_input(
                chain=chain,
                network="mainnet",
                token_in=pair["token_in"],
                token_out="0x000000000000000000000000000000000000dead",
                token_in_decimals=pair["token_in_decimals"],
                token_out_decimals=18,
                amount_in=to_base_units(Decimal(100), pair["token_in_decimals"]),
            )

    print(
        f"\n{chain}: unroutable pair -> {raised.value.failure} "
        f"(market fact: {raised.value.is_market_fact})"
    )
    assert raised.value.is_market_fact
