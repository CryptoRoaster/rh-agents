"""ANCHOR for any pair: the payment asset is valued from the case's own pool.

Each test here records real, normalized provider answers through the production
adapter and recorder, reads them back through the production `MarketReader`,
and runs the production `AnchorContextReader` over them. The case's pool is the
only market that exists. ANCHOR must convert its USD ladder into the pool's own
quote asset using the `quote_price` of that same pool — and ask for nothing
else. No quote asset is special: not a stablecoin, not a wrapped native token.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

import pytest

from src.agents.anchor.context import AnchorContextReader
from src.agents.anchor.ports import AnchorContextUnavailable
from src.core.clock import FixedClock
from src.markets.fake_quotes import FixtureQuoteSource
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from tests.anchor.conftest import StubCases, StubTradeCase, triggered_pair
from tests.markets.test_quote_price import observe
from tests.runner.provider import pool

MEME_A = "0x" + "1a" * 20
MEME_B = "0x" + "2b" * 20
TOKEN = "0x" + "a1" * 20
TOKEN_X = "0x" + "c3" * 20
WRAPPED = "0x" + "3c" * 20
STABLE = "0x" + "4d" * 20
NATIVE = "0x" + "00" * 20


@dataclass
class WatchedMarkets:
    """The production reader, with a note of every identity ANCHOR asks for."""

    inner: MarketReader
    asked: list[str] = field(default_factory=list)

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        self.asked.append(identity)
        return await self.inner.latest(identity, include_fixtures=include_fixtures)


@dataclass
class WatchedQuotes(FixtureQuoteSource):
    """The fixture quote source, keeping each request's tokens and amount."""

    requests: list[dict[str, object]] = field(default_factory=list)

    async def quote_exact_input(self, **kwargs):  # type: ignore[override]
        self.requests.append(kwargs)
        return await super().quote_exact_input(**kwargs)


async def anchor_on(now, sessions, answer, *, case_market=None, quotes_kw=None):
    """Record one pool, then run ANCHOR over it as the case's market."""
    (snapshot,) = await observe(now, [answer])
    await MarketRecorder(sessions, clock=FixedClock(now)).record(snapshot)
    markets = WatchedMarkets(MarketReader(sessions, clock=FixedClock(now)))
    base_usd = snapshot.price.value_usd or Decimal(1)
    quote_usd = (
        snapshot.quote_price.value_usd
        if snapshot.quote_price is not None and snapshot.quote_price.value_usd is not None
        else Decimal(1)
    )
    quotes = WatchedQuotes(
        reference_price=base_usd,
        quote_asset_usd_price=quote_usd,
        quoted_at=now - timedelta(seconds=1),
        **(quotes_kw or {}),
    )
    trade_case = StubTradeCase(case_market or snapshot.pair.market_identity)
    reader = AnchorContextReader(
        cases=StubCases(trade_case, triggered_pair(now)),
        markets=markets,
        quotes=quotes,
        clock=FixedClock(now),
    )
    return snapshot, markets, quotes, reader


# ------------------------------------------------------- the acceptance condition


async def test_meme_a_meme_b_is_quoted_in_meme_b_without_a_second_market(now, market_sessions):
    """DOG_A/DOG_B: $0.004 and $0.002 in one pool, a $100 rung, 50,000 DOG_B."""
    snapshot, markets, quotes, reader = await anchor_on(
        now,
        market_sessions,
        pool("0x" + "51" * 20, base=MEME_A, quote=MEME_B, price="0.004", quote_price="0.002"),
    )

    context = await reader.execution_context(uuid4(), uuid4())

    valuation = context.quote_asset_valuation
    assert valuation is not None
    assert valuation.asset_id == f"robinhood:mainnet:{MEME_B}"
    assert valuation.usd_per_token == Decimal("0.002")
    assert valuation.snapshot_id == snapshot.id, "the case's own pool, not another"
    first = context.ladder[0]
    assert first.amount_in_tokens == Decimal(50_000)
    assert first.amount_in == 50_000 * 10**18
    request = quotes.requests[0]
    assert (request["token_in"], request["token_out"]) == (MEME_B, MEME_A)
    assert request["amount_in"] == 50_000 * 10**18
    # Exactly one market was read: the case's pool. No MEME_B market was sought.
    assert markets.asked == [snapshot.pair.pair_id]


# ------------------------------------------------------------ other pair shapes


@pytest.mark.parametrize(
    ("name", "base", "quote", "base_usd", "quote_usd"),
    [
        ("MEME/TOKEN", MEME_A, TOKEN_X, "0.004", "0.37"),
        ("TOKEN/WETH", TOKEN, WRAPPED, "2", "2600"),
        ("TOKEN/STABLE", TOKEN, STABLE, "2", "0.9990"),
        ("NATIVE/TOKEN", NATIVE, TOKEN, "600", "2"),
        ("TOKEN/NATIVE", TOKEN, NATIVE, "2", "600"),
    ],
)
async def test_every_pair_shape_pays_in_its_own_quote_asset(
    now, market_sessions, name, base, quote, base_usd, quote_usd
):
    snapshot, markets, quotes, reader = await anchor_on(
        now,
        market_sessions,
        pool("0x" + "52" * 20, base=base, quote=quote, price=base_usd, quote_price=quote_usd),
    )

    context = await reader.execution_context(uuid4(), uuid4())

    assert context.quote_asset_valuation is not None, name
    assert context.quote_asset_valuation.usd_per_token == Decimal(quote_usd)
    # tokenIn is the quote asset, tokenOut the base — canonical addresses,
    # the native asset included as the zero address.
    assert (quotes.requests[0]["token_in"], quotes.requests[0]["token_out"]) == (quote, base)
    # $100 in the quote asset's own units, truncated downward to its precision.
    expected = (Decimal(100) / Decimal(quote_usd)).quantize(
        Decimal(1).scaleb(-18), rounding=ROUND_DOWN
    )
    assert context.ladder[0].amount_in_tokens == expected
    assert markets.asked == [snapshot.pair.pair_id]


# ------------------------------------------------------------------- fail closed


async def test_an_unknown_quote_price_stops_before_any_request(now, market_sessions):
    _, markets, quotes, reader = await anchor_on(
        now,
        market_sessions,
        pool("0x" + "53" * 20, base=MEME_A, quote=MEME_B, price="0.004", quote_price=None),
    )
    with pytest.raises(AnchorContextUnavailable) as error:
        await reader.execution_context(uuid4(), uuid4())
    assert error.value.reason_code == "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"
    assert quotes.requests == []
    assert len(markets.asked) == 1, "no other market is tried instead"


async def test_a_case_whose_payment_asset_is_not_the_pools_quote_is_refused(now, market_sessions):
    """The quote price belongs to the pool's quote asset, and only to it."""
    (snapshot,) = await observe(
        now, [pool("0x" + "54" * 20, base=MEME_A, quote=MEME_B, price="0.004", quote_price="0.002")]
    )
    other = snapshot.pair.market_identity.model_copy(
        update={"quote_asset_id": f"robinhood:mainnet:{TOKEN_X}"}
    )
    _, _, quotes, reader = await anchor_on(
        now,
        market_sessions,
        pool("0x" + "54" * 20, base=MEME_A, quote=MEME_B, price="0.004", quote_price="0.002"),
        case_market=other,
    )
    with pytest.raises(AnchorContextUnavailable) as error:
        await reader.execution_context(uuid4(), uuid4())
    assert error.value.reason_code == "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"
    assert quotes.requests == []


async def test_a_legacy_observation_is_never_valued_from_another_pool(now, market_sessions):
    """A version-2 case pool plus a recorded MEME_B market: still refused.

    The old path looked the payment asset up as a market of its own. That path
    is gone, so a legacy observation without a quote price is refused even when
    such a market exists — until the case's pool is observed again as version 3.
    """
    (legacy,) = await observe(
        now, [pool("0x" + "55" * 20, base=MEME_A, quote=MEME_B, price="0.004", quote_price="0.002")]
    )
    data = legacy.model_dump()
    data.pop("quote_price")
    data["schema_version"] = 2
    from src.markets.models import MarketSnapshot

    legacy = MarketSnapshot.model_validate(data)
    (payment,) = await observe(
        now, [pool("0x" + "56" * 20, base=MEME_B, quote=TOKEN_X, price="0.002", quote_price="1")]
    )
    recorder = MarketRecorder(market_sessions, clock=FixedClock(now))
    await recorder.record(legacy)
    await recorder.record(payment)
    markets = WatchedMarkets(MarketReader(market_sessions, clock=FixedClock(now)))
    quotes = WatchedQuotes(reference_price=Decimal("0.004"), quoted_at=now)
    reader = AnchorContextReader(
        cases=StubCases(StubTradeCase(legacy.pair.market_identity), triggered_pair(now)),
        markets=markets,
        quotes=quotes,
        clock=FixedClock(now),
    )

    with pytest.raises(AnchorContextUnavailable) as error:
        await reader.execution_context(uuid4(), uuid4())
    assert error.value.reason_code == "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"
    assert markets.asked == [legacy.pair.pair_id]
    assert quotes.requests == []
