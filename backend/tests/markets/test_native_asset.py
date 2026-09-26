"""The chain's native asset as a pool side, in GeckoTerminal's own representation.

Reproduced from live BSC `new_pools` on 2026-09-26: 10 of 20 pools on one page
were rejected as `provider_identity`, every one at the same rule. Their quote
token is the resource `bsc_0x0000000000000000000000000000000000000000`, which
GeckoTerminal declares as a token with 18 decimals — BNB itself. Uniswap V4 pools
name the native currency by the zero address, and four.meme bonding curves
trade against native BNB. The token-address rule refused every zero address, so
all of those markets were lost before a watch could exist.

The fix is narrow. The zero address is accepted as a token only as the chain's
declared native asset: the resource must still be bound to that exact address,
and it must carry the native decimals. Nothing else about identity changes — the
pool address, the resource bindings, the network, the provenance and "base and
quote differ" are all enforced exactly as before, and no symbol or name is ever
consulted.

The shapes below keep the provider's structure; every address is synthetic.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.core.config import Settings
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import PoolLocatorKind

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
NATIVE = "0x" + "0" * 40
# A four.meme bonding curve: GeckoTerminal addresses the "pool" by the token.
MEME = "0x" + "4f" * 18 + "ffff"
# A Uniswap V4 pool: a bytes32 pool id, not a contract address.
V4_POOL = "0x" + "b4" * 32
V4_TOKEN = "0x" + "97" * 20
USDT = "0x" + "55" * 20

NETWORKS = {
    "data": [
        {
            "id": "bsc",
            "type": "network",
            "attributes": {
                "name": "BNB Chain",
                "coingecko_asset_platform_id": "binance-smart-chain",
            },
        }
    ],
    "links": {"next": None},
}


def token(address: str, symbol: str, decimals: int | None = 18, resource: str | None = None):
    return {
        "id": resource if resource is not None else f"bsc_{address}",
        "type": "token",
        "attributes": {
            "address": address,
            "name": symbol,
            "symbol": symbol,
            "decimals": decimals,
            "image_url": None,
            "coingecko_coin_id": None,
        },
    }


def pool(address: str, base: str, quote: str, dex: str, *, reserve="1234.5", volume="10"):
    return {
        "id": f"bsc_{address}",
        "type": "pool",
        "attributes": {
            "address": address,
            "name": "MEME / BNB",
            "base_token_price_usd": "0.0000042",
            "reserve_in_usd": reserve,
            "volume_usd": {"h24": volume},
        },
        "relationships": {
            "base_token": {"data": {"type": "token", "id": f"bsc_{base}"}},
            "quote_token": {"data": {"type": "token", "id": f"bsc_{quote}"}},
            "dex": {"data": {"type": "dex", "id": dex}},
        },
    }


def native(**overrides):
    return token(NATIVE, overrides.pop("symbol", "BNB"), **overrides)


def four_meme(**kwargs):
    return pool(MEME, MEME, NATIVE, "four-meme", **kwargs)


def uniswap_v4():
    return pool(V4_POOL, V4_TOKEN, NATIVE, "uniswap-v4-bsc")


async def discover(pools, tokens):
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="bsc",
        geckoterminal_pools_per_chain=20,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/networks"):
            body = NETWORKS
        else:
            body = {"data": pools, "included": tokens}
        return httpx.Response(200, text=json.dumps(body))

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        from src.core.clock import FixedClock

        adapter = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS["bsc"],
            settings,
            clock=FixedClock(NOW),
        )
        pairs = await adapter.discover()
        snapshots = [await adapter.snapshot(pair) for pair in pairs]
    return adapter, snapshots


# ------------------------------------------------ previously false rejections


async def test_a_four_meme_pool_quoted_in_native_bnb_is_accepted():
    adapter, (snapshot,) = await discover([four_meme()], [token(MEME, "MEME"), native()])
    assert (adapter.discovered, adapter.failed, adapter.rejections) == (1, 0, {})
    identity = snapshot.pair.market_identity
    assert identity.base_asset_id == f"bsc:mainnet:{MEME}"
    assert identity.quote_asset_id == f"bsc:mainnet:{NATIVE}"
    assert identity.pair_id == f"bsc:mainnet:contract_address:{MEME}"
    assert identity.venue == "four-meme"
    assert identity.pool_locator.kind is PoolLocatorKind.CONTRACT_ADDRESS
    assert snapshot.pair.quote.decimals == 18


async def test_a_uniswap_v4_pool_on_the_native_currency_is_accepted():
    adapter, (snapshot,) = await discover([uniswap_v4()], [token(V4_TOKEN, "V4T"), native()])
    assert adapter.failed == 0
    identity = snapshot.pair.market_identity
    assert identity.quote_asset_id == f"bsc:mainnet:{NATIVE}"
    assert identity.pool_locator.kind is PoolLocatorKind.BYTES32_POOL_ID
    assert identity.pair_id == f"bsc:mainnet:bytes32_pool_id:uniswap-v4-bsc:{V4_POOL}"


async def test_the_native_asset_may_also_be_the_base():
    adapter, (snapshot,) = await discover(
        [pool("0x" + "c7" * 20, NATIVE, USDT, "pancakeswap_v2")],
        [native(), token(USDT, "USDT")],
    )
    assert adapter.failed == 0
    assert snapshot.pair.market_identity.base_asset_id == f"bsc:mainnet:{NATIVE}"


async def test_accepting_the_native_asset_does_not_depend_on_its_symbol_or_name():
    """Identity is the address and the declared decimals, never a label."""
    adapter, pairs = await discover([four_meme()], [token(MEME, "MEME"), native(symbol="XYZ")])
    assert adapter.failed == 0 and len(pairs) == 1


async def test_acceptance_does_not_depend_on_liquidity_or_volume():
    adapter, pairs = await discover(
        [four_meme(reserve="0", volume="0")], [token(MEME, "MEME"), native()]
    )
    assert adapter.failed == 0 and len(pairs) == 1
    assert pairs[0].liquidity.value_usd == 0


# ----------------------------------------------- still rejected, fail closed


@pytest.mark.parametrize(
    "native_token",
    [
        native(decimals=None),
        native(decimals=9),
        native(decimals=0),
        # The zero address claimed by a resource that names another address.
        token(NATIVE, "BNB", resource="bsc_" + "0x" + "bb" * 20),
    ],
    ids=["decimals_unknown", "decimals_nine", "decimals_zero", "resource_not_bound"],
)
async def test_a_zero_address_that_is_not_the_declared_native_asset_is_rejected(native_token):
    tokens = [token(MEME, "MEME"), native_token]
    pools = [four_meme()]
    if native_token["id"] != f"bsc_{NATIVE}":
        pools[0]["relationships"]["quote_token"]["data"]["id"] = native_token["id"]
    adapter, pairs = await discover(pools, tokens)
    assert pairs == []
    assert adapter.rejections == {"provider_identity": 1}


async def test_native_on_both_sides_is_rejected():
    adapter, pairs = await discover(
        [pool("0x" + "c8" * 20, NATIVE, NATIVE, "uniswap-v4-bsc")], [native()]
    )
    assert pairs == [] and adapter.rejections == {"provider_identity": 1}


async def test_a_zero_pool_address_is_still_rejected():
    zero_pool = pool(NATIVE, MEME, NATIVE, "four-meme")
    adapter, pairs = await discover([zero_pool], [token(MEME, "MEME"), native()])
    assert pairs == [] and adapter.rejections == {"provider_identity": 1}


async def test_a_contradicting_token_binding_is_still_rejected():
    """A real provider contradiction: the resource names one token, the attributes another."""
    lying = token("0x" + "12" * 20, "MEME", resource=f"bsc_{MEME}")
    adapter, pairs = await discover([four_meme()], [lying, native()])
    assert pairs == [] and adapter.rejections == {"provider_identity": 1}


async def test_the_native_asset_on_another_network_resource_is_rejected():
    wrong = token(NATIVE, "BNB", resource=f"robinhood_{NATIVE}")
    pools = [four_meme()]
    pools[0]["relationships"]["quote_token"]["data"]["id"] = wrong["id"]
    adapter, pairs = await discover(pools, [token(MEME, "MEME"), wrong])
    assert pairs == [] and adapter.rejections == {"provider_identity": 1}


# ------------------------------------------------ OHLCV orientation, native quote
#
# Verified live on 2026-09-26: for a four.meme pool quoted in native BNB,
# GeckoTerminal's OHLCV `meta.quote.address` is the zero address itself, so the
# unchanged orientation check accepts the canonical native quote as-is.


def native_quoted_market():
    from src.markets.models import MarketIdentity

    return MarketIdentity(
        provider="geckoterminal",
        chain="bsc",
        network="mainnet",
        pair_id=f"bsc:mainnet:contract_address:{MEME}",
        base_asset_id=f"bsc:mainnet:{MEME}",
        quote_asset_id=f"bsc:mainnet:{NATIVE}",
        venue="four-meme",
        is_fixture=False,
    )


async def test_a_native_quote_passes_the_ohlcv_orientation_check():
    from tests.markets.test_ohlcv import ANCHOR, body, fetch, rows

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="bsc",
    )
    history = await fetch(
        settings,
        body(rows(3), base=MEME, quote=NATIVE),
        now=ANCHOR + timedelta(hours=1),
        market=native_quoted_market(),
    )
    assert history.quote_asset_id == f"bsc:mainnet:{NATIVE}"
    assert len(history.bars) == 3


async def test_an_ohlcv_quote_other_than_the_native_asset_is_refused():
    from tests.markets.test_ohlcv import ANCHOR, body, fetch, refuses, rows

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="bsc",
    )
    with refuses("MARKET_HISTORY_PROVIDER_IDENTITY"):
        await fetch(
            settings,
            body(rows(3), base=MEME, quote=USDT),
            now=ANCHOR + timedelta(hours=1),
            market=native_quoted_market(),
        )
