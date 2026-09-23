"""The schema reaches the child as a private descriptor, byte for byte.

Two separate claims, and the test would rather fail than conflate them.

The reference is `/dev/fd/<n>`, not a path in the scratch directory. That is
what lets the outer profile keep the scratch directory out of the boundary
entirely: a file the sandboxed process is told to read by name would have to be
inside a readable root, and then everything beside it would be too.

The bytes the child reads hash to what the parent wrote. A reference by itself
would only say something was handed over; the digest says the snapshot was not
short-written, truncated, replaced or rewritten between the two.
"""

import asyncio
import hashlib
import json

import pytest

from src.agents.orbit.models import OrbitAssessment
from src.evaluation.codex.command import FD_REFERENCE_PREFIX
from src.evaluation.codex.models import EvaluationCompleted
from src.evaluation.codex.schema import strict_schema
from tests.evaluation.conftest import Probe


def expected_digest() -> str:
    """The same bytes the client serialises, computed independently here."""
    payload = json.dumps(strict_schema(OrbitAssessment), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_the_child_reads_the_schema_through_a_descriptor(probe: Probe) -> None:
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)

    observed = json.loads(
        await asyncio.to_thread(
            (probe.workspace / "observed-schema.json").read_text, encoding="utf-8"
        )
    )
    assert observed["reference"].startswith(FD_REFERENCE_PREFIX)
    assert observed["digest"] == expected_digest()


@pytest.mark.asyncio
async def test_the_schema_snapshot_is_unlinked_and_the_scratch_is_left_empty(
    probe: Probe,
) -> None:
    """Nothing survives the attempt in the directory the snapshots came from.

    The snapshot is unlinked while it is still open, so it has no name to reach
    even during the run, and the composed profile is removed in the same pass.
    A leftover here would be a file the next attempt could be pointed at.
    """
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert await asyncio.to_thread(lambda: sorted(i.name for i in probe.scratch.iterdir())) == []
