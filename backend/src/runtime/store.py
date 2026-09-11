"""PostgreSQL ownership and auditable event persistence; no transport credentials."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.data.tables import EvmCursorRow, EvmLogRow, RuntimeAuditRow
from src.runtime.models import ErrorCode, Log, RuntimeFailure


@asynccontextmanager
async def ownership(engine: AsyncEngine, key: int) -> AsyncIterator[bool]:
    """Session lock held on a dedicated connection; OS disconnect releases ownership.

    SQLite cannot claim production ownership. Unit tests inject their own owner.
    """
    if engine.dialect.name != "postgresql":
        raise RuntimeFailure(ErrorCode.CONFIGURATION)
    async with engine.connect() as connection:
        acquired = bool(
            await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        )
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise


class RuntimeStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self.sessions = sessions
        self.clock = clock

    def audit_row(
        self, stream: str, source: str, kind: str, run_id: UUID, payload: dict[str, Any]
    ) -> RuntimeAuditRow:
        return RuntimeAuditRow(
            id=uuid4(),
            run_id=run_id,
            stream=stream,
            source=source,
            kind=kind,
            recorded_at=self.clock.now(),
            payload=payload,
        )

    async def audit(
        self, stream: str, source: str, kind: str, run_id: UUID, payload: dict[str, Any]
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(self.audit_row(stream, source, kind, run_id, payload))

    async def latest(self, stream: str, kind: str | None = None) -> RuntimeAuditRow | None:
        statement = select(RuntimeAuditRow).where(RuntimeAuditRow.stream == stream)
        if kind:
            statement = statement.where(RuntimeAuditRow.kind == kind)
        async with self.sessions() as session:
            return (
                await session.scalars(
                    statement.order_by(
                        RuntimeAuditRow.recorded_at.desc(), RuntimeAuditRow.sequence.desc()
                    ).limit(1)
                )
            ).first()

    async def cursor(self, chain: str) -> EvmCursorRow | None:
        async with self.sessions() as session:
            return await session.get(EvmCursorRow, chain)

    async def save_log(
        self,
        session: AsyncSession,
        chain: str,
        log: Log,
        observed_at: datetime,
        run_id: UUID,
        decoders: tuple[str, ...],
    ) -> None:
        identity = uuid5(
            NAMESPACE_URL,
            f"evm:{chain}:mainnet:{log.block_hash}:{log.transaction_hash}:{log.log_index}",
        )
        payload = log.model_dump(mode="json")
        # Decoder routing is observation provenance, not blockchain content identity.
        insert = (
            pg_insert(EvmLogRow)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(EvmLogRow)
        )
        await session.execute(
            insert.values(
                id=identity,
                chain=chain,
                network="mainnet",
                block_number=log.block_number,
                source="evm_rpc",
                observed_at=observed_at,
                recorded_at=self.clock.now(),
                session_id=run_id,
                payload={"event": payload, "decoders": list(decoders)},
            ).on_conflict_do_nothing(index_elements=["id"])
        )
        existing = await session.get(EvmLogRow, identity)
        if existing is None or existing.payload["event"] != payload:
            raise RuntimeFailure(ErrorCode.CONFLICT)
