"""Version-3 market observations: the pool's own quote-asset USD price.

GeckoTerminal states, for one pool, both `base_token_price_usd` and
`quote_token_price_usd`. A version-3 snapshot keeps both — `price` for the base
asset, `quote_price` for the quote asset — bound to the pool's actual assets in
the pool's own orientation, with nothing derived from ratios, reserves, symbols
or an assumed peg. Versions 1 and 2 stay exactly as they were recorded.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.clock import FixedClock
from src.data.repository import aware
from src.data.tables import MarketObservationRow
from src.markets.fake import fixture_snapshot
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import Availability, MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from tests.runner.provider import MarketProvider, pool
from tests.runner.test_acquisition import acquiring_settings

MEME_A = "0x" + "1a" * 20
MEME_B = "0x" + "2b" * 20
POOL = "0x" + "51" * 20


async def observe(now, pools, chain="robinhood"):
    """Normalized snapshots from a provider answer, through the production adapter."""
    provider = MarketProvider(discovery=pools)
    settings = acquiring_settings(market_chains=chain)
    clock = FixedClock(now)
    transport = GeckoTerminalTransport(settings, transport=provider.transport(), clock=clock)
    try:
        adapter = GeckoTerminalAdapter(
            transport, NetworkDirectory(transport, settings), CHAINS[chain], settings, clock=clock
        )
        return [await adapter.snapshot(pair) for pair in await adapter.discover()]
    finally:
        await transport.__aexit__(None, None, None)


def meme_pool(price="0.004", quote_price="0.002", **kwargs):
    return pool(POOL, base=MEME_A, quote=MEME_B, price=price, quote_price=quote_price, **kwargs)


# ------------------------------------------------------------------ normalization


async def test_both_usd_prices_are_kept_and_bound_to_their_own_assets(now):
    (snapshot,) = await observe(now, [meme_pool()])

    assert snapshot.schema_version == 3
    assert snapshot.price.asset_id == f"robinhood:mainnet:{MEME_A}"
    assert snapshot.price.value_usd == Decimal("0.004")
    assert snapshot.quote_price is not None
    assert snapshot.quote_price.asset_id == f"robinhood:mainnet:{MEME_B}"
    assert snapshot.quote_price.status is Availability.AVAILABLE
    assert snapshot.quote_price.value_usd == Decimal("0.002")
    # The pool's own orientation: base stays base, quote stays quote.
    assert snapshot.pair.base.asset_id.endswith(MEME_A)
    assert snapshot.pair.quote.asset_id.endswith(MEME_B)


@pytest.mark.parametrize("stated", [None, "0"])
async def test_an_unstated_quote_price_is_unknown_never_estimated(now, stated):
    (snapshot,) = await observe(now, [meme_pool(quote_price=stated)])

    assert snapshot.schema_version == 3
    assert snapshot.quote_price is not None, "present, even when unknown"
    assert snapshot.quote_price.status is Availability.UNKNOWN
    assert snapshot.quote_price.value_usd is None
    # And the market itself is still what it was: base price and liquidity.
    assert snapshot.available


async def test_the_quote_price_is_never_derived_from_the_base_price_or_reserves(now):
    """A base price and reserves are present; an absent quote price stays absent."""
    (snapshot,) = await observe(now, [meme_pool(price="5", quote_price=None, liquidity="1000")])
    assert snapshot.quote_price is not None
    assert snapshot.quote_price.value_usd is None


async def test_native_quote_is_priced_from_the_same_pool(now):
    native = "0x" + "00" * 20
    (snapshot,) = await observe(
        now, [pool(POOL, base=MEME_A, quote=native, price="0.004", quote_price="600")]
    )
    assert snapshot.pair.quote.asset_id == f"robinhood:mainnet:{native}"
    assert snapshot.quote_price is not None
    assert snapshot.quote_price.asset_id == f"robinhood:mainnet:{native}"
    assert snapshot.quote_price.value_usd == Decimal("600")


# ------------------------------------------------------------ binding and versions


async def v3(now):
    (snapshot,) = await observe(now, [meme_pool()])
    return snapshot.model_dump()


async def test_version_3_requires_a_quote_price(now):
    data = await v3(now)
    data.pop("quote_price")
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def test_a_quote_price_for_another_token_is_refused(now):
    data = await v3(now)
    data["quote_price"]["asset_id"] = f"robinhood:mainnet:{'0x' + '99' * 20}"
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def test_a_quote_price_for_the_base_asset_is_refused(now):
    data = await v3(now)
    data["quote_price"]["asset_id"] = data["pair"]["base"]["asset_id"]
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "coingecko"),
        ("chain", "bsc"),
        ("network", "testnet"),
        ("correlation_id", uuid4()),
        ("is_fixture", True),
    ],
)
async def test_a_quote_price_with_other_provenance_is_refused(now, field, value):
    data = await v3(now)
    data["quote_price"][field] = value
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def test_a_quote_price_newer_than_its_snapshot_is_refused(now):
    data = await v3(now)
    data["quote_price"]["observed_at"] = now + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def stale_quote(now):
    """A fresh core market whose quote price was observed two hours earlier."""
    data = await v3(now)
    data["quote_price"]["observed_at"] = now - timedelta(hours=2)
    return MarketSnapshot.model_validate(data)


async def test_general_freshness_ignores_the_quote_price(now):
    """`freshness_at` is the market's: snapshot, base price, liquidity, volume."""
    snapshot = await stale_quote(now)
    assert snapshot.freshness_at == now
    assert snapshot.is_valid_at(now, timedelta(seconds=60))


async def test_a_stale_quote_price_does_not_age_the_recorded_market(now, market_sessions):
    """The central regression: a stale quote price never hides a fresh market."""
    snapshot = await stale_quote(now)
    await MarketRecorder(market_sessions, clock=FixedClock(now)).record(snapshot)

    async with market_sessions() as session:
        row = await session.get(MarketObservationRow, snapshot.id)
    assert row is not None
    assert aware(row.freshness_at) == now, "not now - 2h"

    # Every general reader — PULSE, SENTINEL, the scout — still sees the market.
    read = await MarketReader(
        market_sessions, clock=FixedClock(now), max_age=timedelta(seconds=60)
    ).latest(snapshot.pair.pair_id)
    assert read is not None and read.id == snapshot.id
    assert read.quote_price is not None
    assert read.quote_price.observed_at == now - timedelta(hours=2), "nothing re-dated"


def test_version_1_and_2_are_unchanged_and_carry_no_quote_price(now, trace):
    legacy = fixture_snapshot(now, trace)
    assert legacy.schema_version == 1
    dumped = legacy.model_dump(mode="json")
    assert "quote_price" not in dumped, "stored payloads and digests stay exactly as they were"
    assert MarketSnapshot.model_validate(dumped).model_dump(mode="json") == dumped
    with_quote = {**dumped, "quote_price": dumped["price"]}
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(with_quote)


async def test_version_2_payloads_still_validate(now):
    data = await v3(now)
    data.pop("quote_price")
    data["schema_version"] = 2
    snapshot = MarketSnapshot.model_validate(data)
    assert snapshot.quote_price is None
    assert "quote_price" not in snapshot.model_dump(mode="json")


# ------------------------------------------------------------------------ reader


async def test_an_unknown_quote_price_does_not_make_the_market_unreadable(now, market_sessions):
    (snapshot,) = await observe(now, [meme_pool(quote_price=None)])
    await MarketRecorder(market_sessions, clock=FixedClock(now)).record(snapshot)

    read = await MarketReader(
        market_sessions, clock=FixedClock(now), max_age=timedelta(seconds=60)
    ).latest(snapshot.pair.pair_id)
    assert read is not None and read.schema_version == 3
    assert read.quote_price is not None and read.quote_price.status is Availability.UNKNOWN


async def test_a_newer_unknown_quote_price_does_not_fall_back_to_an_older_known_one(
    now, market_sessions
):
    earlier = now - timedelta(seconds=10)
    (old,) = await observe(earlier, [meme_pool(quote_price="0.002")])
    (new,) = await observe(now, [meme_pool(quote_price=None)])
    for at, snapshot in ((earlier, old), (now, new)):
        await MarketRecorder(market_sessions, clock=FixedClock(at)).record(snapshot)

    read = await MarketReader(market_sessions, clock=FixedClock(now)).latest(new.pair.pair_id)
    assert read is not None and read.id == new.id
    assert read.quote_price is not None and read.quote_price.value_usd is None


async def test_version_3_roundtrips_through_the_recorder_exactly(now, market_sessions):
    (snapshot,) = await observe(now, [meme_pool(quote_price="0.00000000123456789012345")])
    recorder = MarketRecorder(market_sessions, clock=FixedClock(now))
    await recorder.record(snapshot)
    await recorder.record(snapshot)  # a replay, not a conflict
    read = await MarketReader(market_sessions, clock=FixedClock(now)).latest(snapshot.pair.pair_id)
    assert read is not None and read.quote_price is not None
    assert read.quote_price.value_usd == Decimal("0.00000000123456789012345")
