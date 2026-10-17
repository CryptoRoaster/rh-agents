"""Per-chain V2/V3 deployments a census may read through. Empty by default.

An entry is configuration, not trust -- but it is also the only thing that can
turn a 20-byte market from UNSUPPORTED into anything else. So nothing is
configured here until it has been verified on-chain at a pinned block:

* V2: the factory's `getPair` returns pairs whose runtime code hashes to
  `pair_code_hash`, with the pair's `factory()` pointing back; the pair code is
  the standard constant-product pair (no fee-on-transfer, no rebasing LP);
* V3: the factory's `getPool` returns pools whose runtime code matches
  `pool_template` with the factory/token/fee immutables bound back, and the
  NonfungiblePositionManager's immutable `factory()` is that factory.

That verification needs RPC reads and a separate approval (issue #102,
docs/lp-control-v2-v3.md). Until then both tuples stay empty, every V2/V3
decision is UNSUPPORTED, and `tests/lpcontrol/test_v2v3_registry.py` fails on
any entry added without changing that test deliberately.
"""

import re
from dataclasses import dataclass

from src.agents.atlas.v4.custody.template import CodeTemplate

ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
HASH = re.compile(r"^0x[0-9a-f]{64}$")


@dataclass(frozen=True)
class V2Deployment:
    """One verified V2-style factory on one chain."""

    chain: str
    chain_id: int
    venue: str
    factory: str
    # keccak256 of every pair's exact runtime code (a V2 pair has no immutables).
    pair_code_hash: str
    # The factory's deployment block: where a pair-creation scan may start.
    start_block: int

    def __post_init__(self) -> None:
        if not (ADDRESS.match(self.factory) and HASH.match(self.pair_code_hash)):
            raise ValueError("A V2 deployment names a lowercase factory and a code hash")
        if self.chain_id <= 0 or self.start_block < 0:
            raise ValueError("A V2 deployment names a chain id and a start block")


@dataclass(frozen=True)
class V3Deployment:
    """One verified V3-style factory and its position manager on one chain."""

    chain: str
    chain_id: int
    venue: str
    factory: str
    position_manager: str
    # The pool's runtime code with its immutables masked.
    pool_template: CodeTemplate
    start_block: int

    def __post_init__(self) -> None:
        if not (ADDRESS.match(self.factory) and ADDRESS.match(self.position_manager)):
            raise ValueError("A V3 deployment names a lowercase factory and position manager")
        if self.chain_id <= 0 or self.start_block < 0:
            raise ValueError("A V3 deployment names a chain id and a start block")


@dataclass(frozen=True)
class LpDeploymentRegistry:
    v2: tuple[V2Deployment, ...] = ()
    v3: tuple[V3Deployment, ...] = ()

    def __post_init__(self) -> None:
        v2_keys = [(item.chain_id, item.factory) for item in self.v2]
        v3_keys = [(item.chain_id, item.factory, item.position_manager) for item in self.v3]
        if len(v2_keys) != len(set(v2_keys)) or len(v3_keys) != len(set(v3_keys)):
            raise ValueError("A deployment is registered once")
        pinned = [(item.chain, item.chain_id) for item in self.v2] + [
            (item.chain, item.chain_id) for item in self.v3
        ]
        for chain in {name for name, _ in pinned}:
            if len({chain_id for name, chain_id in pinned if name == chain}) != 1:
                raise ValueError("A chain name is pinned to one chain id")

    def v2_on(self, chain: str) -> tuple[V2Deployment, ...]:
        return tuple(item for item in self.v2 if item.chain == chain)

    def v3_on(self, chain: str) -> tuple[V3Deployment, ...]:
        return tuple(item for item in self.v3 if item.chain == chain)

    def v2_factory(self, chain: str, chain_id: int, factory: str) -> V2Deployment | None:
        return next(
            (
                item
                for item in self.v2
                if (item.chain, item.chain_id, item.factory) == (chain, chain_id, factory)
            ),
            None,
        )

    def v3_factory(
        self, chain: str, chain_id: int, factory: str, position_manager: str
    ) -> V3Deployment | None:
        return next(
            (
                item
                for item in self.v3
                if (item.chain, item.chain_id, item.factory, item.position_manager)
                == (chain, chain_id, factory, position_manager)
            ),
            None,
        )


# Deliberately empty: no V2/V3 factory, pair template or position manager has
# been verified on-chain. See the module docstring before adding anything.
LP_DEPLOYMENTS = LpDeploymentRegistry()
