from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import Position, Side
from src.execution.paper import PaperExecutor
from src.ledger.accounting import apply_fill, calculate_pnl
from tests.test_execution import approved_order


async def test_buy_partial_sell_and_pnl(intent, market, context, now):
    position = Position(
        source="test", correlation_id=intent.correlation_id, asset_id=intent.asset_id
    )
    buy = await PaperExecutor().execute(approved_order(intent, market, context, now), market)
    position, _, cash = apply_fill(position, buy, Decimal("10000"))
    assert position.quantity == 2
    assert position.cost_basis_usd == Decimal("201.201")
    assert cash == Decimal("9798.799")
    pnl = calculate_pnl(
        [position], {intent.asset_id: Decimal("110")}, cash=cash, fees_paid=buy.fees_usd, fill=buy
    )
    assert pnl.unrealized_pnl_usd == Decimal("18.799")
    assert pnl.equity_usd == Decimal("10018.799")

    sell_intent = intent.model_copy(
        update={"id": uuid4(), "side": Side.SELL, "quantity": Decimal("1")}
    )
    market = market.model_copy(update={"id": uuid4(), "price_usd": Decimal("110")})
    context = context.model_copy(update={"position_quantity": Decimal("2")})
    sell = await PaperExecutor().execute(approved_order(sell_intent, market, context, now), market)
    position, trade, cash = apply_fill(position, sell, cash)
    assert position.quantity == 1
    assert position.cost_basis_usd == Decimal("100.6005")
    assert trade.realized_pnl_usd == Decimal("8.74005")
    assert cash == Decimal("9908.13955")
    pnl = calculate_pnl(
        [position],
        {intent.asset_id: Decimal("110")},
        cash=cash,
        fees_paid=buy.fees_usd + sell.fees_usd,
        fill=sell,
    )
    assert pnl.total_pnl_usd == Decimal("18.13955")
    assert pnl.total_pnl_usd == pnl.equity_usd - Decimal("10000")

    position, _, cash = apply_fill(position, sell, cash)
    assert position.quantity == 0
    assert position.cost_basis_usd == 0
    with pytest.raises(ValueError, match="sell more"):
        apply_fill(position, sell, cash)


async def test_missing_mark_never_treated_as_zero(intent, market, context, now):
    fill = await PaperExecutor().execute(approved_order(intent, market, context, now), market)
    position = Position(
        source="test",
        correlation_id=intent.correlation_id,
        asset_id=intent.asset_id,
        quantity=1,
        cost_basis_usd=100,
    )
    with pytest.raises(KeyError):
        calculate_pnl([position], {}, cash=Decimal("100"), fees_paid=Decimal("0"), fill=fill)
