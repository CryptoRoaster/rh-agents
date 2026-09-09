from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from src.core.models import ExecutionResult, ExecutionTiming, MarketSnapshot, OrderIntent, Side
from src.core.numbers import quantize
from src.execution.base import Executor
from src.risk.engine import BPS


class PaperExecutor(Executor):
    """Pure fill simulation. Database service owns durable idempotency and accounting."""

    async def execute(self, order: OrderIntent, market: MarketSnapshot) -> ExecutionResult:
        # Revalidate at the infrastructure boundary, even for Python model callers.
        order = OrderIntent.model_validate_json(order.model_dump_json())
        market = MarketSnapshot.model_validate_json(market.model_dump_json())
        if order.risk.market_snapshot_id != market.id or order.intent.asset_id != market.asset_id:
            raise ValueError("Execution must use the approved market snapshot")
        if order.risk.market_fingerprint != market.fingerprint():
            raise ValueError("Market snapshot changed after approval")
        slippage = market.liquidity.estimated_slippage_bps
        if slippage is None or market.fee_bps is None:
            raise ValueError("Unknown fill costs fail closed")
        if slippage > order.risk.max_slippage_bps or slippage >= BPS or market.fee_bps >= BPS:
            raise ValueError("Fill costs exceed approval")
        sign = Decimal("1") if order.intent.side == Side.BUY else Decimal("-1")
        price = quantize(market.price_usd * (1 + sign * slippage / BPS))
        fees = quantize(order.intent.quantity * price * market.fee_bps / BPS)
        now = order.execution_requested_at
        return ExecutionResult(
            id=uuid5(NAMESPACE_URL, f"rh-agents:paper:{order.id}"),
            created_at=now,
            updated_at=now,
            source="EXECUTOR:PAPER",
            correlation_id=order.correlation_id,
            order_id=order.id,
            intent_id=order.intent.id,
            asset_id=market.asset_id,
            side=order.intent.side,
            quantity=order.intent.quantity,
            signal_price=order.intent.signal_price,
            quote_price=market.price_usd,
            execution_price=price,
            estimated_slippage_bps=slippage,
            realized_slippage_bps=slippage,
            fees_usd=fees,
            gas_usd=Decimal("0"),
            execution_latency_ms=0,
            timing=ExecutionTiming(
                detected_at=order.intent.timing.detected_at,
                decision_at=order.intent.timing.decision_at,
                risk_approved_at=order.risk.evaluated_at,
                execution_requested_at=now,
            ),
        )
