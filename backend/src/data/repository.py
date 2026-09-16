from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import (
    AgentDecision,
    ExecutionResult,
    MarketSnapshot,
    OrderIntent,
    PnLSnapshot,
    Position,
    Record,
    RiskDecision,
    Trade,
    TradeIntent,
)
from src.data.tables import (
    AgentDecisionRow,
    Document,
    ExecutionRow,
    IntentRow,
    MarketRow,
    OrderRow,
    PnLRow,
    PositionRow,
    RiskRow,
    TradeRow,
)

TABLES: dict[type[Record], type[Document]] = {
    MarketSnapshot: MarketRow,
    AgentDecision: AgentDecisionRow,
    TradeIntent: IntentRow,
    RiskDecision: RiskRow,
    OrderIntent: OrderRow,
    ExecutionResult: ExecutionRow,
    Trade: TradeRow,
    PnLSnapshot: PnLRow,
}


async def append(session: AsyncSession, record: Record) -> None:
    table = TABLES[type(record)]
    payload = record.model_dump(mode="json")
    existing = await session.get(table, record.id)
    if existing:
        if existing.payload != payload:
            raise ValueError("An immutable record ID was reused with different content")
        return
    row = table(
        id=record.id,
        source=record.source,
        correlation_id=record.correlation_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        payload=payload,
    )
    if isinstance(row, RiskRow) and isinstance(record, RiskDecision):
        row.intent_id = record.intent_id
        row.market_snapshot_id = record.market_snapshot_id
    elif isinstance(row, OrderRow) and isinstance(record, OrderIntent):
        row.intent_id = record.intent.id
        row.risk_id = record.risk.id
    elif isinstance(row, ExecutionRow) and isinstance(record, ExecutionResult):
        row.order_id, row.intent_id = record.order_id, record.intent_id
    elif isinstance(row, TradeRow) and isinstance(record, Trade):
        row.execution_id, row.position_id = record.execution_id, record.position_id
    session.add(row)
    await session.flush()


def aware(value: datetime) -> datetime:
    # SQLite test adapter drops offsets; production PostgreSQL preserves timestamptz.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def read_position(row: PositionRow) -> Position:
    return Position(
        id=row.id,
        asset_id=row.asset_id,
        market_pair_id=row.market_pair_id,
        market_chain=row.market_chain,
        market_network=row.market_network,
        market_provider=row.market_provider,
        cycle_id=row.cycle_id,
        quantity=row.quantity,
        cost_basis_usd=row.cost_basis_usd,
        realized_pnl_usd=row.realized_pnl_usd,
        created_at=aware(row.created_at),
        updated_at=aware(row.updated_at),
        source=row.source,
        correlation_id=row.correlation_id,
    )


async def save_position(session: AsyncSession, position: Position) -> None:
    row = await session.get(PositionRow, position.id)
    if row is None:
        row = PositionRow(**position.model_dump())
        session.add(row)
    else:
        for key, value in position.model_dump().items():
            setattr(row, key, value)
    await session.flush()
