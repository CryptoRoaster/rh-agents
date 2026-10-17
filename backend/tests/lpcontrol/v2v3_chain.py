"""FIXTURE ONLY: deterministic in-memory V2/V3 chains for the LP-control censuses.

Scenarios are declared as LP-token operations (V2) or positions (V3); every
answer the census reads -- Transfer / PairCreated / PoolCreated / Mint /
IncreaseLiquidity logs, balances, totalSupply, pool position storage, slot0,
active liquidity, NFT owners and contract code -- is derived from that
declaration with the protocol's own semantics, so the fixture can only agree
with itself the way a chain does. Failure injection is explicit per test.

No RPC is involved anywhere, and no address here is a real deployment.
"""

from dataclasses import dataclass, field

from src.agents.atlas.lpcontrol.custody import LockerQuery, LockerVerdict
from src.agents.atlas.lpcontrol.models import BURN_DEAD, ZERO_ADDRESS, HolderState
from src.agents.atlas.lpcontrol.reads import (
    CallReverted,
    LpLog,
    address_topic,
    address_word,
    word,
)
from src.agents.atlas.lpcontrol.registry import (
    LpDeploymentRegistry,
    V2Deployment,
    V3Deployment,
)
from src.agents.atlas.lpcontrol.v2_census import PAIR_CREATED_TOPIC, TRANSFER_TOPIC
from src.agents.atlas.lpcontrol.v3_census import (
    INCREASE_LIQUIDITY_TOPIC,
    MINT_TOPIC,
    POOL_CREATED_TOPIC,
    position_key,
)
from src.agents.atlas.v4.control import CustodyReadPort
from src.agents.atlas.v4.custody.template import CodeTemplate, code_keccak
from src.agents.atlas.v4.keccak import keccak256, selector
from tests.lpcontrol.v2v3_fixtures import (
    FIXTURE_CHAIN,
    FIXTURE_CHAIN_ID,
    NPM,
    QUOTE,
    TOKEN,
    V2_FACTORY,
    V2_PAIR,
    V3_FACTORY,
    V3_POOL,
)

START_BLOCK = 100
CREATED_BLOCK = 1_000
HEAD = 50_000
GENESIS = 1_760_000_000
V2_PAIR_CODE = "0x6080604052" + "a2" * 40
ACCOUNT_CODE = "0x"
VAULT_CODE = "0x6080604052" + "77" * 20
# The FIXTURE locker's exact runtime code: a stand-in for a verified adapter.
FIXTURE_LOCKER_CODE = "0x6080604052" + "fe" * 30
MINIMAL_PROXY_CODE = "0x363d3d373d3d3d363d73" + "77" * 20 + "5af43d82803e903d91602b57fd5bf3"


class FixtureLockerAdapter:
    """FIXTURE ONLY: recognises `FIXTURE_LOCKER_CODE` as a permanent lock."""

    name = "fixture-locker"

    async def verify(self, reads: CustodyReadPort, query: LockerQuery) -> LockerVerdict | None:
        if query.holder_code != FIXTURE_LOCKER_CODE:
            return None
        return LockerVerdict(
            state=HolderState.PERMANENTLY_LOCKED,
            proof_contract="FixtureLocker",
            proof_version="fixture",
        )


FIXTURE_LOCKERS = (FixtureLockerAdapter(),)


def hexword(value: int) -> str:
    return "0x" + word(value)


def _int_topic(value: int) -> str:
    return "0x" + word(value)


@dataclass
class _Base:
    chain_id_value: int = FIXTURE_CHAIN_ID
    codes: dict[str, str] = field(default_factory=dict)
    logs_list: list[tuple[str, LpLog]] = field(default_factory=list)
    # (address, calldata) -> forced answer, or an exception to raise.
    overrides: dict[tuple[str, str], str | Exception] = field(default_factory=dict)
    dropped_logs: set[tuple[str, int]] = field(default_factory=set)
    requests: int = 0

    def _emit(self, address: str, block: int, topics: tuple[str, ...], data: str) -> None:
        index = len(self.logs_list)
        self.logs_list.append((address, LpLog(block, index, topics, data)))

    async def chain_id(self) -> int:
        self.requests += 1
        return self.chain_id_value

    async def block_timestamp(self, number: int) -> int:
        self.requests += 1
        return GENESIS + number

    async def code(self, address: str, block: int) -> str:
        self.requests += 1
        return self.codes.get(address, ACCOUNT_CODE)

    async def storage(self, address: str, slot: str, block: int) -> str:
        self.requests += 1
        return hexword(0)

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[LpLog, ...]:
        self.requests += 1
        found = []
        for emitter, log in self.logs_list:
            if emitter != address or not start <= log.block_number <= end:
                continue
            if (emitter, log.log_index) in self.dropped_logs:
                continue
            if all(
                want is None or (i < len(log.topics) and log.topics[i] == want)
                for i, want in enumerate(topics)
            ):
                found.append(log)
        return tuple(found)

    async def call(self, address: str, data: str, block: int) -> str:
        self.requests += 1
        forced = self.overrides.get((address, data))
        if isinstance(forced, Exception):
            raise forced
        if forced is not None:
            return forced
        return self.answer(address, data)

    def answer(self, address: str, data: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


# ----------------------------------------------------------------------- V2


def v2_deployment(code: str = V2_PAIR_CODE) -> V2Deployment:
    return V2Deployment(
        chain=FIXTURE_CHAIN,
        chain_id=FIXTURE_CHAIN_ID,
        venue="fixture-v2",
        factory=V2_FACTORY,
        pair_code_hash=code_keccak(code),
        start_block=START_BLOCK,
    )


@dataclass
class FakeV2Chain(_Base):
    pair: str = V2_PAIR
    factory: str = V2_FACTORY
    token0: str = TOKEN
    token1: str = QUOTE
    supply: int = 0
    balances: dict[str, int] = field(default_factory=dict)
    block: int = CREATED_BLOCK

    def __post_init__(self) -> None:
        self.codes.setdefault(self.pair, V2_PAIR_CODE)
        self.codes.setdefault(self.factory, "0x60806040")
        self._emit(
            self.factory,
            CREATED_BLOCK,
            (PAIR_CREATED_TOPIC, address_topic(self.token0), address_topic(self.token1)),
            "0x" + address_word(self.pair) + word(1),
        )

    def _transfer_log(self, sender: str, receiver: str, value: int) -> None:
        self.block += 1
        self._emit(
            self.pair,
            self.block,
            (TRANSFER_TOPIC, address_topic(sender), address_topic(receiver)),
            "0x" + word(value),
        )

    def _move(self, sender: str, receiver: str, value: int) -> None:
        self.balances[sender] = self.balances.get(sender, 0) - value
        assert self.balances[sender] >= 0
        self.balances[receiver] = self.balances.get(receiver, 0) + value

    # The pair's own semantics (UniswapV2Pair / UniswapV2ERC20).
    def first_mint(self, to: str, value: int) -> "FakeV2Chain":
        """`mint` on an empty pair: MINIMUM_LIQUIDITY to the zero address, the rest to ``to``."""
        self._transfer_log(ZERO_ADDRESS, ZERO_ADDRESS, 1_000)
        self.supply += 1_000
        self.balances[ZERO_ADDRESS] = self.balances.get(ZERO_ADDRESS, 0) + 1_000
        return self.mint(to, value)

    def mint(self, to: str, value: int) -> "FakeV2Chain":
        self._transfer_log(ZERO_ADDRESS, to, value)
        self.supply += value
        self.balances[to] = self.balances.get(to, 0) + value
        return self

    def transfer(self, sender: str, receiver: str, value: int) -> "FakeV2Chain":
        """`transfer`, including to the zero or dead address: the supply is unchanged."""
        self._transfer_log(sender, receiver, value)
        self._move(sender, receiver, value)
        return self

    def burn(self, holder: str, value: int) -> "FakeV2Chain":
        """Remove liquidity: LP to the pair, then the pair's `_burn`."""
        self.transfer(holder, self.pair, value)
        self._transfer_log(self.pair, ZERO_ADDRESS, value)
        self.balances[self.pair] -= value
        self.supply -= value
        return self

    def answer(self, address: str, data: str) -> str:
        sel = data[:10]
        if address == self.pair:
            if sel == selector("token0()"):
                return "0x" + address_word(self.token0)
            if sel == selector("token1()"):
                return "0x" + address_word(self.token1)
            if sel == selector("factory()"):
                return "0x" + address_word(self.factory)
            if sel == selector("totalSupply()"):
                return hexword(self.supply)
            if sel == selector("balanceOf(address)"):
                return hexword(self.balances.get("0x" + data[-40:], 0))
        if address == self.factory and sel == selector("getPair(address,address)"):
            return "0x" + address_word(self.pair)
        raise CallReverted(f"{address} {sel}")


def v2_registry(code: str = V2_PAIR_CODE) -> LpDeploymentRegistry:
    return LpDeploymentRegistry(v2=(v2_deployment(code),))


# ----------------------------------------------------------------------- V3

# Fixture pool code: four immutables (factory, token0, token1, fee) around a stub.
_PREFIX = bytes.fromhex("60806040")
_OFFSETS = {"factory": (4,), "token0": (36,), "token1": (68,), "fee": (100,)}
_LENGTH = 4 + 4 * 32 + 4


def v3_pool_code(factory: str, token0: str, token1: str, fee: int) -> str:
    body = bytearray(_PREFIX + bytes(4 * 32) + bytes.fromhex("5b5b5b00"))
    for name, value in (
        ("factory", int(factory, 16)),
        ("token0", int(token0, 16)),
        ("token1", int(token1, 16)),
        ("fee", fee),
    ):
        offset = _OFFSETS[name][0]
        body[offset : offset + 32] = value.to_bytes(32, "big")
    return "0x" + body.hex()


def _masked_hash() -> str:
    masked = bytearray(bytes.fromhex(v3_pool_code(V3_FACTORY, TOKEN, QUOTE, 3000)[2:]))
    for offsets in _OFFSETS.values():
        for offset in offsets:
            masked[offset : offset + 32] = bytes(32)
    return "0x" + keccak256(bytes(masked)).hex()


FIXTURE_POOL_TEMPLATE = CodeTemplate(
    contract="FixtureV3Pool",
    version="fixture",
    source_repository="fixture",
    source_commit="fixture",
    build="fixture",
    length=_LENGTH,
    masked_keccak=_masked_hash(),
    immutables=_OFFSETS,
)


def v3_deployment() -> V3Deployment:
    return V3Deployment(
        chain=FIXTURE_CHAIN,
        chain_id=FIXTURE_CHAIN_ID,
        venue="fixture-v3",
        factory=V3_FACTORY,
        position_manager=NPM,
        pool_template=FIXTURE_POOL_TEMPLATE,
        start_block=START_BLOCK,
    )


def v3_registry() -> LpDeploymentRegistry:
    return LpDeploymentRegistry(v3=(v3_deployment(),))


@dataclass
class _Nft:
    token_id: int
    owner: str
    lower: int
    upper: int
    liquidity: int
    token0: str
    token1: str
    fee: int
    burned: bool = False


@dataclass
class FakeV3Chain(_Base):
    pool: str = V3_POOL
    factory: str = V3_FACTORY
    npm: str = NPM
    token0: str = TOKEN
    token1: str = QUOTE
    fee: int = 3000
    tick: int = 0
    nfts: dict[int, _Nft] = field(default_factory=dict)
    # (owner, lower, upper) -> liquidity in the pool.
    pool_positions: dict[tuple[str, int, int], int] = field(default_factory=dict)
    block: int = CREATED_BLOCK
    liquidity_override: int | None = None

    def __post_init__(self) -> None:
        self.codes.setdefault(
            self.pool, v3_pool_code(self.factory, self.token0, self.token1, self.fee)
        )
        self.codes.setdefault(self.npm, "0x60806040")
        self._emit(
            self.factory,
            CREATED_BLOCK,
            (
                POOL_CREATED_TOPIC,
                address_topic(self.token0),
                address_topic(self.token1),
                _int_topic(self.fee),
            ),
            "0x" + word(60) + address_word(self.pool),
        )

    def _mint_log(self, owner: str, lower: int, upper: int, amount: int) -> None:
        self._emit(
            self.pool,
            self.block,
            (MINT_TOPIC, address_topic(owner), _int_topic(lower), _int_topic(upper)),
            "0x" + address_word(owner) + word(amount) + word(0) + word(0),
        )

    def nft(
        self, token_id: int, owner: str, liquidity: int, *, lower: int = -600, upper: int = 600
    ) -> "FakeV3Chain":
        """NPM `mint`: the pool records the NPM's key; the NPM emits IncreaseLiquidity."""
        self.block += 1
        self._mint_log(self.npm, lower, upper, liquidity)
        self._emit(
            self.npm,
            self.block,
            (INCREASE_LIQUIDITY_TOPIC, _int_topic(token_id)),
            "0x" + word(liquidity) + word(0) + word(0),
        )
        key = (self.npm, lower, upper)
        self.pool_positions[key] = self.pool_positions.get(key, 0) + liquidity
        self.nfts[token_id] = _Nft(
            token_id, owner, lower, upper, liquidity, self.token0, self.token1, self.fee
        )
        return self

    def other_pool_nft(self, token_id: int, owner: str) -> "FakeV3Chain":
        """An NFT of another pool, minted in the same block as the last NPM mint."""
        self._emit(
            self.npm,
            self.block,
            (INCREASE_LIQUIDITY_TOPIC, _int_topic(token_id)),
            "0x" + word(1) + word(0) + word(0),
        )
        self.nfts[token_id] = _Nft(token_id, owner, -60, 60, 1, self.token1, QUOTE, 500)
        return self

    def decrease_and_burn(self, token_id: int) -> "FakeV3Chain":
        nft = self.nfts[token_id]
        self.pool_positions[(self.npm, nft.lower, nft.upper)] -= nft.liquidity
        nft.liquidity = 0
        nft.burned = True
        return self

    def direct(
        self, owner: str, liquidity: int, *, lower: int = -600, upper: int = 600
    ) -> "FakeV3Chain":
        self.block += 1
        self._mint_log(owner, lower, upper, liquidity)
        key = (owner, lower, upper)
        self.pool_positions[key] = self.pool_positions.get(key, 0) + liquidity
        return self

    def active(self) -> int:
        if self.liquidity_override is not None:
            return self.liquidity_override
        return sum(
            value
            for (_, lower, upper), value in self.pool_positions.items()
            if lower <= self.tick < upper
        )

    def answer(self, address: str, data: str) -> str:
        sel = data[:10]
        if address == self.pool:
            simple = {
                selector("token0()"): "0x" + address_word(self.token0),
                selector("token1()"): "0x" + address_word(self.token1),
                selector("factory()"): "0x" + address_word(self.factory),
                selector("fee()"): hexword(self.fee),
                selector("liquidity()"): hexword(self.active()),
                selector("slot0()"): hexword(1 << 96) + word(self.tick) + word(0) * 5,
            }
            if sel in simple:
                return simple[sel]
            if sel == selector("positions(bytes32)"):
                for (owner, lower, upper), value in self.pool_positions.items():
                    if position_key(owner, lower, upper) == data[10:]:
                        return hexword(value) + word(0) * 4
                return hexword(0) + word(0) * 4
        if address == self.factory and sel == selector("getPool(address,address,uint24)"):
            return "0x" + address_word(self.pool)
        if address == self.npm:
            if sel == selector("factory()"):
                return "0x" + address_word(self.factory)
            token = self.nfts.get(int(data[10:], 16)) if len(data) == 74 else None
            if token is None or token.burned:
                raise CallReverted("Invalid token ID")
            if sel == selector("ownerOf(uint256)"):
                return "0x" + address_word(token.owner)
            if sel == selector("positions(uint256)"):
                return (
                    hexword(0)
                    + word(0)
                    + address_word(token.token0)
                    + address_word(token.token1)
                    + word(token.fee)
                    + word(token.lower)
                    + word(token.upper)
                    + word(token.liquidity)
                    + word(0) * 4
                )
        raise CallReverted(f"{address} {sel}")


__all__ = [
    "ACCOUNT_CODE",
    "BURN_DEAD",
    "FIXTURE_LOCKERS",
    "FIXTURE_LOCKER_CODE",
    "HEAD",
    "MINIMAL_PROXY_CODE",
    "VAULT_CODE",
    "FakeV2Chain",
    "FakeV3Chain",
    "v2_deployment",
    "v2_registry",
    "v3_deployment",
    "v3_registry",
]
