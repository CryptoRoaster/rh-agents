"""Exact runtime-code templates, pinned from a reproducible build.

A Solidity contract's runtime code is fixed by its source, compiler and
settings, except for its ``immutable`` values, which the constructor writes
into known byte ranges. A template is that code with every immutable range
zeroed, pinned by length and keccak256, plus the ranges themselves. Deployed
code matches only when it has exactly that length, the same immutable value at
every occurrence of each immutable, and -- with those ranges zeroed -- exactly
the pinned hash. A contract that merely shares an ABI, a name, or most of the
bytes does not match, and neither does a proxy or a clone in front of it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from src.agents.atlas.v4.keccak import keccak256

WORD_BYTES = 32


@dataclass(frozen=True)
class CodeTemplate:
    """One contract version's runtime code, as built from a pinned commit."""

    contract: str
    version: str
    source_repository: str
    source_commit: str
    # Compiler, optimizer, EVM version and metadata settings of the build.
    build: str
    length: int
    masked_keccak: str
    # Immutable name -> every byte offset at which its 32-byte value appears.
    immutables: Mapping[str, tuple[int, ...]]

    def __post_init__(self) -> None:
        offsets = sorted(offset for items in self.immutables.values() for offset in items)
        pairs = zip(offsets, offsets[1:], strict=False)
        if any(later < earlier + WORD_BYTES for earlier, later in pairs) or any(
            not 0 <= offset <= self.length - WORD_BYTES for offset in offsets
        ):
            raise ValueError("Immutable ranges must lie inside the code and not overlap")


def code_keccak(code: str) -> str:
    return "0x" + keccak256(bytes.fromhex(code[2:])).hex()


def match_template(code: str, template: CodeTemplate) -> dict[str, int] | None:
    """The immutable values of ``code`` when it is exactly ``template``, else None."""
    if not code.startswith("0x") or len(code) % 2:
        return None
    try:
        raw = bytes.fromhex(code[2:])
    except ValueError:
        return None
    if len(raw) != template.length:
        return None
    masked = bytearray(raw)
    values: dict[str, int] = {}
    for name, offsets in template.immutables.items():
        seen = {raw[offset : offset + WORD_BYTES] for offset in offsets}
        if len(seen) != 1:
            # The compiler writes one value to every occurrence; differing
            # values mean this is not that compiler's output.
            return None
        values[name] = int.from_bytes(seen.pop(), "big")
        for offset in offsets:
            masked[offset : offset + WORD_BYTES] = bytes(WORD_BYTES)
    if "0x" + keccak256(bytes(masked)).hex() != template.masked_keccak:
        return None
    return values


def as_address(value: int) -> str | None:
    """An immutable that must be an address; None when the word is not one."""
    if value >> 160:
        return None
    return "0x" + format(value, "040x")
