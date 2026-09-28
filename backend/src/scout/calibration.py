"""A reproducible random label sample for calibrating shadow signals. Design helper.

The Codex ORBIT results collected so far are not a random sample: the queue is
FIFO by due time, so what got reviewed is what came first. A calibration of a
fast signal against Codex needs watches chosen independently of that order and
independently of the fast signal itself.

`calibration_sample` ranks candidates by a keyed hash of a stable id — a
fast-assessment id, so declined candidates without a watch are included — and
takes the lowest ranks per chain. The same seed always gives the same sample;
the function is only ever shown ids and chains, so it cannot be influenced by
a JEV score, a classification, liquidity or any other market value.

Not wired into the ORBIT queue. Using the sample to decide which watches Codex
reviews would be a queue-policy change and is a separate decision.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID


def sample_rank(seed: str, candidate_id: UUID) -> str:
    return hashlib.sha256(f"{seed}:{candidate_id}".encode()).hexdigest()


def calibration_sample(
    candidates: Iterable[tuple[UUID, str]], *, seed: str, per_chain: int
) -> dict[str, tuple[UUID, ...]]:
    """Up to `per_chain` candidates per chain, reproducibly random, from (id, chain) only."""
    by_chain: dict[str, list[UUID]] = defaultdict(list)
    for candidate_id, chain in candidates:
        by_chain[chain].append(candidate_id)
    return {
        chain: tuple(sorted(ids, key=lambda item: sample_rank(seed, item))[: max(0, per_chain)])
        for chain, ids in sorted(by_chain.items())
    }
