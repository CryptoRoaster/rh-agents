"""Pricing an open position from the market it was acquired in.

Pure contract tests over the reader: no database, no services. The integration
proofs live in `tests/casefill/test_marks.py`, which carries a real position
through a real fill.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.core.models import Position
from src.markets.models import Availability
from src.orchestration.valuation.models import PositionMark, ValuationRefusal
from src.orchestration.valuation.service import PositionValuationReader
from tests.casefill.conftest import MultiMarkets
from tests.riskdata.conftest import BASE_ASSET, PAIR_ID, market_for, recorded_snapshot

OTHER = market_for(token="c7" * 20, pool="d8" * 20)


def holding(now, *, asset_id=BASE_ASSET, pair_id=PAIR_ID, quantity="5", **overrides):
    values = {
        "source": "LEDGER",
        "correlation_id": uuid5(NAMESPACE_URL, "rh-agents:valuation-test"),
        "asset_id": asset_id,
        "market_pair_id": pair_id,
        "market_chain": "robinhood",
        "market_network": "mainnet",
        "market_provider": "geckoterminal",
        "quantity": Decimal(quantity),
        "cost_basis_usd": Decimal("50"),
        "created_at": now,
        "updated_at": now,
    }
    return Position(**{**values, **overrides})


def reader(feed, *, max_age=30):
    return PositionValuationReader(markets=feed, max_age_seconds=max_age)


async def test_an_open_position_is_priced_from_its_own_market(now):
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)))
    valuation = await reader(feed).value([holding(now)], now)

    assert valuation.complete
    mark = valuation.by_asset[BASE_ASSET]
    assert mark.price_usd == Decimal("1.25")
    assert mark.pair_id == PAIR_ID
    assert mark.provider == "geckoterminal"
    # The source's own instant, never the moment it was read.
    assert mark.observed_at == now - timedelta(seconds=5)
    assert feed.requested == [PAIR_ID]


async def test_several_open_positions_are_all_priced(now):
    other = recorded_snapshot(
        now,
        age=timedelta(seconds=5),
        base_asset_id=OTHER.base_asset_id,
        pair_id=OTHER.pair_id,
        label="other",
        price=Decimal("7.5"),
    )
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)), other)

    valuation = await reader(feed).value(
        [holding(now), holding(now, asset_id=OTHER.base_asset_id, pair_id=OTHER.pair_id)], now
    )

    assert valuation.complete
    assert len(valuation.marks) == 2
    assert valuation.by_asset[OTHER.base_asset_id].price_usd == Decimal("7.5")
    assert set(valuation.valued_assets) == {BASE_ASSET, OTHER.base_asset_id}


async def test_a_closed_position_needs_no_price(now):
    feed = MultiMarkets()
    valuation = await reader(feed).value([holding(now, quantity="0")], now)

    assert valuation.complete
    assert valuation.marks == ()
    assert valuation.valued_assets == ()
    assert feed.requested == []


async def test_a_position_without_a_market_cannot_be_valued(now):
    """An asset is not a market, and choosing one for it would be a guess."""
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)))
    valuation = await reader(feed).value([holding(now, pair_id=None)], now)

    assert not valuation.complete
    assert valuation.unvalued[0].reason is ValuationRefusal.POSITION_MARKET_UNKNOWN
    assert feed.requested == []


async def test_an_unrecorded_market_cannot_be_valued(now):
    valuation = await reader(MultiMarkets()).value([holding(now)], now)

    assert valuation.unvalued[0].reason is ValuationRefusal.MARKET_NOT_RECORDED


async def test_an_unavailable_price_is_never_replaced(now):
    """Not the entry price, not zero. Both turn unknown into confidently wrong."""
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5), price=None))
    valuation = await reader(feed).value([holding(now)], now)

    assert valuation.unvalued[0].reason is ValuationRefusal.PRICE_UNAVAILABLE
    assert valuation.marks == ()


async def test_a_price_for_another_asset_is_refused(now):
    """Wrong by the exchange rate, and entirely plausible-looking."""
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)))
    valuation = await reader(feed).value(
        [holding(now, asset_id=OTHER.base_asset_id, pair_id=PAIR_ID)], now
    )

    assert valuation.unvalued[0].reason is ValuationRefusal.PRICE_ASSET_MISMATCH


async def test_a_recording_from_another_chain_is_refused(now):
    """An address means nothing across chains."""
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)))
    valuation = await reader(feed).value([holding(now, market_chain="bsc")], now)

    assert valuation.unvalued[0].reason is ValuationRefusal.MARKET_IDENTITY_MISMATCH


async def test_a_recording_from_another_provider_is_refused(now):
    """Two providers observing one pool are two sources."""
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5)))
    valuation = await reader(feed).value([holding(now, market_provider="elsewhere")], now)

    assert valuation.unvalued[0].reason is ValuationRefusal.MARKET_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=29), None),
        (timedelta(seconds=30), None),
        (timedelta(seconds=31), ValuationRefusal.PRICE_STALE),
        (timedelta(seconds=-1), ValuationRefusal.PRICE_NOT_YET_OBSERVED),
    ],
)
async def test_freshness_is_judged_against_the_bound_the_evaluation_uses(now, age, expected):
    """The same tolerance SENTINEL applies, never a second one chosen here."""
    feed = MultiMarkets(recorded_snapshot(now, age=age, metadata_age=max(age, timedelta(0))))
    valuation = await reader(feed, max_age=30).value([holding(now)], now)

    if expected is None:
        assert valuation.complete
    else:
        assert valuation.unvalued[0].reason is expected


async def test_the_same_inputs_give_the_same_valuation(now):
    """Deterministic: nothing here depends on when it ran."""
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    first = await reader(MultiMarkets(snapshot)).value([holding(now)], now)
    second = await reader(MultiMarkets(snapshot)).value([holding(now)], now)

    assert first.model_dump_json() == second.model_dump_json()


async def test_a_valuation_is_complete_or_it_is_nothing(now):
    """A partial exposure figure is worse than none: it looks like one."""
    other = recorded_snapshot(
        now,
        age=timedelta(seconds=5),
        base_asset_id=OTHER.base_asset_id,
        pair_id=OTHER.pair_id,
        label="other",
    )
    feed = MultiMarkets(other)  # the primary market is missing

    valuation = await reader(feed).value(
        [holding(now), holding(now, asset_id=OTHER.base_asset_id, pair_id=OTHER.pair_id)], now
    )

    assert not valuation.complete
    assert len(valuation.marks) == 1
    assert {item.asset_id for item in valuation.unvalued} == {BASE_ASSET}


def test_a_mark_ages_out_against_a_later_instant(now):
    mark = PositionMark(
        asset_id=BASE_ASSET,
        pair_id=PAIR_ID,
        provider="geckoterminal",
        snapshot_id=uuid4(),
        observation_id=uuid4(),
        price_usd=Decimal("1.25"),
        observed_at=now,
    )
    assert mark.is_current_at(now + timedelta(seconds=30), 30)
    assert not mark.is_current_at(now + timedelta(seconds=31), 30)
    assert not mark.is_current_at(now - timedelta(seconds=1), 30)


def test_the_reader_holds_one_read_only_port():
    from src.orchestration.valuation.service import ValuationMarketInput

    assert {name for name in vars(ValuationMarketInput) if not name.startswith("_")} == {"latest"}


def test_availability_is_the_market_layer_s_own(now):
    assert Availability.AVAILABLE.value == "AVAILABLE"
