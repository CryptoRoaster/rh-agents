"""Long-only spot accounting using weighted average cost; buy fees enter cost basis."""

from decimal import Decimal

from src.core.models import ExecutionResult, PnLSnapshot, Position, Side, Trade
from src.core.numbers import quantize


def apply_fill(
    position: Position, fill: ExecutionResult, cash: Decimal
) -> tuple[Position, Trade, Decimal]:
    if position.asset_id != fill.asset_id:
        raise ValueError("Position and fill assets differ")
    gross = quantize(fill.quantity * fill.execution_price)
    costs = fill.fees_usd + fill.gas_usd
    realized = Decimal("0")
    if fill.side == Side.BUY:
        if gross + costs > cash:
            raise ValueError("Insufficient paper cash")
        quantity = position.quantity + fill.quantity
        basis = position.cost_basis_usd + gross + costs
        cash -= gross + costs
    else:
        if fill.quantity > position.quantity:
            raise ValueError("Cannot sell more than the recorded position")
        removed_basis = quantize(position.cost_basis_usd * fill.quantity / position.quantity)
        realized = gross - costs - removed_basis
        quantity = position.quantity - fill.quantity
        basis = position.cost_basis_usd - removed_basis
        cash += gross - costs
    updated = Position(
        id=position.id,
        created_at=position.created_at,
        updated_at=fill.created_at,
        source="LEDGER",
        correlation_id=fill.correlation_id,
        asset_id=position.asset_id,
        quantity=quantity,
        cost_basis_usd=basis,
        realized_pnl_usd=position.realized_pnl_usd + realized,
    )
    trade = Trade(
        created_at=fill.created_at,
        updated_at=fill.created_at,
        source="LEDGER",
        correlation_id=fill.correlation_id,
        execution_id=fill.id,
        position_id=position.id,
        asset_id=fill.asset_id,
        side=fill.side,
        quantity=fill.quantity,
        price_usd=fill.execution_price,
        fees_usd=costs,
        realized_pnl_usd=realized,
    )
    return updated, trade, cash


def calculate_pnl(
    positions: list[Position],
    prices: dict[str, Decimal],
    *,
    cash: Decimal,
    fees_paid: Decimal,
    fill: ExecutionResult,
) -> PnLSnapshot:
    # Missing marks are errors, never zero-valued assets.
    value = quantize(
        sum((p.quantity * prices[p.asset_id] for p in positions if p.quantity), Decimal("0"))
    )
    basis = sum((p.cost_basis_usd for p in positions), Decimal("0"))
    realized = sum((p.realized_pnl_usd for p in positions), Decimal("0"))
    unrealized = value - basis
    return PnLSnapshot(
        source="LEDGER",
        correlation_id=fill.correlation_id,
        created_at=fill.created_at,
        updated_at=fill.created_at,
        cash_usd=cash,
        market_value_usd=value,
        equity_usd=cash + value,
        realized_pnl_usd=realized,
        unrealized_pnl_usd=unrealized,
        total_pnl_usd=realized + unrealized,
        fees_paid_usd=fees_paid,
    )
