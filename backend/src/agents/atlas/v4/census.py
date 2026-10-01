"""Bounded, chain-verified census of a token's Uniswap V4 pools and positions.

Pools are found from the PoolManager's own `Initialize` events with the token
as either currency, so a pool nobody traded and no aggregator lists is found
exactly like the busiest one. Positions are rebuilt from `ModifyLiquidity`
events and then checked against the PoolManager's storage at the pinned block,
so a reconstruction that disagrees with the chain is refused rather than used.

Everything is bounded: block chunks, requests, pools, events, positions and
wall-clock time. A bound that is reached makes the census incomplete; it never
truncates quietly and reports itself complete. A failed read is unknown, never
"nothing there".
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from src.agents.atlas.models import AtlasSourceFailure, ChainSnapshot
from src.agents.atlas.v4.math import LiquidityMathError, position_amounts
from src.agents.atlas.v4.models import (
    MAX_POOLS,
    MAX_POSITIONS,
    HookOwnerStatus,
    PoolControlGap,
    PositionKind,
    PositionOwnerStatus,
    V4Census,
    V4HookFacts,
    V4PoolFacts,
    V4PositionFacts,
)
from src.agents.atlas.v4.protocol import (
    EXTSLOAD_SELECTOR,
    INITIALIZE_TOPIC,
    MODIFY_LIQUIDITY_TOPIC,
    OWNER_OF_SELECTOR,
    OWNER_SELECTOR,
    POOL_AND_POSITION_INFO_SELECTOR,
    POOL_MANAGER_SELECTOR,
    V4_DEPLOYMENTS,
    InitializeEvent,
    V4DecodeError,
    V4Deployment,
    decode_address_result,
    decode_initialize,
    decode_liquidity,
    decode_modify_liquidity,
    decode_pool_and_position_info,
    decode_slot0,
    hook_permissions,
    pool_id,
    pool_state_slot,
    position_slot,
    slot_word,
)
from src.markets.models import Availability
from src.runtime.models import ErrorCode, RuntimeFailure

FAILURES: dict[ErrorCode, AtlasSourceFailure] = {
    ErrorCode.TIMEOUT: AtlasSourceFailure.TIMEOUT,
    ErrorCode.RATE_LIMITED: AtlasSourceFailure.RATE_LIMIT,
    ErrorCode.CONFIGURATION: AtlasSourceFailure.NOT_CONFIGURED,
    ErrorCode.CONTRACT: AtlasSourceFailure.INVALID_RESPONSE,
}
# A call that reverted or answered malformed bytes. For an optional read such
# as a hook's `owner()` this means "not established", never a transport error.
CALL_REFUSALS = frozenset({ErrorCode.RPC_ERROR, ErrorCode.CONTRACT})
# EIP-7702 delegation designator: an externally owned account with delegated code.
DELEGATION_PREFIX = "0xef0100"


@dataclass(frozen=True)
class ChainLog:
    block_number: int
    topics: tuple[str, ...]
    data: str


class V4ChainReadPort(Protocol):
    """The narrow read surface the census needs. Raises `RuntimeFailure`."""

    async def chain_id(self) -> int: ...

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[ChainLog, ...]: ...

    async def block_timestamp(self, number: int) -> int: ...

    async def code(self, address: str, block: int) -> str: ...

    async def call(self, address: str, selector: str, block: int) -> str: ...

    async def call_word(self, address: str, selector: str, argument: str, block: int) -> str: ...

    async def balance_of(self, token: str, holder: str, block: int) -> int: ...


class PoolControlChainRefused(Exception):
    """The source answers for a different chain. A hard stop, never a gap."""


@dataclass(frozen=True)
class CensusBounds:
    # Blocks per `eth_getLogs` request. Robinhood Chain produces roughly ten
    # blocks a second, so one chunk is about seventeen minutes of history.
    chunk_blocks: int = 10_000
    # Every read counts: logs, calls, code, balances and block headers.
    max_requests: int = 240
    max_pools: int = MAX_POOLS
    max_positions: int = MAX_POSITIONS
    max_position_events: int = 4_000
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not (
            0 < self.chunk_blocks <= 100_000
            and 0 < self.max_requests <= 1_000
            and 0 < self.max_pools <= MAX_POOLS
            and 0 < self.max_positions <= MAX_POSITIONS
            and 0 < self.max_position_events <= 20_000
            and 0 < self.timeout_seconds <= 120
        ):
            raise ValueError("Census bounds must be positive and within hard caps")


class _Incomplete(Exception):
    def __init__(self, gap: PoolControlGap, failure: AtlasSourceFailure | None) -> None:
        self.gap = gap
        self.failure = failure
        super().__init__(gap.value)


@dataclass
class _Run:
    """One census pass: a request budget and the phase failures are charged to."""

    reads: V4ChainReadPort
    bounds: CensusBounds
    requests: int = 0
    phase: PoolControlGap = PoolControlGap.CENSUS_UNAVAILABLE
    bound_gap: PoolControlGap = PoolControlGap.CENSUS_BOUNDS_EXCEEDED
    timestamps: dict[int, int] = field(default_factory=dict)
    # Pools proven before any later failure: they keep pool control required.
    pools_found: int = 0

    def spend(self) -> None:
        self.requests += 1
        if self.requests > self.bounds.max_requests:
            raise _Incomplete(self.bound_gap, AtlasSourceFailure.INCOMPLETE_RESULT)


def is_externally_owned(code: str) -> bool:
    """No code, or only an EIP-7702 delegation: an account a private key controls."""
    return code == "0x" or (code.startswith(DELEGATION_PREFIX) and len(code) == 2 + 46)


def block_ranges(start: int, end: int, chunk: int) -> tuple[tuple[int, int], ...]:
    return tuple((low, min(low + chunk - 1, end)) for low in range(start, end + 1, chunk))


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


@dataclass(frozen=True)
class V4PoolCensus:
    """Collects a `V4Census` for one chain. Contains no economic judgement."""

    reads: V4ChainReadPort
    chain: str
    bounds: CensusBounds = CensusBounds()
    deployments: Mapping[tuple[str, int], V4Deployment] = field(
        default_factory=lambda: dict(V4_DEPLOYMENTS)
    )
    source: str = "evm-rpc-v4-pool-manager"

    async def census(self, snapshot: ChainSnapshot, token: str, from_block: int | None) -> V4Census:
        if snapshot.chain != self.chain:
            raise PoolControlChainRefused(snapshot.chain)
        deployment = self.deployments.get((snapshot.chain, snapshot.chain_id))
        if deployment is None:
            return V4Census(
                status=Availability.UNAVAILABLE,
                gap=PoolControlGap.DEPLOYMENT_NOT_CONFIGURED,
                failure=AtlasSourceFailure.NOT_CONFIGURED,
                source=self.source,
                chain_id=snapshot.chain_id,
            )
        common = {
            "source": self.source,
            "chain_id": snapshot.chain_id,
            "pool_manager": deployment.pool_manager,
            "position_managers": deployment.position_managers,
            "scan_to_block": snapshot.block_number,
        }
        if from_block is None or from_block > snapshot.block_number:
            # Never guessed: scanning from an assumed block could miss the very
            # pool this exists to find, and scanning from genesis is unbounded.
            return V4Census(
                status=Availability.UNAVAILABLE,
                gap=PoolControlGap.CREATION_BLOCK_UNKNOWN,
                **common,  # type: ignore[arg-type]
            )
        run = _Run(self.reads, self.bounds)
        try:
            async with asyncio.timeout(self.bounds.timeout_seconds):
                return await self._collect(run, deployment, snapshot, token, from_block)
        except _Incomplete as error:
            gap, failure = error.gap, error.failure
        except TimeoutError:
            gap, failure = run.phase, AtlasSourceFailure.TIMEOUT
        except RuntimeFailure as error:
            if error.code == ErrorCode.CHAIN_ID_MISMATCH:
                raise PoolControlChainRefused(snapshot.chain) from None
            gap = run.phase
            failure = FAILURES.get(error.code, AtlasSourceFailure.UNAVAILABLE)
        return V4Census(
            status=Availability.UNAVAILABLE,
            gap=gap,
            failure=failure,
            scan_from_block=from_block,
            requests_made=run.requests,
            pools_found=run.pools_found,
            **common,  # type: ignore[arg-type]
        )

    async def _collect(
        self,
        run: _Run,
        deployment: V4Deployment,
        snapshot: ChainSnapshot,
        token: str,
        from_block: int,
    ) -> V4Census:
        block = snapshot.block_number
        run.spend()
        if await self.reads.chain_id() != snapshot.chain_id:
            raise PoolControlChainRefused(snapshot.chain)
        await self._verify_deployment(run, deployment, block)
        await self._verify_creation(run, token, from_block)

        ranges = block_ranges(from_block, block, self.bounds.chunk_blocks)
        if 2 * len(ranges) > self.bounds.max_requests:
            # Known before the first scan: the history is longer than the budget.
            raise _Incomplete(
                PoolControlGap.CENSUS_BOUNDS_EXCEEDED, AtlasSourceFailure.INCOMPLETE_RESULT
            )
        found = await self._initializations(run, deployment.pool_manager, token, ranges)
        pools = [await self._pool(run, deployment, item, block) for item in found]

        run.phase = PoolControlGap.POSITION_FACTS_INCOMPLETE
        run.bound_gap = PoolControlGap.POSITION_FACTS_INCOMPLETE
        positions = await self._positions(run, deployment, pools, token, block)

        run.spend()
        balance = await self.reads.balance_of(token, deployment.pool_manager, block)
        return V4Census(
            status=Availability.AVAILABLE,
            source=self.source,
            chain_id=snapshot.chain_id,
            pool_manager=deployment.pool_manager,
            position_managers=deployment.position_managers,
            scan_from_block=from_block,
            scan_to_block=block,
            requests_made=run.requests,
            pools=tuple(pools),
            positions=tuple(positions),
            pool_manager_balance_raw=balance,
        )

    async def _verify_deployment(self, run: _Run, deployment: V4Deployment, block: int) -> None:
        """Code at every address, and each PositionManager bound to this PoolManager.

        The binding is the PositionManager's immutable `poolManager()`, read at
        the pinned block. A name, label or ABI guess proves nothing.
        """
        unverified = _Incomplete(
            PoolControlGap.DEPLOYMENT_UNVERIFIED, AtlasSourceFailure.INVALID_RESPONSE
        )
        run.spend()
        if len(await self.reads.code(deployment.pool_manager, block)) <= 2:
            raise unverified
        for manager in deployment.position_managers:
            run.spend()
            if len(await self.reads.code(manager, block)) <= 2:
                raise unverified
            run.spend()
            try:
                bound = decode_address_result(
                    await self.reads.call(manager, POOL_MANAGER_SELECTOR, block)
                )
            except (V4DecodeError, RuntimeFailure) as error:
                if isinstance(error, RuntimeFailure) and error.code not in CALL_REFUSALS:
                    raise
                raise unverified from None
            if bound != deployment.pool_manager:
                raise unverified

    async def _verify_creation(self, run: _Run, token: str, created: int) -> None:
        """The claimed creation block, proven: no code before it, code at it.

        A provider's creation block is a claim. Scanning from a block later than
        the real one could miss a pool initialized in between, so the claim is
        checked against the chain's own state before the scan relies on it.
        """
        run.spend()
        before = "0x" if created == 0 else await self.reads.code(token, created - 1)
        run.spend()
        at = await self.reads.code(token, created)
        if before != "0x" or len(at) <= 2:
            raise _Incomplete(PoolControlGap.CREATION_BLOCK_UNKNOWN, None)

    async def _initializations(
        self,
        run: _Run,
        manager: str,
        token: str,
        ranges: tuple[tuple[int, int], ...],
    ) -> list[InitializeEvent]:
        topic = address_topic(token)
        found: dict[str, InitializeEvent] = {}
        for start, end in ranges:
            for topics in ((INITIALIZE_TOPIC, None, topic), (INITIALIZE_TOPIC, None, None, topic)):
                run.spend()
                for log in await self.reads.logs(manager, topics, start, end):
                    try:
                        event = decode_initialize(log.topics, log.data, log.block_number)
                    except V4DecodeError:
                        raise _Incomplete(
                            PoolControlGap.CENSUS_UNAVAILABLE, AtlasSourceFailure.INVALID_RESPONSE
                        ) from None
                    expected = pool_id(
                        event.currency0,
                        event.currency1,
                        event.fee,
                        event.tick_spacing,
                        event.hooks,
                    )
                    if (
                        expected != event.pool_id
                        or token not in (event.currency0, event.currency1)
                        or not start <= event.block_number <= end
                    ):
                        # The id the chain reported does not hash from the key it
                        # reported, or the log is not what was asked for.
                        raise _Incomplete(
                            PoolControlGap.POOL_KEY_MISMATCH, AtlasSourceFailure.INVALID_RESPONSE
                        )
                    if event.pool_id in found:
                        # A pool initializes once; a second Initialize is not V4.
                        raise _Incomplete(
                            PoolControlGap.POOL_KEY_MISMATCH, AtlasSourceFailure.INVALID_RESPONSE
                        )
                    found[event.pool_id] = event
                    run.pools_found = len(found)
                    if len(found) > self.bounds.max_pools:
                        raise _Incomplete(
                            PoolControlGap.CENSUS_BOUNDS_EXCEEDED,
                            AtlasSourceFailure.INCOMPLETE_RESULT,
                        )
        return sorted(found.values(), key=lambda item: (item.block_number, item.pool_id))

    async def _pool(
        self, run: _Run, deployment: V4Deployment, event: InitializeEvent, block: int
    ) -> V4PoolFacts:
        created_at = await self._timestamp(run, event.block_number)
        run.spend()
        try:
            sqrt_price, tick = decode_slot0(
                await self.reads.call_word(
                    deployment.pool_manager,
                    EXTSLOAD_SELECTOR,
                    slot_word(pool_state_slot(event.pool_id)),
                    block,
                )
            )
        except V4DecodeError:
            raise _Incomplete(
                PoolControlGap.CENSUS_UNAVAILABLE, AtlasSourceFailure.INVALID_RESPONSE
            ) from None
        hook = None
        if int(event.hooks, 16) != 0:
            hook = await self._hook(run, event.hooks, block)
        initialized = sqrt_price != 0
        return V4PoolFacts(
            pool_manager=deployment.pool_manager,
            pool_id=event.pool_id,
            currency0=event.currency0,
            currency1=event.currency1,
            fee=event.fee,
            tick_spacing=event.tick_spacing,
            hook=event.hooks,
            created_block=event.block_number,
            created_at=created_at,
            initialized=initialized,
            sqrt_price_x96=sqrt_price if initialized else None,
            tick=tick if initialized else None,
            hook_facts=hook,
        )

    async def _timestamp(self, run: _Run, number: int) -> datetime:
        if number not in run.timestamps:
            run.spend()
            run.timestamps[number] = await self.reads.block_timestamp(number)
        return datetime.fromtimestamp(run.timestamps[number], UTC)

    async def _hook(self, run: _Run, hook: str, block: int) -> V4HookFacts:
        run.spend()
        present = len(await self.reads.code(hook, block)) > 2
        owner: str | None = None
        if present:
            run.spend()
            try:
                owner = decode_address_result(await self.reads.call(hook, OWNER_SELECTOR, block))
            except V4DecodeError:
                owner = None
            except RuntimeFailure as error:
                if error.code not in CALL_REFUSALS:
                    raise
                # Reverted or answered something else: not established. A hook
                # without `owner()` may still be controlled by somebody.
                owner = None
        return V4HookFacts(
            address=hook,
            code_present=present,
            permissions=hook_permissions(hook),
            owner=owner,
            owner_status=(
                HookOwnerStatus.OWNER_READ if owner is not None else HookOwnerStatus.OWNER_UNKNOWN
            ),
        )

    async def _positions(
        self,
        run: _Run,
        deployment: V4Deployment,
        pools: list[V4PoolFacts],
        token: str,
        block: int,
    ) -> list[V4PositionFacts]:
        invalid = _Incomplete(
            PoolControlGap.POSITION_FACTS_INCOMPLETE, AtlasSourceFailure.INVALID_RESPONSE
        )
        net: dict[tuple[str, str, int, int, str], int] = {}
        events = 0
        for pool in pools:
            for start, end in block_ranges(pool.created_block, block, self.bounds.chunk_blocks):
                run.spend()
                logs = await self.reads.logs(
                    deployment.pool_manager, (MODIFY_LIQUIDITY_TOPIC, pool.pool_id), start, end
                )
                events += len(logs)
                if events > self.bounds.max_position_events:
                    raise _Incomplete(
                        PoolControlGap.POSITION_FACTS_INCOMPLETE,
                        AtlasSourceFailure.INCOMPLETE_RESULT,
                    )
                for log in logs:
                    try:
                        change = decode_modify_liquidity(log.topics, log.data, log.block_number)
                    except V4DecodeError:
                        raise invalid from None
                    if change.pool_id != pool.pool_id or not start <= change.block_number <= end:
                        raise invalid
                    key = (
                        pool.pool_id,
                        change.sender,
                        change.tick_lower,
                        change.tick_upper,
                        change.salt,
                    )
                    net[key] = net.get(key, 0) + change.liquidity_delta
        if any(value < 0 for value in net.values()):
            # More liquidity removed than was ever added: the history is not whole.
            raise invalid
        active = sorted((key, value) for key, value in net.items() if value > 0)
        if len(active) > self.bounds.max_positions:
            raise _Incomplete(
                PoolControlGap.POSITION_FACTS_INCOMPLETE, AtlasSourceFailure.INCOMPLETE_RESULT
            )
        by_id = {pool.pool_id: pool for pool in pools}
        managers = frozenset(deployment.position_managers)
        found: list[V4PositionFacts] = []
        for (pool_key, sender, lower, upper, salt), liquidity in active:
            pool = by_id[pool_key]
            run.spend()
            try:
                stored = decode_liquidity(
                    await self.reads.call_word(
                        deployment.pool_manager,
                        EXTSLOAD_SELECTOR,
                        slot_word(position_slot(pool_key, sender, lower, upper, salt)),
                        block,
                    )
                )
            except V4DecodeError:
                raise invalid from None
            if stored != liquidity or pool.sqrt_price_x96 is None:
                # Our reconstruction disagrees with the PoolManager itself.
                raise invalid
            try:
                amount0, amount1 = position_amounts(pool.sqrt_price_x96, lower, upper, liquidity)
            except LiquidityMathError:
                raise invalid from None
            controlled = amount0 if pool.currency0 == token else amount1
            if sender in managers:
                found.append(
                    await self._managed(
                        run, sender, pool_key, salt, lower, upper, liquidity, controlled, block
                    )
                )
            else:
                found.append(
                    await self._direct(
                        run, sender, pool_key, salt, lower, upper, liquidity, controlled, block
                    )
                )
        return found

    async def _managed(
        self,
        run: _Run,
        manager: str,
        pool_key: str,
        salt: str,
        lower: int,
        upper: int,
        liquidity: int,
        controlled: int,
        block: int,
    ) -> V4PositionFacts:
        """A verified PositionManager position: its ERC-721 owner controls it."""
        token_id = int(salt, 16)
        run.spend()
        try:
            info = decode_pool_and_position_info(
                await self.reads.call_word(
                    manager, POOL_AND_POSITION_INFO_SELECTOR, slot_word(token_id), block
                )
            )
        except V4DecodeError:
            info = None
        except RuntimeFailure as error:
            if error.code not in CALL_REFUSALS:
                raise
            info = None
        if (
            info is None
            or info.pool_id != pool_key
            or info.truncated_pool_id != int(pool_key, 16) >> 56
            or (info.tick_lower, info.tick_upper) != (lower, upper)
        ):
            # The manager does not bind this token id to this pool and range.
            raise _Incomplete(
                PoolControlGap.POSITION_FACTS_INCOMPLETE, AtlasSourceFailure.INVALID_RESPONSE
            )
        owner: str | None
        run.spend()
        try:
            owner = decode_address_result(
                await self.reads.call_word(manager, OWNER_OF_SELECTOR, slot_word(token_id), block)
            )
        except V4DecodeError:
            owner = None
        except RuntimeFailure as error:
            if error.code not in CALL_REFUSALS:
                raise
            owner = None
        return V4PositionFacts(
            kind=PositionKind.POSITION_MANAGER,
            pool_id=pool_key,
            owner_key=manager,
            salt=salt,
            position_manager=manager,
            token_id=token_id,
            tick_lower=lower,
            tick_upper=upper,
            liquidity=liquidity,
            owner=owner,
            owner_status=(
                PositionOwnerStatus.ATTRIBUTED
                if owner is not None
                else PositionOwnerStatus.OWNER_UNKNOWN
            ),
            controlled_token_raw=controlled,
        )

    async def _direct(
        self,
        run: _Run,
        sender: str,
        pool_key: str,
        salt: str,
        lower: int,
        upper: int,
        liquidity: int,
        controlled: int,
        block: int,
    ) -> V4PositionFacts:
        """A position held directly under some sender's owner key.

        Only the owner key can modify it, so the key is the protocol owner. It is
        the *economic* owner only when the key is an account a private key
        controls. A contract key — a hook, a custom manager, a vault — acts for
        whoever its code says, which the chain does not state, so the owner is
        unknown rather than guessed.
        """
        run.spend()
        code = await self.reads.code(sender, block)
        owner = sender if is_externally_owned(code) else None
        return V4PositionFacts(
            kind=PositionKind.DIRECT,
            pool_id=pool_key,
            owner_key=sender,
            salt=salt,
            tick_lower=lower,
            tick_upper=upper,
            liquidity=liquidity,
            owner=owner,
            owner_status=(
                PositionOwnerStatus.ATTRIBUTED
                if owner is not None
                else PositionOwnerStatus.OWNER_UNKNOWN
            ),
            controlled_token_raw=controlled,
        )
