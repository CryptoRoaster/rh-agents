"""Keccak-256 as Ethereum uses it, in pure integer Python.

Ethereum's hash is the original Keccak submission (padding byte 0x01), not
NIST SHA3-256 (padding byte 0x06), so ``hashlib.sha3_256`` gives different
answers and cannot be used. Nothing here needs speed: it hashes a handful of
pool keys and storage slots per snapshot, so a small, auditable implementation
is preferred over a new cryptography dependency.
"""

ROUND_CONSTANTS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
MASK = (1 << 64) - 1
RATE = 136


def _rotate(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & MASK if shift else value


def _permute(state: list[list[int]]) -> None:
    for constant in ROUND_CONSTANTS:
        parity = [lanes[0] ^ lanes[1] ^ lanes[2] ^ lanes[3] ^ lanes[4] for lanes in state]
        for x in range(5):
            delta = parity[(x - 1) % 5] ^ _rotate(parity[(x + 1) % 5], 1)
            for y in range(5):
                state[x][y] ^= delta
        moved = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                moved[y][(2 * x + 3 * y) % 5] = _rotate(state[x][y], ROTATIONS[x][y])
        for x in range(5):
            for y in range(5):
                state[x][y] = moved[x][y] ^ ((~moved[(x + 1) % 5][y]) & moved[(x + 2) % 5][y])
        state[0][0] ^= constant


def keccak256(data: bytes) -> bytes:
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % RATE:
        padded.append(0)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), RATE):
        block = padded[offset : offset + RATE]
        for index in range(RATE // 8):
            lane = int.from_bytes(block[index * 8 : index * 8 + 8], "little")
            state[index % 5][index // 5] ^= lane
        _permute(state)
    return b"".join(state[index % 5][index // 5].to_bytes(8, "little") for index in range(4))


def selector(signature: str) -> str:
    """The 4-byte function selector of a canonical Solidity signature."""
    return "0x" + keccak256(signature.encode()).hex()[:8]
