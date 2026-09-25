"""A position mark is a recorded market price, at the precision it was recorded.

The mark is provenance about an observation, so it keeps the market layer's
precision. The accounting boundary is the valuation result: exposure and
unrealized loss are computed from quantity times the exact mark, and only that
USD result is brought to the ledger's eighteen places.
"""

import json
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from src.core.numbers import quantize
from src.ledger.portfolio import portfolio_basis, portfolio_state, replay_portfolio_basis
from src.orchestration.valuation.service import PositionValuationReader
from tests.casefill.conftest import MultiMarkets
from tests.riskdata.conftest import BASE_ASSET, market_for, recorded_snapshot
from tests.valuation.test_marks import holding

# Synthetic, 23 decimal places.
WIDE_MARK = Decimal("1.25000000000000000000123")
QUANTITY = Decimal("123456.123456789012345678")
OTHER = market_for(token="c7" * 20, pool="d8" * 20)


async def wide_valuation(now):
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5), price=WIDE_MARK))
    reader = PositionValuationReader(markets=feed, max_age_seconds=30)
    return await reader.value([holding(now, quantity=str(QUANTITY))], now)


async def test_a_wide_recorded_price_becomes_an_exact_mark(now) -> None:
    valuation = await wide_valuation(now)
    assert valuation.complete
    mark = valuation.by_asset[BASE_ASSET]
    assert mark.price_usd == WIDE_MARK
    assert mark.price_usd.as_tuple() == WIDE_MARK.as_tuple()
    assert isinstance(mark.price_usd, Decimal)


async def test_the_serialized_mark_loses_no_precision(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    dumped = mark.model_dump(mode="json")
    assert dumped["price_usd"] == "1.25000000000000000000123"
    assert type(mark).model_validate(json.loads(json.dumps(dumped))).price_usd == WIDE_MARK


async def test_exposure_is_quantity_times_the_exact_mark_then_quantized(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    held = holding(now, quantity=str(QUANTITY))
    # The holding is valued from its mark: the order is for a different asset.
    state = portfolio_state(
        cash_usd=Decimal("10000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[held],
        asset_id=OTHER.base_asset_id,
        price_usd=Decimal("2"),
        marks={BASE_ASSET: mark},
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=OTHER,
    )
    with localcontext() as exact:
        exact.prec = 200
        expected = quantize(QUANTITY * WIDE_MARK)
    assert state.marks_used == (mark,)
    assert state.prices[BASE_ASSET] == WIDE_MARK
    assert state.context.exposure_usd == expected
    # The product is quantized once, at the end: not the price first.
    assert state.context.exposure_usd != quantize(QUANTITY * quantize(WIDE_MARK))


async def test_the_stored_basis_replays_to_the_same_state(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    held = holding(now, quantity=str(QUANTITY))
    state = portfolio_state(
        cash_usd=Decimal("10000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[held],
        asset_id=OTHER.base_asset_id,
        price_usd=Decimal("2"),
        marks={BASE_ASSET: mark},
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=OTHER,
    )
    stored = json.loads(json.dumps(portfolio_basis(state)))
    replayed = replay_portfolio_basis(stored)
    assert replayed.context == state.context
    assert replayed.marks_used == state.marks_used
    assert replayed.marks_used[0].price_usd == WIDE_MARK
