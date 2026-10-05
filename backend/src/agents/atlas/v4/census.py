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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from src.agents.atlas.models import AtlasSourceFailure, ChainSnapshot
from src.agents.atlas.v4.control import (
    CustodyChainRefused,
    CustodyQuery,
    PositionControlFacts,
    PositionCustodyAdapter,
)
from src.agents.atlas.v4.custody.resolver import (
    DEFAULT_ADAPTERS,
    is_externally_owned,
    resolve_owner,
)
from src.agents.atlas.v4.custody.template import code_keccak
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
# Range read failures a smaller window can plausibly answer: the provider
# refused the span, cut or garbled a large answer, or ran out of time on it.
# Anything else -- rate limits, credentials, chain identity, connectivity, an
# unavailable provider -- is not about the window, and asking more often would
# only make it worse, so it fails the census as it always did.
SPLITTABLE_LOG_FAILURES = frozenset({ErrorCode.RPC_ERROR, ErrorCode.CONTRACT, ErrorCode.TIMEOUT})
# The smallest window a failing range is narrowed to: one block. A range that
# fails even there fails the census.
MIN_LOG_SPAN = 1


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

    async def storage(self, address: str, slot: str, block: int) -> str: ...


class PoolControlChainRefused(Exception):
    """The source answers for a different chain. A hard stop, never a gap."""


@dataclass(frozen=True)
class CensusBounds:
    # The largest window one `eth_getLogs` request starts with, and the most
    # the RPC client accepts (`MAX_EVENT_LOG_SPAN`). A maximum, not a promise:
    # a provider that refuses it gets the same unanswered window again, halved,
    # until one is answered (`_scan`). Robinhood Chain produces roughly ten
    # blocks a second, so a full window is close to three hours of history.
    chunk_blocks: int = 100_000
    # Every read counts: logs -- each narrowing retry included -- calls, code,
    # balances and block headers. Never exceeded, never reset.
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
    # Runtime code per position owner, read once per census.
    owner_codes: dict[str, str] = field(default_factory=dict)

    def spend(self) -> None:
        """Claim one read before it is made. With the budget gone, no read happens."""
        if self.requests >= self.bounds.max_requests:
            raise _Incomplete(self.bound_gap, AtlasSourceFailure.INCOMPLETE_RESULT)
        self.requests += 1


@dataclass(frozen=True)
class _CustodyReads:
    """The custody adapters' reads: pinned to the census block, charged to its budget."""

    run: _Run
    block: int

    async def code(self, address: str) -> str:
        self.run.spend()
        return await self.run.reads.code(address, self.block)

    async def storage(self, address: str, slot: str) -> str:
        self.run.spend()
        return await self.run.reads.storage(address, slot, self.block)

    async def call(self, address: str, selector: str) -> str:
        self.run.spend()
        return await self.run.reads.call(address, selector, self.block)


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
    # What a contract holding a position NFT is verified to allow. Order is
    # irrelevant: each adapter recognises only its own exact code.
    custody: tuple[PositionCustodyAdapter, ...] = DEFAULT_ADAPTERS

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
        except CustodyChainRefused:
            raise PoolControlChainRefused(snapshot.chain) from None
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

        # The cheapest conceivable discovery: both orientations, every window
        # at the largest span, none refused. If even that exceeds what is left
        # of the budget the census cannot complete, and saying so now spends
        # nothing. Anything this does not rule out is attempted.
        windows = -(-(block - from_block + 1) // self.bounds.chunk_blocks)
        if run.requests + 2 * windows > self.bounds.max_requests:
            raise _Incomplete(
                PoolControlGap.CENSUS_BOUNDS_EXCEEDED, AtlasSourceFailure.INCOMPLETE_RESULT
            )
        found = await self._initializations(run, deployment.pool_manager, token, from_block, block)
        pools = [await self._pool(run, deployment, item, block) for item in found]

        run.phase = PoolControlGap.POSITION_FACTS_INCOMPLETE
        run.bound_gap = PoolControlGap.POSITION_FACTS_INCOMPLETE
        positions = await self._positions(run, deployment, pools, token, snapshot)

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

    async def _scan(
        self,
        run: _Run,
        address: str,
        topics: tuple[str | None, ...],
        start: int,
        end: int,
        accept: Callable[[tuple[ChainLog, ...], int, int], None],
    ) -> None:
        """Every matching log over ``[start, end]``, window by window, in order.

        One stream: one contract, one topic filter, one range. Each window is
        one budgeted read. A window the provider fails to answer in a way a
        smaller window might fix is asked again from the same block, half as
        wide; the narrower span that worked is kept for the rest of the stream.
        A failed window is never read as "no logs": the cursor moves only past
        windows that were answered and accepted, so the accepted windows tile
        the range exactly -- no gap, no overlap, each boundary block once.
        Narrowing is sequential, never a tree of parallel halves, and stops at
        one block: a range unanswerable even there fails the census.
        """
        span = self.bounds.chunk_blocks
        cursor = start
        while cursor <= end:
            window_end = min(cursor + span - 1, end)
            run.spend()
            try:
                logs = await self.reads.logs(address, topics, cursor, window_end)
            except RuntimeFailure as error:
                width = window_end - cursor + 1
                if error.code not in SPLITTABLE_LOG_FAILURES or width <= MIN_LOG_SPAN:
                    raise
                span = max(MIN_LOG_SPAN, width // 2)
                continue
            accept(logs, cursor, window_end)
            cursor = window_end + 1

    async def _initializations(
        self,
        run: _Run,
        manager: str,
        token: str,
        start: int,
        end: int,
    ) -> list[InitializeEvent]:
        topic = address_topic(token)
        found: dict[str, InitializeEvent] = {}

        def accept(logs: tuple[ChainLog, ...], low: int, high: int) -> None:
            for log in logs:
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
                    or not low <= event.block_number <= high
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

        # The token as currency0, then as currency1: no quote asset is assumed.
        for topics in ((INITIALIZE_TOPIC, None, topic), (INITIALIZE_TOPIC, None, None, topic)):
            await self._scan(run, manager, topics, start, end, accept)
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
        snapshot: ChainSnapshot,
    ) -> list[V4PositionFacts]:
        block = snapshot.block_number
        invalid = _Incomplete(
            PoolControlGap.POSITION_FACTS_INCOMPLETE, AtlasSourceFailure.INVALID_RESPONSE
        )
        net: dict[tuple[str, str, int, int, str], int] = {}
        events = 0
        for pool in pools:

            def accept(
                logs: tuple[ChainLog, ...], low: int, high: int, pool: V4PoolFacts = pool
            ) -> None:
                nonlocal events
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
                    if change.pool_id != pool.pool_id or not low <= change.block_number <= high:
                        raise invalid
                    key = (
                        pool.pool_id,
                        change.sender,
                        change.tick_lower,
                        change.tick_upper,
                        change.salt,
                    )
                    net[key] = net.get(key, 0) + change.liquidity_delta

            await self._scan(
                run,
                deployment.pool_manager,
                (MODIFY_LIQUIDITY_TOPIC, pool.pool_id),
                pool.created_block,
                block,
                accept,
            )
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
                        run,
                        deployment,
                        sender,
                        pool_key,
                        salt,
                        lower,
                        upper,
                        liquidity,
                        controlled,
                        snapshot,
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
        deployment: V4Deployment,
        manager: str,
        pool_key: str,
        salt: str,
        lower: int,
        upper: int,
        liquidity: int,
        controlled: int,
        snapshot: ChainSnapshot,
    ) -> V4PositionFacts:
        """A verified PositionManager position, its ERC-721 owner and what that means.

        The owner is recorded exactly as `ownerOf` answered. Whether it controls
        the principal is a separate fact: an account does, a contract only as
        far as a verified custody adapter establishes.
        """
        block = snapshot.block_number
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
        control = (
            None
            if owner is None
            else await self._control(run, deployment, manager, token_id, pool_key, owner, snapshot)
        )
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
            control=control,
        )

    async def _control(
        self,
        run: _Run,
        deployment: V4Deployment,
        manager: str,
        token_id: int,
        pool_key: str,
        owner: str,
        snapshot: ChainSnapshot,
    ) -> PositionControlFacts:
        reads = _CustodyReads(run, snapshot.block_number)
        if owner not in run.owner_codes:
            run.owner_codes[owner] = await reads.code(owner)
        code = run.owner_codes[owner]
        query = CustodyQuery(
            chain=snapshot.chain,
            chain_id=snapshot.chain_id,
            block=snapshot.block_number,
            pool_manager=deployment.pool_manager,
            position_manager=manager,
            token_id=token_id,
            pool_id=pool_key,
            owner=owner,
            owner_code=code,
            owner_code_hash=code_keccak(code),
        )
        return await resolve_owner(reads, query, self.custody)

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
