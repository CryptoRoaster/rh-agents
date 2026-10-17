"""The pool index in PostgreSQL: every write atomic and conditional on the coverage."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.atlas.v4.pool_index import IndexConflict, IndexCoverage, IndexedPool
from src.data.tables import V4PoolIndexCoverageRow, V4PoolIndexPoolRow


def _coverage(row: V4PoolIndexCoverageRow) -> IndexCoverage:
    return IndexCoverage(
        chain_id=row.chain_id,
        pool_manager=row.pool_manager,
        deployment_block=row.deployment_block,
        through_block=row.through_block,
        through_hash=row.through_hash,
    )


@dataclass(frozen=True)
class SqlPoolIndexStore:
    sessions: async_sessionmaker[AsyncSession]

    async def coverage(self, chain_id: int, pool_manager: str) -> IndexCoverage | None:
        async with self.sessions() as session:
            row = await session.get(V4PoolIndexCoverageRow, (chain_id, pool_manager))
            return None if row is None else _coverage(row)

    async def begin(self, coverage: IndexCoverage) -> None:
        async with self.sessions.begin() as session:
            if await session.get(
                V4PoolIndexCoverageRow, (coverage.chain_id, coverage.pool_manager)
            ):
                raise IndexConflict("coverage exists")
            session.add(
                V4PoolIndexCoverageRow(
                    chain_id=coverage.chain_id,
                    pool_manager=coverage.pool_manager,
                    deployment_block=coverage.deployment_block,
                    through_block=coverage.through_block,
                    through_hash=coverage.through_hash,
                    updated_at=datetime.now(UTC),
                )
            )

    async def _move(
        self, session: AsyncSession, coverage: IndexCoverage, expected_through: int
    ) -> None:
        moved = await session.execute(
            update(V4PoolIndexCoverageRow)
            .where(
                V4PoolIndexCoverageRow.chain_id == coverage.chain_id,
                V4PoolIndexCoverageRow.pool_manager == coverage.pool_manager,
                V4PoolIndexCoverageRow.through_block == expected_through,
            )
            .values(
                through_block=coverage.through_block,
                through_hash=coverage.through_hash,
                updated_at=datetime.now(UTC),
            )
        )
        if getattr(moved, "rowcount", 0) != 1:
            raise IndexConflict("coverage moved")

    async def extend(
        self, coverage: IndexCoverage, pools: tuple[IndexedPool, ...], expected_through: int
    ) -> None:
        async with self.sessions.begin() as session:
            await self._move(session, coverage, expected_through)
            if pools:
                known = await session.scalars(
                    select(V4PoolIndexPoolRow.pool_id).where(
                        V4PoolIndexPoolRow.chain_id == coverage.chain_id,
                        V4PoolIndexPoolRow.pool_manager == coverage.pool_manager,
                        V4PoolIndexPoolRow.pool_id.in_([pool.pool_id for pool in pools]),
                    )
                )
                if known.first() is not None:
                    raise IndexConflict("pool initialized twice")
            session.add_all(
                V4PoolIndexPoolRow(
                    chain_id=coverage.chain_id,
                    pool_manager=coverage.pool_manager,
                    pool_id=pool.pool_id,
                    currency0=pool.currency0,
                    currency1=pool.currency1,
                    fee=pool.fee,
                    tick_spacing=pool.tick_spacing,
                    hooks=pool.hooks,
                    block_number=pool.block_number,
                )
                for pool in pools
            )

    async def rewind(self, coverage: IndexCoverage, expected_through: int) -> None:
        async with self.sessions.begin() as session:
            await self._move(session, coverage, expected_through)
            await session.execute(
                delete(V4PoolIndexPoolRow).where(
                    V4PoolIndexPoolRow.chain_id == coverage.chain_id,
                    V4PoolIndexPoolRow.pool_manager == coverage.pool_manager,
                    V4PoolIndexPoolRow.block_number > coverage.through_block,
                )
            )

    async def pools_with(
        self, chain_id: int, pool_manager: str, token: str, before_block: int
    ) -> tuple[IndexedPool, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(V4PoolIndexPoolRow)
                .where(
                    V4PoolIndexPoolRow.chain_id == chain_id,
                    V4PoolIndexPoolRow.pool_manager == pool_manager,
                    or_(
                        V4PoolIndexPoolRow.currency0 == token,
                        V4PoolIndexPoolRow.currency1 == token,
                    ),
                    V4PoolIndexPoolRow.block_number < before_block,
                )
                .order_by(V4PoolIndexPoolRow.block_number, V4PoolIndexPoolRow.pool_id)
            )
            return tuple(
                IndexedPool(
                    pool_id=row.pool_id,
                    currency0=row.currency0,
                    currency1=row.currency1,
                    fee=row.fee,
                    tick_spacing=row.tick_spacing,
                    hooks=row.hooks,
                    block_number=row.block_number,
                )
                for row in rows
            )
