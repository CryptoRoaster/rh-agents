"""Uniswap V4 protocol constants and exact decoders.

Everything here is a protocol fact that can be checked against the chain: event
signatures, function selectors, the PoolKey hash, the PoolManager storage
layout and the hook permission bits. No address is trusted for what it is
called; a deployment is only ever *configured* here, and is verified on-chain
before anything it reports is used.
"""

import re
from dataclasses import dataclass
from enum import IntEnum

from src.agents.atlas.v4.keccak import keccak256, selector

# keccak256 of the canonical event signatures, as emitted by the PoolManager.
INITIALIZE_TOPIC = (
    "0x"
    + keccak256(b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
)
MODIFY_LIQUIDITY_TOPIC = (
    "0x" + keccak256(b"ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)").hex()
)

OWNER_SELECTOR = selector("owner()")
OWNER_OF_SELECTOR = selector("ownerOf(uint256)")
EXTSLOAD_SELECTOR = selector("extsload(bytes32)")
POOL_MANAGER_SELECTOR = selector("poolManager()")
POOL_AND_POSITION_INFO_SELECTOR = selector("getPoolAndPositionInfo(uint256)")

# `StateLibrary.POOLS_SLOT` and `POSITIONS_OFFSET` in v4-core: pools live in a
# mapping at slot 6, and inside `Pool.State` the positions mapping is the
# seventh member (offset 6).
POOLS_SLOT = 6
POSITIONS_OFFSET = 6

ZERO_ADDRESS = "0x" + "0" * 40
WORD = re.compile(r"^0x[0-9a-f]{64}$")
UINT160_MASK = (1 << 160) - 1
UINT128_MASK = (1 << 128) - 1


class V4DecodeError(ValueError):
    """A chain answer that does not have the shape the protocol defines."""


class HookPermission(IntEnum):
    """`Hooks.sol` permission flags, by the address bit that encodes them."""

    BEFORE_INITIALIZE = 13
    AFTER_INITIALIZE = 12
    BEFORE_ADD_LIQUIDITY = 11
    AFTER_ADD_LIQUIDITY = 10
    BEFORE_REMOVE_LIQUIDITY = 9
    AFTER_REMOVE_LIQUIDITY = 8
    BEFORE_SWAP = 7
    AFTER_SWAP = 6
    BEFORE_DONATE = 5
    AFTER_DONATE = 4
    BEFORE_SWAP_RETURNS_DELTA = 3
    AFTER_SWAP_RETURNS_DELTA = 2
    AFTER_ADD_LIQUIDITY_RETURNS_DELTA = 1
    AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA = 0


def hook_permissions(hook: str) -> tuple[str, ...]:
    """The permissions a hook address commits to, from its low 14 bits.

    V4 reads permissions from the address itself, so this is exact protocol
    semantics rather than an inference: the PoolManager calls exactly these
    hooks and no others. Ordered from the highest bit down, so stable.
    """
    value = int(hook, 16)
    return tuple(item.name for item in HookPermission if value >> item.value & 1)


@dataclass(frozen=True)
class V4Deployment:
    """One configured V4 deployment on one chain. Verified before every use."""

    chain: str
    chain_id: int
    pool_manager: str
    position_managers: tuple[str, ...]


# Configuration, not trust. Each address was observed acting in its role on the
# chain (Initialize and ModifyLiquidity emitted by the PoolManager; position
# NFTs minted by the PositionManager), and every snapshot re-verifies code
# presence and the PositionManager's immutable `poolManager()` binding at the
# pinned block before reading anything through them.
V4_DEPLOYMENTS: dict[tuple[str, int], V4Deployment] = {
    ("robinhood", 4663): V4Deployment(
        chain="robinhood",
        chain_id=4663,
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        position_managers=("0x58daec3116aae6d93017baaea7749052e8a04fa7",),
    ),
}


def word(value: int) -> bytes:
    """ABI encoding of one signed or unsigned integer as a 32-byte word."""
    return (value % (1 << 256)).to_bytes(32, "big")


def address_word(address: str) -> bytes:
    return word(int(address, 16))


def pool_id(currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str) -> str:
    """`PoolKey.toId()`: keccak256 of the ABI-encoded five-field key."""
    encoded = (
        address_word(currency0)
        + address_word(currency1)
        + word(fee)
        + word(tick_spacing)
        + address_word(hooks)
    )
    return "0x" + keccak256(encoded).hex()


def pool_state_slot(pool: str) -> int:
    """The PoolManager storage slot of `pools[poolId]` (its packed Slot0)."""
    return int.from_bytes(keccak256(bytes.fromhex(pool[2:]) + word(POOLS_SLOT)), "big")


def position_key(owner: str, tick_lower: int, tick_upper: int, salt: str) -> bytes:
    """`Position.calculatePositionKey`: owner, int24 ticks and salt, packed."""
    return keccak256(
        bytes.fromhex(owner[2:])
        + (tick_lower % (1 << 24)).to_bytes(3, "big")
        + (tick_upper % (1 << 24)).to_bytes(3, "big")
        + bytes.fromhex(salt[2:])
    )


def position_slot(pool: str, owner: str, tick_lower: int, tick_upper: int, salt: str) -> int:
    """The slot of `pools[poolId].positions[positionKey]`, whose low 128 bits are liquidity."""
    mapping = pool_state_slot(pool) + POSITIONS_OFFSET
    key = position_key(owner, tick_lower, tick_upper, salt)
    return int.from_bytes(keccak256(key + word(mapping)), "big")


def slot_word(slot: int) -> str:
    return "0x" + word(slot).hex()


def words(data: str, count: int) -> tuple[int, ...]:
    """Exactly ``count`` 32-byte words, or a decode error. Never padded or cut."""
    if not isinstance(data, str) or re.fullmatch(r"0x[0-9a-fA-F]*", data) is None:
        raise V4DecodeError("not hex")
    body = data[2:]
    if len(body) != 64 * count:
        raise V4DecodeError("unexpected length")
    return tuple(int(body[index * 64 : index * 64 + 64], 16) for index in range(count))


def as_address(value: int) -> str:
    if value >> 160:
        raise V4DecodeError("dirty address word")
    return "0x" + format(value, "040x")


def as_signed(value: int, bits: int) -> int:
    """A sign-extended ``intN`` word, refusing anything not canonically extended."""
    signed = value - (1 << 256) if value >> 255 else value
    if not -(1 << (bits - 1)) <= signed < 1 << (bits - 1):
        raise V4DecodeError("value outside its declared width")
    return signed


def as_unsigned(value: int, bits: int) -> int:
    if value >> bits:
        raise V4DecodeError("value outside its declared width")
    return value


def topic_address(topic: str) -> str:
    return as_address(int(topic, 16))


@dataclass(frozen=True)
class InitializeEvent:
    pool_id: str
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str
    block_number: int


def decode_initialize(topics: tuple[str, ...], data: str, block_number: int) -> InitializeEvent:
    if len(topics) != 4 or topics[0] != INITIALIZE_TOPIC:
        raise V4DecodeError("not an Initialize event")
    fee, spacing, hooks, sqrt_price, tick = words(data, 5)
    as_unsigned(sqrt_price, 160)
    as_signed(tick, 24)
    return InitializeEvent(
        pool_id=topics[1],
        currency0=topic_address(topics[2]),
        currency1=topic_address(topics[3]),
        fee=as_unsigned(fee, 24),
        tick_spacing=as_signed(spacing, 24),
        hooks=as_address(hooks),
        block_number=block_number,
    )


@dataclass(frozen=True)
class ModifyLiquidityEvent:
    pool_id: str
    sender: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: str
    block_number: int


def decode_modify_liquidity(
    topics: tuple[str, ...], data: str, block_number: int
) -> ModifyLiquidityEvent:
    if len(topics) != 3 or topics[0] != MODIFY_LIQUIDITY_TOPIC:
        raise V4DecodeError("not a ModifyLiquidity event")
    lower, upper, delta, salt = words(data, 4)
    return ModifyLiquidityEvent(
        pool_id=topics[1],
        sender=topic_address(topics[2]),
        tick_lower=as_signed(lower, 24),
        tick_upper=as_signed(upper, 24),
        liquidity_delta=as_signed(delta, 256),
        salt="0x" + format(salt, "064x"),
        block_number=block_number,
    )


def decode_slot0(data: str) -> tuple[int, int]:
    """(sqrtPriceX96, tick) from the packed Slot0 word read via `extsload`."""
    (value,) = words(data, 1)
    sqrt_price = value & UINT160_MASK
    tick = (value >> 160) & 0xFFFFFF
    return sqrt_price, tick - (1 << 24) if tick >> 23 else tick


def decode_liquidity(data: str) -> int:
    """`Position.State.liquidity`: the low 128 bits of the position's first slot."""
    (value,) = words(data, 1)
    return value & UINT128_MASK


@dataclass(frozen=True)
class PositionInfo:
    """`PositionManager.getPoolAndPositionInfo`, decoded."""

    pool_id: str
    tick_lower: int
    tick_upper: int
    truncated_pool_id: int


def decode_pool_and_position_info(data: str) -> PositionInfo:
    currency0, currency1, fee, spacing, hooks, info = words(data, 6)
    key_id = pool_id(
        as_address(currency0),
        as_address(currency1),
        as_unsigned(fee, 24),
        as_signed(spacing, 24),
        as_address(hooks),
    )
    lower = (info >> 8) & 0xFFFFFF
    upper = (info >> 32) & 0xFFFFFF
    return PositionInfo(
        pool_id=key_id,
        tick_lower=lower - (1 << 24) if lower >> 23 else lower,
        tick_upper=upper - (1 << 24) if upper >> 23 else upper,
        truncated_pool_id=info >> 56,
    )


def decode_address_result(data: str) -> str:
    (value,) = words(data, 1)
    return as_address(value)
