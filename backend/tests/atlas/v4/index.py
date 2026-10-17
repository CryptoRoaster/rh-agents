"""An in-memory pool index store with the same contract as the SQL one."""

from dataclasses import dataclass, field

from src.agents.atlas.v4.pool_index import (
    IndexConflict,
    IndexCoverage,
    IndexedPool,
)


@dataclass
class MemoryPoolIndexStore:
    coverages: dict[tuple[int, str], IndexCoverage] = field(default_factory=dict)
    pools: dict[tuple[int, str], dict[str, IndexedPool]] = field(default_factory=dict)

    async def coverage(self, chain_id: int, pool_manager: str) -> IndexCoverage | None:
        return self.coverages.get((chain_id, pool_manager))

    async def begin(self, coverage: IndexCoverage) -> None:
        key = (coverage.chain_id, coverage.pool_manager)
        if key in self.coverages:
            raise IndexConflict("coverage exists")
        self.coverages[key] = coverage
        self.pools[key] = {}

    async def extend(
        self, coverage: IndexCoverage, pools: tuple[IndexedPool, ...], expected_through: int
    ) -> None:
        key = (coverage.chain_id, coverage.pool_manager)
        current = self.coverages.get(key)
        if current is None or current.through_block != expected_through:
            raise IndexConflict("coverage moved")
        known = self.pools[key]
        if any(pool.pool_id in known for pool in pools):
            raise IndexConflict("pool initialized twice")
        known.update({pool.pool_id: pool for pool in pools})
        self.coverages[key] = coverage

    async def rewind(self, coverage: IndexCoverage, expected_through: int) -> None:
        key = (coverage.chain_id, coverage.pool_manager)
        current = self.coverages.get(key)
        if current is None or current.through_block != expected_through:
            raise IndexConflict("coverage moved")
        self.pools[key] = {
            pool_id: pool
            for pool_id, pool in self.pools[key].items()
            if pool.block_number <= coverage.through_block
        }
        self.coverages[key] = coverage

    async def pools_with(
        self, chain_id: int, pool_manager: str, token: str, before_block: int
    ) -> tuple[IndexedPool, ...]:
        return tuple(
            sorted(
                (
                    pool
                    for pool in self.pools.get((chain_id, pool_manager), {}).values()
                    if token in (pool.currency0, pool.currency1)
                    and pool.block_number < before_block
                ),
                key=lambda pool: (pool.block_number, pool.pool_id),
            )
        )
