"""The GeckoTerminal OHLCV adapter, against the contract verified from the API.

The response shapes below are the ones the live V2 endpoint actually returns —
newest-first rows of `[timestamp, open, high, low, close, volume]`, a `meta`
naming which token was priced, and an in-progress newest interval that the
provider does not mark as such. Each test names the provider behaviour it exists
because of.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from src.core.clock import FixedClock
from src.core.config import Settings
from src.markets.geckoterminal.errors import ContractError, IdentityError
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.ohlcv import GeckoTerminalOhlcvSource
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.history import HistoryCoverage, MarketBar, interval_seconds
from src.markets.models import MarketIdentity
from tests.markets.test_geckoterminal import fixture

BASE = "0x" + "a1" * 20
QUOTE = "0x" + "b2" * 20
POOL = "0x" + "e5" * 20
# 2026-09-12 06:00:00Z — an exact hour, as every hourly bar opening is.
ANCHOR = datetime(2026, 9, 12, 6, tzinfo=UTC)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="bsc",
    )


def identity(chain: str = "bsc") -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network="mainnet",
        pair_id=f"{chain}:mainnet:contract_address:{POOL}",
        base_asset_id=f"{chain}:mainnet:{BASE}",
        quote_asset_id=f"{chain}:mainnet:{QUOTE}",
        venue="pancakeswap-v3",
        is_fixture=False,
    )


def rows(count: int, *, newest_open: datetime = ANCHOR, step: int = 3600, price="1.00"):
    """Newest-first rows, exactly as the provider orders them."""
    base = Decimal(price)
    built = []
    for index in range(count):
        opened = newest_open - timedelta(seconds=step * index)
        close = base + Decimal(index) / Decimal(100)
        built.append(
            [
                int(opened.timestamp()),
                str(close),
                str(close + Decimal("0.02")),
                str(close - Decimal("0.02")),
                str(close),
                str(Decimal(100 + index)),
            ]
        )
    return built


def body(ohlcv_list, *, base=BASE, quote=QUOTE):
    return json.dumps(
        {
            "data": {
                "id": "f0a0e2f0-0000-0000-0000-000000000000",
                "type": "ohlcv_request_response",
                "attributes": {"ohlcv_list": ohlcv_list},
            },
            "meta": {
                "base": {"address": base, "name": "Base", "symbol": "BASE"},
                "quote": {"address": quote, "name": "Quote", "symbol": "QUOTE"},
            },
        }
    )


async def fetch(settings, payload, *, now, chain="bsc", bars=24, market=None, capture=None):
    def handle(request):
        if request.url.path.endswith("/networks"):
            return httpx.Response(200, text=fixture("networks"))
        if capture is not None:
            capture.append(request)
        return httpx.Response(200, text=payload)

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        source = GeckoTerminalOhlcvSource(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS[chain],
            settings,
            clock=FixedClock(now),
        )
        return await source.history(
            market or identity(chain), timeframe="hour", aggregate=1, bars=bars
        )


# ------------------------------------------------- the request that is sent


async def test_the_orientation_parameters_are_sent_explicitly(settings):
    """Both are the documented defaults, and a default belongs to the vendor."""
    capture: list[httpx.Request] = []
    await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1), capture=capture)
    request = capture[-1]
    assert request.url.params["currency"] == "usd"
    assert request.url.params["token"] == "base"
    assert request.url.params["aggregate"] == "1"
    # Empty intervals stay omitted: a synthesized flat bar would be invented
    # structure, which is the whole thing this phase exists to prevent.
    assert request.url.params["include_empty_intervals"] == "false"
    assert request.url.path.endswith(f"/pools/{POOL}/ohlcv/hour")


async def test_one_extra_bar_is_requested_because_the_newest_is_discarded(settings):
    capture: list[httpx.Request] = []
    await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1), bars=24, capture=capture)
    assert capture[-1].url.params["limit"] == "25"


async def test_exactly_one_history_request_is_issued(settings):
    """No retry amplification: the Phase 2B runtime owns retries, not this source."""
    capture: list[httpx.Request] = []
    await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1), capture=capture)
    assert len(capture) == 1


# ----------------------------------------- the in-progress bar is dropped


async def test_the_forming_interval_is_never_presented_as_closed(settings):
    """Verified live: the newest bar's close equals the pool's current price.

    It is the current price wearing a candle's shape. Included, it would
    understate the range and invent a high and a low the interval has not
    finished making.
    """
    # Retrieved at 06:30, mid-way through the interval that opened at 06:00.
    history = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(minutes=30))
    assert history.bars
    assert all(bar.closed_at <= ANCHOR + timedelta(minutes=30) for bar in history.bars)
    assert history.bars[-1].opened_at == ANCHOR - timedelta(hours=1)
    assert history.observed_at == ANCHOR


async def test_a_bar_that_has_just_closed_is_kept(settings):
    """The boundary is inclusive: an interval that ended exactly now is closed."""
    history = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1))
    assert history.bars[-1].opened_at == ANCHOR
    assert history.observed_at == ANCHOR + timedelta(hours=1)


async def test_a_window_of_only_a_forming_bar_is_empty_rather_than_wrong(settings):
    history = await fetch(settings, body(rows(1)), now=ANCHOR + timedelta(minutes=1))
    assert history.bars == ()
    assert history.coverage == HistoryCoverage.EMPTY
    assert history.observed_at is None


# ------------------------------------------------------------- ordering


async def test_newest_first_rows_are_normalized_oldest_first(settings):
    """Confirmed newest-first on both chains, and sorted rather than trusted."""
    history = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1))
    openings = [bar.opened_at for bar in history.bars]
    assert openings == sorted(openings)
    assert history.bars[0].opened_at < history.bars[-1].opened_at


async def test_rows_in_any_order_normalize_to_the_same_series(settings):
    shuffled = list(reversed(rows(30)))
    straight = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1))
    jumbled = await fetch(settings, body(shuffled), now=ANCHOR + timedelta(hours=1))
    assert straight.bars == jumbled.bars


# --------------------------------------------- F, G, H: malformed series


async def test_scenario_f_a_low_above_its_high_is_refused(settings):
    broken = rows(30)
    broken[3] = [broken[3][0], "1.00", "0.90", "1.10", "1.00", "10"]
    with pytest.raises(ContractError):
        await fetch(settings, body(broken), now=ANCHOR + timedelta(hours=1))


async def test_a_close_outside_its_own_bar_is_refused(settings):
    broken = rows(30)
    broken[3] = [broken[3][0], "1.00", "1.05", "0.95", "1.50", "10"]
    with pytest.raises(ContractError):
        await fetch(settings, body(broken), now=ANCHOR + timedelta(hours=1))


@pytest.mark.parametrize(
    "row",
    [
        [1789196400, "1.0", "1.1", "0.9", "1.0"],  # short
        [1789196400, "1.0", "1.1", "0.9", "1.0", "10", "extra"],  # long
        ["1789196400", "1.0", "1.1", "0.9", "1.0", "10"],  # timestamp not an integer
        [1789196400, "0", "1.1", "0.9", "1.0", "10"],  # a price of zero
        [1789196400, "1.0", "1.1", "0.9", "1.0", "-5"],  # negative volume
        [-1, "1.0", "1.1", "0.9", "1.0", "10"],  # implausible epoch second
    ],
)
async def test_a_malformed_row_refuses_the_whole_series(settings, row):
    """One bad bar is a contract violation, not a thin market to be worked around."""
    broken = rows(30)
    broken[5] = row
    with pytest.raises(ContractError):
        await fetch(settings, body(broken), now=ANCHOR + timedelta(hours=1))


@pytest.mark.parametrize("offset", [137, 1800])
async def test_scenario_g_a_timestamp_off_the_interval_grid_is_refused(settings, offset):
    """A bar that does not start where an interval starts is not that interval.

    Two different guards catch this. A sub-minute offset fails the bar's own
    boundary rule; a clean half-hour offset passes that and is caught by the
    series, which knows the grid its interval implies. Both are exercised
    because a series-level slip is the one that would otherwise look tidy.
    """
    broken = rows(30)
    broken[4] = [broken[4][0] + offset, "1.0", "1.1", "0.9", "1.0", "10"]
    with pytest.raises(ContractError):
        await fetch(settings, body(broken), now=ANCHOR + timedelta(hours=1))


async def test_scenario_h_the_same_interval_twice_is_refused(settings):
    """Choosing between two copies would be inventing which one happened."""
    duplicated = rows(30)
    duplicated[7] = list(duplicated[6])
    with pytest.raises(ContractError):
        await fetch(settings, body(duplicated), now=ANCHOR + timedelta(hours=1))


async def test_a_bare_json_number_never_becomes_a_float_price(settings):
    """The live endpoint sends bare JSON numbers, not decimal strings.

    `3454.61590249189` in the response body is exactly the value that would lose
    precision through a float. The transport parses with ``parse_float=Decimal``
    and nothing downstream reintroduces one, so this asserts the real path rather
    than the convenient string form the other fixtures use.
    """
    numeric = json.loads(body(rows(30)))
    numeric["data"]["attributes"]["ohlcv_list"] = [
        [row[0], *(float(value) for value in row[1:])]
        for row in numeric["data"]["attributes"]["ohlcv_list"]
    ]
    raw = json.dumps(numeric)
    assert '"1.0"' not in raw and "1.0," in raw
    history = await fetch(settings, raw, now=ANCHOR + timedelta(hours=1))
    assert history.bars
    for bar in history.bars:
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume):
            assert isinstance(value, Decimal)
            assert not isinstance(value, float)


# ------------------------------------------ I, J: orientation and identity


async def test_scenario_i_a_series_priced_on_the_quote_side_is_refused(settings):
    """`token=quote` swaps which token the response calls base. That is detectable.

    It has to be detected structurally: against a dollar-stablecoin quote the
    inverted numbers can sit within a fraction of a percent of the correct ones,
    so no magnitude check would catch it.
    """
    with pytest.raises(IdentityError):
        await fetch(
            settings,
            body(rows(30), base=QUOTE, quote=BASE),
            now=ANCHOR + timedelta(hours=1),
        )


async def test_scenario_j_a_series_for_another_token_is_refused(settings):
    with pytest.raises(IdentityError):
        await fetch(
            settings,
            body(rows(30), base="0x" + "cc" * 20),
            now=ANCHOR + timedelta(hours=1),
        )


async def test_a_series_naming_one_token_on_both_sides_is_refused(settings):
    with pytest.raises(IdentityError):
        await fetch(settings, body(rows(30), quote=BASE), now=ANCHOR + timedelta(hours=1))


async def test_a_market_on_another_chain_is_refused_before_any_request(settings):
    with pytest.raises(IdentityError):
        await fetch(
            settings,
            body(rows(30)),
            now=ANCHOR + timedelta(hours=1),
            market=identity("robinhood"),
        )


async def test_the_series_declares_the_orientation_it_was_requested_in(settings):
    history = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1))
    assert history.price_basis == "USD_PER_BASE_UNIT"
    assert history.base_asset_id == f"bsc:mainnet:{BASE}"
    assert history.provider == "geckoterminal"
    assert history.is_fixture is False


# ------------------------------------------------------------ coverage


async def test_a_whole_window_is_complete(settings):
    history = await fetch(settings, body(rows(30)), now=ANCHOR + timedelta(hours=1), bars=24)
    assert len(history.bars) == 24
    assert history.coverage == HistoryCoverage.COMPLETE
    assert history.missing_intervals == 0


async def test_a_short_window_is_partial_rather_than_complete(settings):
    history = await fetch(settings, body(rows(10)), now=ANCHOR + timedelta(hours=1), bars=24)
    assert len(history.bars) == 10
    assert history.coverage == HistoryCoverage.PARTIAL


async def test_an_omitted_interval_is_counted_and_never_filled(settings):
    """`include_empty_intervals=false` means an untraded interval simply is not there."""
    sparse = [row for index, row in enumerate(rows(30)) if index not in {4, 5}]
    history = await fetch(settings, body(sparse), now=ANCHOR + timedelta(hours=1), bars=24)
    assert history.missing_intervals == 2
    assert history.coverage == HistoryCoverage.PARTIAL
    openings = [bar.opened_at for bar in history.bars]
    assert len(set(openings)) == len(openings)


async def test_the_window_is_trimmed_to_what_was_asked_for(settings):
    history = await fetch(settings, body(rows(60)), now=ANCHOR + timedelta(hours=1), bars=24)
    assert len(history.bars) == 24
    assert history.requested_bars == 24


# ------------------------------------------------------- the bar contract


def test_a_bar_cannot_open_off_a_minute_boundary():
    with pytest.raises(ValueError):
        MarketBar(
            opened_at=datetime(2026, 9, 12, 6, 0, 17, tzinfo=UTC),
            interval_seconds=3600,
            open=Decimal("1"),
            high=Decimal("1.1"),
            low=Decimal("0.9"),
            close=Decimal("1"),
            volume=Decimal("10"),
        )


def test_a_bar_knows_when_it_closed():
    bar = MarketBar(
        opened_at=ANCHOR,
        interval_seconds=3600,
        open=Decimal("1"),
        high=Decimal("1.1"),
        low=Decimal("0.9"),
        close=Decimal("1"),
        volume=Decimal("10"),
    )
    assert bar.closed_at == ANCHOR + timedelta(hours=1)


@pytest.mark.parametrize(
    ("timeframe", "aggregate", "seconds"),
    [
        ("minute", 1, 60),
        ("minute", 15, 900),
        ("hour", 1, 3600),
        ("hour", 4, 14400),
        ("day", 1, 86400),
    ],
)
def test_the_provider_timeframes_are_the_documented_ones(timeframe, aggregate, seconds):
    assert interval_seconds(timeframe, aggregate) == seconds


@pytest.mark.parametrize(
    ("timeframe", "aggregate"),
    [("hour", 2), ("minute", 30), ("day", 7), ("week", 1), ("hour", 0)],
)
def test_an_undocumented_timeframe_combination_is_refused(timeframe, aggregate):
    """The provider accepts a fixed set per timeframe; nothing else is requestable."""
    with pytest.raises(ValueError):
        interval_seconds(timeframe, aggregate)


async def test_something_that_is_not_a_market_identity_is_refused(settings):
    """The source is given a typed identity or nothing at all."""
    with pytest.raises(IdentityError):
        await fetch(
            settings,
            body(rows(30)),
            now=ANCHOR + timedelta(hours=1),
            market={"pair_id": "bsc:mainnet:contract_address:" + POOL},
        )


async def test_an_identity_naming_one_token_on_both_sides_is_refused(settings):
    """A degenerate market would make the orientation check vacuous.

    If base and quote were the same asset, matching the response's base would
    also match its quote and the comparison would prove nothing, so the
    degenerate case is refused outright rather than passed.
    """
    degenerate = identity().model_copy(update={"quote_asset_id": f"bsc:mainnet:{BASE}"})
    with pytest.raises(IdentityError):
        await fetch(
            settings,
            body(rows(30), quote=BASE),
            now=ANCHOR + timedelta(hours=1),
            market=degenerate,
        )
