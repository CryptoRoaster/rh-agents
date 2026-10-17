"""The read port a V2/V3 LP-control census needs, its budget, and exact ABI helpers.

No implementation here reaches a chain. A future RPC adapter implements
`LpChainReads` over the managed read-only EVM client (as `v4/rpc.py` does for
the V4 census); until then the censuses run only against in-memory fakes.

Every read is at a pinned block, charged to one request budget, and log scans
are chunked to a bounded block span. Exhausting the budget is a gap, never a
truncated answer.
"""

from dataclasses import dataclass, field
from typing import Protocol

from src.agents.atlas.v4.keccak import keccak256, selector


@dataclass(frozen=True)
class LpLog:
    block_number: int
    log_index: int
    topics: tuple[str, ...]
    data: str


class CallReverted(Exception):
    """The call executed at the pinned block and reverted. Not a failed read."""


class ReadBudgetExhausted(Exception):
    """The census reached its request budget before it was complete."""


class LpChainReads(Protocol):
    """Every read a V2/V3 census makes. All but `chain_id` take a pinned block."""

    async def chain_id(self) -> int: ...

    async def block_timestamp(self, number: int) -> int: ...

    async def code(self, address: str, block: int) -> str: ...

    # `data` is the full calldata: selector and ABI-encoded arguments.
    async def call(self, address: str, data: str, block: int) -> str: ...

    async def storage(self, address: str, slot: str, block: int) -> str: ...

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[LpLog, ...]: ...


@dataclass
class BudgetedReads:
    """`LpChainReads`, each request charged against ``max_requests``."""

    reads: LpChainReads
    max_requests: int
    max_log_span: int
    used: int = field(default=0)

    def _charge(self) -> None:
        if self.used >= self.max_requests:
            raise ReadBudgetExhausted
        self.used += 1

    async def chain_id(self) -> int:
        self._charge()
        return await self.reads.chain_id()

    async def block_timestamp(self, number: int) -> int:
        self._charge()
        return await self.reads.block_timestamp(number)

    async def code(self, address: str, block: int) -> str:
        self._charge()
        return await self.reads.code(address, block)

    async def call(self, address: str, data: str, block: int) -> str:
        self._charge()
        return await self.reads.call(address, data, block)

    async def storage(self, address: str, slot: str, block: int) -> str:
        self._charge()
        return await self.reads.storage(address, slot, block)

    async def scan(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[LpLog, ...]:
        """Every log in [start, end], in chunks of at most ``max_log_span`` blocks."""
        found: list[LpLog] = []
        cursor = start
        while cursor <= end:
            upper = min(end, cursor + self.max_log_span - 1)
            self._charge()
            found.extend(await self.reads.logs(address, topics, cursor, upper))
            cursor = upper + 1
        return tuple(sorted(found, key=lambda item: (item.block_number, item.log_index)))

    def at(self, block: int) -> "BlockReads":
        return BlockReads(self, block)


@dataclass(frozen=True)
class BlockReads:
    """The V4 `CustodyReadPort` shape, bound to one block, for locker adapters."""

    reads: BudgetedReads
    block: int

    async def code(self, address: str) -> str:
        return await self.reads.code(address, self.block)

    async def storage(self, address: str, slot: str) -> str:
        return await self.reads.storage(address, slot, self.block)

    async def call(self, address: str, selector: str) -> str:
        return await self.reads.call(address, selector, self.block)


# ---------------------------------------------------------------------- ABI


class AbiDecodeError(ValueError):
    pass


def topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


def word(value: int) -> str:
    return format(value % (1 << 256), "064x")


def address_word(address: str) -> str:
    return "0" * 24 + address[2:]


def address_topic(address: str) -> str:
    return "0x" + address_word(address)


def calldata(signature: str, *words: str) -> str:
    return selector(signature) + "".join(words)


def words_of(result: str) -> list[int]:
    if not result.startswith("0x") or (len(result) - 2) % 64:
        raise AbiDecodeError("Not a sequence of ABI words")
    try:
        return [int(result[2 + 64 * i : 66 + 64 * i], 16) for i in range((len(result) - 2) // 64)]
    except ValueError as error:
        raise AbiDecodeError("Not hex") from error


def word_at(result: str, index: int) -> int:
    values = words_of(result)
    if index >= len(values):
        raise AbiDecodeError("Result too short")
    return values[index]


def as_address(value: int) -> str:
    if value >> 160:
        raise AbiDecodeError("Not an address word")
    return "0x" + format(value, "040x")


def as_int24(value: int) -> int:
    signed = value - (1 << 256) if value >> 255 else value
    if not -(1 << 23) <= signed < 1 << 23:
        raise AbiDecodeError("Not an int24 word")
    return signed


def as_uint(value: int, bits: int) -> int:
    if value >> bits:
        raise AbiDecodeError(f"Not a uint{bits} word")
    return value


def topic_int(value: str) -> int:
    return words_of(value)[0]
