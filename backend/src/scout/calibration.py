"""A reproducible random label sample for calibrating shadow signals. Design helper.

The Codex ORBIT results collected so far are not a random sample: the queue is
FIFO by due time, so what got reviewed is what came first. A calibration of a
fast signal against Codex needs watches chosen independently of that order and
independently of the fast signal itself.

`calibration_sample` ranks watches by a keyed hash of their id and takes the
lowest ranks per chain. The same seed always gives the same sample; the
function is only ever shown watch ids and chains, so it cannot be influenced
by a JEV score, a classification, liquidity or any other market value.

Not wired into the ORBIT queue. Using the sample to decide which watches Codex
reviews would be a queue-policy change and is a separate decision.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID


def sample_rank(seed: str, watch_id: UUID) -> str:
    return hashlib.sha256(f"{seed}:{watch_id}".encode()).hexdigest()


def calibration_sample(
    watches: Iterable[tuple[UUID, str]], *, seed: str, per_chain: int
) -> dict[str, tuple[UUID, ...]]:
    """Up to `per_chain` watches per chain, reproducibly random, from (id, chain) pairs only."""
    by_chain: dict[str, list[UUID]] = defaultdict(list)
    for watch_id, chain in watches:
        by_chain[chain].append(watch_id)
    return {
        chain: tuple(sorted(ids, key=lambda item: sample_rank(seed, item))[: max(0, per_chain)])
        for chain, ids in sorted(by_chain.items())
    }
