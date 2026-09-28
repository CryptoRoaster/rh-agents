"""The calibration sample: reproducible, stratified by chain, blind to every signal."""

import inspect
from uuid import UUID, uuid5

from src.scout.calibration import calibration_sample

NAMESPACE = UUID("00000000-0000-4000-8000-000000000000")
WATCHES = [(uuid5(NAMESPACE, f"r{index}"), "robinhood") for index in range(40)] + [
    (uuid5(NAMESPACE, f"b{index}"), "bsc") for index in range(10)
]


def test_the_same_seed_gives_the_same_sample_in_any_input_order():
    first = calibration_sample(WATCHES, seed="2026-09-28", per_chain=5)
    again = calibration_sample(list(reversed(WATCHES)), seed="2026-09-28", per_chain=5)
    assert first == again


def test_every_chain_is_sampled_on_its_own():
    sample = calibration_sample(WATCHES, seed="s", per_chain=5)
    assert {chain: len(ids) for chain, ids in sample.items()} == {"bsc": 5, "robinhood": 5}
    assert set(sample["bsc"]) <= {watch for watch, chain in WATCHES if chain == "bsc"}


def test_another_seed_draws_another_sample():
    assert calibration_sample(WATCHES, seed="a", per_chain=5) != calibration_sample(
        WATCHES, seed="b", per_chain=5
    )


def test_the_sample_cannot_see_any_signal():
    # Only ids, chains, a seed and a size go in: no JEV score, classification or market value.
    assert list(inspect.signature(calibration_sample).parameters) == [
        "watches",
        "seed",
        "per_chain",
    ]
