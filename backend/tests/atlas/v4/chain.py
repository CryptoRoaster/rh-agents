"""A deterministic in-memory chain that answers the V4 census like a real node.

Scenarios are declared as pools and liquidity changes; every answer the census
reads — Initialize and ModifyLiquidity logs, the packed Slot0 and position
slots behind `extsload`, `getPoolAndPositionInfo`, `ownerOf`, hook `owner()`,
contract code and the PoolManager's token balance — is *derived* from that
declaration with the same protocol arithmetic, so the fixture can only agree
with itself the way a chain does. Failure injection is explicit per test.

No RPC is involved anywhere.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from src.agents.atlas.v4.census import ChainLog
from src.agents.atlas.v4.math import position_amounts, sqrt_price_at_tick
from src.agents.atlas.v4.protocol import (
    EXTSLOAD_SELECTOR,
    INITIALIZE_TOPIC,
    MODIFY_LIQUIDITY_TOPIC,
    OWNER_OF_SELECTOR,
    OWNER_SELECTOR,
    POOL_AND_POSITION_INFO_SELECTOR,
    POOL_MANAGER_SELECTOR,
    pool_id,
    pool_state_slot,
    position_slot,
    word,
)
from src.runtime.models import ErrorCode, RuntimeFailure

NATIVE = "0x" + "0" * 40
# The Robinhood Chain V4 deployment, as configured in production: the official
# custody registry binds its FeeSplitters to exactly these two contracts.
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
SECOND_POSITION_MANAGER = "0x" + "a0" * 19 + "03"
CHAIN_ID = 4663
CREATED_BLOCK = 77_186_957
HEAD_BLOCK = 77_205_000
GENESIS_TIME = 1_759_000_000
CODE = "0x6080604052"


def hex_word(value: int) -> str:
    return "0x" + word(value).hex()


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


@dataclass(frozen=True)
class Pool:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hook: str = NATIVE
    created_block: int = CREATED_BLOCK + 10
    # The pool's current tick; its price is the exact price at that tick.
    tick: int = 0
    initialized: bool = True

    @property
    def id(self) -> str:
        return pool_id(self.currency0, self.currency1, self.fee, self.tick_spacing, self.hook)

    @property
    def sqrt_price(self) -> int:
        return sqrt_price_at_tick(self.tick) if self.initialized else 0


@dataclass(frozen=True)
class Change:
    pool: Pool
    sender: str
    lower: int
    upper: int
    delta: int
    salt: str
    block: int

    @property
    def key(self) -> tuple[str, str, int, int, str]:
        return (self.pool.id, self.sender, self.lower, self.upper, self.salt)


def token_salt(token_id: int) -> str:
    return hex_word(token_id)


@dataclass
class FakeV4Chain:
    token: str
    network_id: int = CHAIN_ID
    head: int = HEAD_BLOCK
    created_block: int = CREATED_BLOCK
    pool_manager: str = POOL_MANAGER
    position_managers: tuple[str, ...] = (POSITION_MANAGER,)
    pools: list[Pool] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    # (manager, token id) -> current ERC-721 owner. Absent: `ownerOf` reverts.
    nft_owners: dict[tuple[str, int], str] = field(default_factory=dict)
    # hook -> owner; absent: `owner()` reverts.
    hook_owners: dict[str, str] = field(default_factory=dict)
    # Addresses with contract code. Everything else is an externally owned account.
    contracts: set[str] = field(default_factory=set)
    # EIP-7702 delegated accounts.
    delegated: set[str] = field(default_factory=set)
    # Exact runtime code for chosen contracts, such as a custody contract.
    codes: dict[str, str] = field(default_factory=dict)
    # (address, slot) -> word, for EIP-1967 proxy slots and the like.
    slots: dict[tuple[str, str], int] = field(default_factory=dict)
    # A manager bound to some other PoolManager, for verification failures.
    manager_binding: dict[str, str] = field(default_factory=dict)
    # Token units the PoolManager holds beyond every position: fees, rounding.
    extra_pool_balance: int = 0
    # Fail the n-th request (1-based) with this error.
    fail_at: int | None = None
    fail_with: ErrorCode = ErrorCode.TIMEOUT
    # Rewrites applied to logs and storage answers, for tampering tests.
    tamper_log: Callable[[ChainLog], ChainLog] | None = None
    tamper_liquidity: Callable[[int], int] | None = None
    # A provider log-range limit: wider `eth_getLogs` windows fail with
    # `span_failure`, as a real provider refuses or cuts them.
    max_log_span: int | None = None
    span_failure: ErrorCode = ErrorCode.RPC_ERROR
    # A block no window containing it can be read across, whatever its width.
    unreadable_block: int | None = None
    # Every window that was answered, in order, for coverage checks.
    answered_windows: list[tuple[tuple[str | None, ...], int, int]] = field(default_factory=list)
    requests: int = 0
    log_requests: list[tuple[str, tuple[str | None, ...], int, int]] = field(default_factory=list)

    # ------------------------------------------------------------- declaration

    def add_pool(self, pool: Pool) -> Pool:
        self.pools.append(pool)
        if int(pool.hook, 16):
            self.contracts.add(pool.hook)
        return pool

    def mint(
        self,
        pool: Pool,
        owner: str,
        token_id: int,
        lower: int,
        upper: int,
        liquidity: int,
        *,
        manager: str = POSITION_MANAGER,
        block: int | None = None,
    ) -> None:
        """A PositionManager mint: liquidity under the manager, salt = token id."""
        self.changes.append(
            Change(
                pool,
                manager,
                lower,
                upper,
                liquidity,
                token_salt(token_id),
                block or pool.created_block + 1,
            )
        )
        self.nft_owners[(manager, token_id)] = owner

    def modify(
        self,
        pool: Pool,
        sender: str,
        lower: int,
        upper: int,
        delta: int,
        salt: str,
        *,
        block: int | None = None,
    ) -> None:
        self.changes.append(
            Change(pool, sender, lower, upper, delta, salt, block or pool.created_block + 2)
        )

    # ------------------------------------------------------------------ derived

    def net_liquidity(self, block: int) -> dict[tuple[str, str, int, int, str], int]:
        net: dict[tuple[str, str, int, int, str], int] = {}
        for change in self.changes:
            if change.block <= block:
                net[change.key] = net.get(change.key, 0) + change.delta
        return net

    def controlled(self, block: int) -> dict[tuple[str, str, int, int, str], int]:
        """Token units each active position holds, with the census's own math."""
        by_id = {pool.id: pool for pool in self.pools}
        held: dict[tuple[str, str, int, int, str], int] = {}
        for key, liquidity in self.net_liquidity(block).items():
            if liquidity <= 0:
                continue
            pool = by_id[key[0]]
            amount0, amount1 = position_amounts(pool.sqrt_price, key[2], key[3], liquidity)
            held[key] = amount0 if pool.currency0 == self.token else amount1
        return held

    def pool_balance(self, block: int) -> int:
        return sum(self.controlled(block).values()) + self.extra_pool_balance

    # --------------------------------------------------------------------- port

    def _tick(self) -> None:
        self.requests += 1
        if self.fail_at is not None and self.requests == self.fail_at:
            raise RuntimeFailure(self.fail_with)

    async def chain_id(self) -> int:
        self._tick()
        return self.network_id

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[ChainLog, ...]:
        self._tick()
        self.log_requests.append((address, topics, start, end))
        if self.max_log_span is not None and end - start + 1 > self.max_log_span:
            raise RuntimeFailure(self.span_failure)
        if self.unreadable_block is not None and start <= self.unreadable_block <= end:
            raise RuntimeFailure(ErrorCode.RPC_ERROR)
        self.answered_windows.append((topics, start, end))
        if address != self.pool_manager:
            return ()
        found: list[ChainLog] = []
        if topics[0] == INITIALIZE_TOPIC:
            side = len(topics) - 3  # 0: token as currency0, 1: token as currency1
            for pool in self.pools:
                currency = (pool.currency0, pool.currency1)[side]
                if currency != self.token or not start <= pool.created_block <= end:
                    continue
                found.append(
                    ChainLog(
                        block_number=pool.created_block,
                        topics=(
                            INITIALIZE_TOPIC,
                            pool.id,
                            address_topic(pool.currency0),
                            address_topic(pool.currency1),
                        ),
                        data="0x"
                        + "".join(
                            word(value).hex()
                            for value in (
                                pool.fee,
                                pool.tick_spacing,
                                int(pool.hook, 16),
                                pool.sqrt_price,
                                pool.tick,
                            )
                        ),
                    )
                )
        elif topics[0] == MODIFY_LIQUIDITY_TOPIC:
            for change in self.changes:
                if change.pool.id != topics[1] or not start <= change.block <= end:
                    continue
                found.append(
                    ChainLog(
                        block_number=change.block,
                        topics=(
                            MODIFY_LIQUIDITY_TOPIC,
                            change.pool.id,
                            address_topic(change.sender),
                        ),
                        data="0x"
                        + "".join(
                            word(value).hex()
                            for value in (
                                change.lower,
                                change.upper,
                                change.delta,
                                int(change.salt, 16),
                            )
                        ),
                    )
                )
        if self.tamper_log is not None:
            found = [self.tamper_log(item) for item in found]
        return tuple(found)

    async def block_timestamp(self, number: int) -> int:
        self._tick()
        return GENESIS_TIME + number // 10

    def _has_code(self, address: str, block: int) -> bool:
        if address == self.token:
            return block >= self.created_block
        return (
            address in self.contracts
            or address in self.codes
            or address in (self.pool_manager, *self.position_managers)
        )

    async def code(self, address: str, block: int) -> str:
        self._tick()
        if address in self.delegated:
            return "0xef0100" + "11" * 20
        if address in self.codes:
            return self.codes[address]
        return CODE if self._has_code(address, block) else "0x"

    async def storage(self, address: str, slot: str, block: int) -> str:
        self._tick()
        return hex_word(self.slots.get((address, slot), 0))

    async def call(self, address: str, selector: str, block: int) -> str:
        self._tick()
        if selector == POOL_MANAGER_SELECTOR and address in self.position_managers:
            return hex_word(int(self.manager_binding.get(address, self.pool_manager), 16))
        if selector == OWNER_SELECTOR and address in self.hook_owners:
            return hex_word(int(self.hook_owners[address], 16))
        raise RuntimeFailure(ErrorCode.RPC_ERROR)

    async def call_word(self, address: str, selector: str, argument: str, block: int) -> str:
        self._tick()
        value = int(argument, 16)
        if address == self.pool_manager and selector == EXTSLOAD_SELECTOR:
            return hex_word(self._storage(value, block))
        if address in self.position_managers and selector == OWNER_OF_SELECTOR:
            owner = self.nft_owners.get((address, value))
            if owner is None:
                raise RuntimeFailure(ErrorCode.RPC_ERROR)
            return hex_word(int(owner, 16))
        if address in self.position_managers and selector == POOL_AND_POSITION_INFO_SELECTOR:
            return self._position_info(address, value)
        raise RuntimeFailure(ErrorCode.RPC_ERROR)

    def _storage(self, slot: int, block: int) -> int:
        for pool in self.pools:
            if pool_state_slot(pool.id) == slot:
                return pool.sqrt_price | ((pool.tick % (1 << 24)) << 160)
        for key, liquidity in self.net_liquidity(block).items():
            if position_slot(*key) == slot:
                if self.tamper_liquidity is not None:
                    return self.tamper_liquidity(liquidity)
                return liquidity
        return 0

    def _position_info(self, manager: str, token_id: int) -> str:
        salt = token_salt(token_id)
        for change in self.changes:
            if change.sender == manager and change.salt == salt:
                pool = change.pool
                info = (
                    (int(pool.id, 16) >> 56 << 56)
                    | ((change.upper % (1 << 24)) << 32)
                    | ((change.lower % (1 << 24)) << 8)
                )
                return "0x" + "".join(
                    word(value).hex()
                    for value in (
                        int(pool.currency0, 16),
                        int(pool.currency1, 16),
                        pool.fee,
                        pool.tick_spacing,
                        int(pool.hook, 16),
                        info,
                    )
                )
        return "0x" + "00" * 32 * 6

    async def balance_of(self, token: str, holder: str, block: int) -> int:
        self._tick()
        if token != self.token or holder != self.pool_manager:
            return 0
        return self.pool_balance(block)
