from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.models import OrderIntent, RiskLimits, RiskOutcome, Side
from src.execution.paper import PaperExecutor
from src.risk.engine import evaluate


def approved_order(intent, market, context, now):
    risk = evaluate(intent, market, context, RiskLimits(), now=now)
    return OrderIntent(
        source="test",
        correlation_id=intent.correlation_id,
        intent=intent,
        risk=risk,
        execution_requested_at=now,
    )


async def test_paper_buy(intent, market, context, now):
    order = approved_order(intent, market, context, now)
    fill = await PaperExecutor().execute(order, market)
    assert fill.quantity == 2
    assert fill.execution_price == Decimal("100.5")
    assert fill.fees_usd == Decimal("0.201")
    assert fill.realized_slippage_bps == 50
    assert fill.gas_usd == 0
    assert fill.correlation_id == intent.correlation_id
    assert fill.timing.tx_signed_at is None
    assert fill.timing.tx_sent_at is None
    assert fill.timing.tx_confirmed_at is None


async def test_paper_sell(intent, market, context, now):
    intent = intent.model_copy(update={"side": Side.SELL})
    context = context.model_copy(update={"position_quantity": Decimal("2")})
    fill = await PaperExecutor().execute(approved_order(intent, market, context, now), market)
    assert fill.execution_price == Decimal("99.5")
    assert fill.fees_usd == Decimal("0.199")
    assert fill.side == Side.SELL


async def test_identical_orders_produce_identical_results_across_instances(
    intent, market, context, now
):
    order = approved_order(intent, market, context, now)
    assert await PaperExecutor().execute(order, market) == await PaperExecutor().execute(
        order, market
    )


@pytest.mark.parametrize("mutation", ["quantity", "rejection", "trace", "expired"])
def test_order_cannot_reuse_invalid_approval(intent, market, context, now, mutation):
    order = approved_order(intent, market, context, now)
    data = order.model_dump()
    if mutation == "quantity":
        data["intent"]["quantity"] = Decimal("200")
    elif mutation == "rejection":
        data["risk"]["outcome"] = RiskOutcome.REJECT
    elif mutation == "trace":
        data["correlation_id"] = uuid4()
    else:
        data["execution_requested_at"] = now + timedelta(seconds=6)
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(data)


async def test_market_cannot_change_under_same_id(intent, market, context, now):
    order = approved_order(intent, market, context, now)
    market = market.model_copy(update={"price_usd": Decimal("1000")})
    with pytest.raises(ValueError, match="changed after approval"):
        await PaperExecutor().execute(order, market)
