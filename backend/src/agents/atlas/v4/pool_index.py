"""A PoolManager-wide index of V4 pools, so pool discovery covers its whole history.

v4-core initializes a pool for any currency address, whether or not code exists
there yet, and a token's address is known before it is deployed. A census that
searches for a token's pools from the token's creation block can therefore miss
a pool initialized earlier -- and liquidity added to it later sits in the
PoolManager as an unexplained surplus (review finding B1 on PR #99).

This index records every `Initialize` the verified PoolManager emitted, of any
token, from its verified deployment block:

* the deployment block is located by the PoolManager's own code presence and
  verified (code at it, none before), never configured or guessed;
* it advances only to a confirmed block (head minus the runtime's confirmation
  lag), window by window, each window stored atomically with the hash of its
  last block and only if the coverage has not moved meanwhile;
* before advancing it re-reads the hash of the block it last confirmed; if the
  chain no longer agrees, it rewinds and reads that stretch again;
* every pool id must hash from the key the event reported, exactly as in the
  census, and a pool can be initialized once;
* every read is budgeted; running out keeps what was confirmed and the next
  run resumes there. A failed read is never "no pools".

The census asks it only for pools of one token initialized before the token's
creation block, and only when the coverage reaches that block. Everything about
those pools is still read live at the census block; the index only says which
pools exist.
"""

from dataclasses import dataclass, replace
from typing import Literal, Protocol

from src.agents.atlas.v4.protocol import (
    INITIALIZE_TOPIC,
    V4DecodeError,
    V4Deployment,
    decode_initialize,
    pool_id,
)
from src.runtime.models import ErrorCode, RuntimeFailure

# How far a reorganised tip is rewound before reading again. Far deeper than
# any reorganisation the confirmation lag admits; a rewound block that itself
# no longer agrees is rewound again on the next run.
REWIND_BLOCKS = 1_000
# The widest window one log read starts with: the RPC client's own cap.
MAX_WINDOW = 100_000
SPLITTABLE = frozenset({ErrorCode.RPC_ERROR, ErrorCode.CONTRACT, ErrorCode.TIMEOUT})


class IndexConflict(Exception):
    """The stored coverage moved, or a pool would be recorded twice."""


@dataclass(frozen=True)
class IndexedPool:
    pool_id: str
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str
    block_number: int


@dataclass(frozen=True)
class IndexCoverage:
    """Every Initialize from ``deployment_block`` through ``through_block`` is stored."""

    chain_id: int
    pool_manager: str
    deployment_block: int
    through_block: int
    through_hash: str


class PoolIndexStore(Protocol):
    async def coverage(self, chain_id: int, pool_manager: str) -> IndexCoverage | None: ...

    async def begin(self, coverage: IndexCoverage) -> None: ...

    async def extend(
        self, coverage: IndexCoverage, pools: tuple[IndexedPool, ...], expected_through: int
    ) -> None: ...

    async def rewind(self, coverage: IndexCoverage, expected_through: int) -> None: ...

    async def pools_with(
        self, chain_id: int, pool_manager: str, token: str, before_block: int
    ) -> tuple[IndexedPool, ...]: ...


class LogRecord(Protocol):
    @property
    def block_number(self) -> int: ...

    @property
    def topics(self) -> tuple[str, ...]: ...

    @property
    def data(self) -> str: ...


class PoolIndexReadPort(Protocol):
    """The chain reads the indexer needs. Raises `RuntimeFailure`."""

    async def chain_id(self) -> int: ...

    async def head_block(self) -> int: ...

    async def block_hash(self, number: int) -> str: ...

    async def code(self, address: str, block: int) -> str: ...

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[LogRecord, ...]: ...


@dataclass(frozen=True)
class IndexAdvance:
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    through_block: int | None
    requests: int
    rewound: bool = False
    reason: str | None = None


class _Budget(Exception):
    pass


@dataclass
class V4PoolIndexer:
    """Advances the index of one verified PoolManager. Holds no state between runs."""

    reads: PoolIndexReadPort
    store: PoolIndexStore
    deployment: V4Deployment
    confirmations: int
    max_requests: int
    window: int = MAX_WINDOW
    requests: int = 0

    def __post_init__(self) -> None:
        if self.confirmations < 0 or not 0 < self.window <= MAX_WINDOW or self.max_requests <= 0:
            raise ValueError("Index bounds must be positive and within the RPC window cap")

    def _spend(self) -> None:
        if self.requests >= self.max_requests:
            raise _Budget
        self.requests += 1

    async def advance(self) -> IndexAdvance:
        self.requests = 0
        coverage: IndexCoverage | None = None
        rewound = False
        try:
            self._spend()
            if await self.reads.chain_id() != self.deployment.chain_id:
                return IndexAdvance("FAILED", None, self.requests, reason="CHAIN_ID_MISMATCH")
            self._spend()
            safe = await self.reads.head_block() - self.confirmations
            coverage = await self.store.coverage(
                self.deployment.chain_id, self.deployment.pool_manager
            )
            if coverage is None:
                coverage = await self._begin(safe)
            else:
                coverage, rewound = await self._agreed(coverage)
            while coverage.through_block < safe:
                coverage = await self._window(coverage, safe)
        except _Budget:
            return IndexAdvance(
                "PARTIAL",
                None if coverage is None else coverage.through_block,
                self.requests,
                rewound,
            )
        except (RuntimeFailure, V4DecodeError, IndexConflict) as error:
            reason = error.code.value if isinstance(error, RuntimeFailure) else type(error).__name__
            return IndexAdvance(
                "FAILED",
                None if coverage is None else coverage.through_block,
                self.requests,
                rewound,
                reason,
            )
        return IndexAdvance("COMPLETE", coverage.through_block, self.requests, rewound)

    async def _begin(self, safe: int) -> IndexCoverage:
        """Locate the PoolManager's deployment block by its own code, and start there."""
        manager = self.deployment.pool_manager
        self._spend()
        if len(await self.reads.code(manager, safe)) <= 2:
            raise V4DecodeError("no PoolManager code at the confirmed block")
        low, high = 0, safe
        while low < high:
            middle = (low + high) // 2
            self._spend()
            if len(await self.reads.code(manager, middle)) > 2:
                high = middle
            else:
                low = middle + 1
        # `low` has code and, by the search, `low - 1` has none.
        before = low - 1
        self._spend()
        coverage = IndexCoverage(
            chain_id=self.deployment.chain_id,
            pool_manager=manager,
            deployment_block=low,
            through_block=before,
            through_hash=await self.reads.block_hash(max(before, 0)),
        )
        await self.store.begin(coverage)
        return coverage

    async def _agreed(self, coverage: IndexCoverage) -> tuple[IndexCoverage, bool]:
        """The coverage, rewound if the chain no longer agrees with its last block."""
        if coverage.through_block < coverage.deployment_block:
            return coverage, False
        self._spend()
        if await self.reads.block_hash(coverage.through_block) == coverage.through_hash:
            return coverage, False
        target = max(coverage.deployment_block - 1, coverage.through_block - REWIND_BLOCKS)
        self._spend()
        rewound = replace(
            coverage,
            through_block=target,
            through_hash=await self.reads.block_hash(max(target, 0)),
        )
        await self.store.rewind(rewound, expected_through=coverage.through_block)
        return rewound, True

    async def _window(self, coverage: IndexCoverage, safe: int) -> IndexCoverage:
        start = coverage.through_block + 1
        span = self.window
        while True:
            end = min(start + span - 1, safe)
            self._spend()
            try:
                logs = await self.reads.logs(
                    self.deployment.pool_manager, (INITIALIZE_TOPIC,), start, end
                )
            except RuntimeFailure as error:
                if error.code not in SPLITTABLE or end == start:
                    raise
                span = max(1, (end - start + 1) // 2)
                continue
            break
        pools = _pools(logs, start, end)
        self._spend()
        extended = replace(
            coverage, through_block=end, through_hash=await self.reads.block_hash(end)
        )
        await self.store.extend(extended, pools, expected_through=coverage.through_block)
        return extended


def _pools(logs: tuple[LogRecord, ...], start: int, end: int) -> tuple[IndexedPool, ...]:
    found: dict[str, IndexedPool] = {}
    for log in logs:
        number = log.block_number
        event = decode_initialize(log.topics, log.data, number)
        expected = pool_id(
            event.currency0, event.currency1, event.fee, event.tick_spacing, event.hooks
        )
        if expected != event.pool_id or not start <= number <= end or event.pool_id in found:
            raise V4DecodeError("an Initialize that is not what the PoolManager emits")
        found[event.pool_id] = IndexedPool(
            pool_id=event.pool_id,
            currency0=event.currency0,
            currency1=event.currency1,
            fee=event.fee,
            tick_spacing=event.tick_spacing,
            hooks=event.hooks,
            block_number=number,
        )
    return tuple(sorted(found.values(), key=lambda pool: (pool.block_number, pool.pool_id)))


@dataclass(frozen=True)
class IndexedHistory:
    """The pools of one token initialized before a block, from a covering index."""

    deployment_block: int
    pools: tuple[IndexedPool, ...]


class PoolHistory(Protocol):
    async def history(
        self, chain_id: int, pool_manager: str, token: str, before_block: int
    ) -> IndexedHistory | None: ...


@dataclass(frozen=True)
class PoolIndexHistory:
    """Answers the census only when the index covers everything before the block."""

    store: PoolIndexStore

    async def history(
        self, chain_id: int, pool_manager: str, token: str, before_block: int
    ) -> IndexedHistory | None:
        coverage = await self.store.coverage(chain_id, pool_manager)
        if coverage is None or coverage.through_block < before_block - 1:
            return None
        pools = await self.store.pools_with(chain_id, pool_manager, token, before_block)
        return IndexedHistory(deployment_block=coverage.deployment_block, pools=pools)
