"""Optional bounded live OHLCV reads. Never run in CI and never by default.

Enabled only by ``RH_AGENTS_LIVE_OHLCV_SMOKE=1``. The GeckoTerminal public API
needs no credential, so there is no key here and nothing to leak; the opt-in
exists because the public tier is rate-limited to roughly ten calls a minute and
a test suite should not spend somebody's budget without being asked.

Read-only. Nothing here trades, signs, broadcasts, persists a response or writes
any state. Each chain costs two requests: one to resolve the network directory
and one for the bars.

These exist because the three facts the adapter depends on are not in the
provider's documentation — that the newest interval is still forming, that rows
arrive newest-first, and that the timestamp is the interval's opening. If the
provider changes any of them, this is where it shows up first.
"""

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.config import Settings
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.ohlcv import GeckoTerminalOhlcvSource
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.history import HistoryCoverage
from src.markets.models import MarketIdentity

LIVE = os.environ.get("RH_AGENTS_LIVE_OHLCV_SMOKE") == "1"

# Pools confirmed to exist and to return OHLCV during the Phase 2H market-data
# audit. They are read targets only. An operator may point these elsewhere.
POOLS = {
    "robinhood": (
        os.environ.get("RH_OHLCV_POOL", "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3"),
        os.environ.get("RH_OHLCV_BASE", "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"),
        os.environ.get("RH_OHLCV_QUOTE", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
    ),
    "bsc": (
        os.environ.get("BSC_OHLCV_POOL", "0x185d73ec966d464a40372cd7e737bb68b0b95f1f"),
        os.environ.get("BSC_OHLCV_BASE", "0x3510fbbc13090f991ffa523527113a166161683e"),
        os.environ.get("BSC_OHLCV_QUOTE", "0x55d398326f99059ff775485246999027b3197955"),
    ),
}

DATABASE = "postgresql+asyncpg://test_user@localhost/test_database"

pytestmark = pytest.mark.skipif(not LIVE, reason="RH_AGENTS_LIVE_OHLCV_SMOKE is not enabled")


def market(chain: str) -> MarketIdentity:
    pool, base, quote = POOLS[chain]
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network="mainnet",
        pair_id=f"{chain}:mainnet:contract_address:{pool}",
        base_asset_id=f"{chain}:mainnet:{base}",
        quote_asset_id=f"{chain}:mainnet:{quote}",
        venue="uniswap-v3" if chain == "robinhood" else "pancakeswap-v3",
        is_fixture=False,
    )


@pytest.mark.parametrize("chain", sorted(POOLS))
async def test_a_real_pool_returns_closed_usd_denominated_bars(chain):
    """One bounded read per chain, asserting the contract the adapter relies on."""
    settings = Settings(
        _env_file=None,
        database_url=DATABASE,
        market_provider="geckoterminal",
        market_chains=chain,
    )
    identity = market(chain)
    async with GeckoTerminalTransport(settings) as transport:
        source = GeckoTerminalOhlcvSource(
            transport, NetworkDirectory(transport, settings), CHAINS[chain], settings
        )
        history = await source.history(identity, timeframe="hour", aggregate=1, bars=24)

    now = datetime.now(UTC)
    assert history.provider == "geckoterminal"
    assert history.pair_id == identity.pair_id
    assert history.price_basis == "USD_PER_BASE_UNIT"
    assert history.timeframe == "hour" and history.aggregate == 1
    assert history.coverage in {HistoryCoverage.COMPLETE, HistoryCoverage.PARTIAL}
    assert history.bars, "a live pool with trading activity should return closed bars"

    # Every returned bar has actually closed. This is the assertion that catches
    # the provider starting to mark, or stopping to return, the forming interval.
    assert all(bar.closed_at <= history.fetched_at for bar in history.bars)
    openings = [bar.opened_at for bar in history.bars]
    assert openings == sorted(openings)
    assert len(set(openings)) == len(openings)
    assert all(bar.opened_at.minute == 0 and bar.opened_at.second == 0 for bar in history.bars)
    assert all(isinstance(bar.close, Decimal) and bar.close > 0 for bar in history.bars)
    assert history.observed_at is not None and history.observed_at <= now

    print(
        f"\n{chain}: pool={identity.pair_id.rsplit(':', 1)[-1]} bars={len(history.bars)} "
        f"timeframe={history.timeframe}/{history.aggregate} coverage={history.coverage.value} "
        f"window={history.window_start.isoformat()}..{history.observed_at.isoformat()} "
        f"basis={history.price_basis} range={history.range_low}..{history.range_high}"
    )


@pytest.mark.parametrize("chain", sorted(POOLS))
async def test_a_quote_oriented_series_is_refused_against_a_real_pool(chain):
    """The orientation defence, proven against the live provider rather than a stub.

    Asking for the same pool with base and quote swapped in our own identity is
    exactly what a misconfigured market would look like. The response's own
    ``meta.base`` disagrees, and the read is refused.
    """
    from src.markets.geckoterminal.errors import IdentityError

    settings = Settings(
        _env_file=None,
        database_url=DATABASE,
        market_provider="geckoterminal",
        market_chains=chain,
    )
    pool, base, quote = POOLS[chain]
    swapped = market(chain).model_copy(
        update={
            "base_asset_id": f"{chain}:mainnet:{quote}",
            "quote_asset_id": f"{chain}:mainnet:{base}",
        }
    )
    async with GeckoTerminalTransport(settings) as transport:
        source = GeckoTerminalOhlcvSource(
            transport, NetworkDirectory(transport, settings), CHAINS[chain], settings
        )
        with pytest.raises(IdentityError):
            await source.history(swapped, timeframe="hour", aggregate=1, bars=6)
