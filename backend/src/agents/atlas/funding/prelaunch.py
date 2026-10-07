"""CREATOR_FUNDING_GRAPH V2: the creator's direct native funding before the creation block.

Pure and deterministic. The edges are exactly V1's (``funding_edges``); only
the window differs: rows strictly before the creation block, no older than
``creation_timestamp - lookback``. Each window's coverage is proven on its own
from the provider's validated newest-first order -- the list ended, or the read
reached a row older than that window's cutoff -- so a short window can be
exact while a longer one is only a lower bound.
"""

from collections import defaultdict
from datetime import datetime

from src.agents.atlas.funding.graph import (
    FundingDataConflict,
    edges_digest,
    funding_edges,
    holder_basis,
    share,
)
from src.agents.atlas.funding.models import (
    CREATION_TIME_SOURCE,
    LONGEST_LOOKBACK,
    MAX_DURABLE_EDGES,
    PRELAUNCH_WINDOWS,
    ROLLING_INTERVAL,
    FundingCoverage,
    FundingEdge,
    FundingSourceResult,
    HolderOverlapBasis,
    PrelaunchFundingFacts,
    PrelaunchGap,
    PrelaunchWindow,
    PrelaunchWindowFacts,
)
from src.agents.atlas.models import HolderFacts, OriginFacts
from src.agents.atlas.primitives import AtlasSourceFailure
from src.agents.atlas.v4.models import PoolControlFacts
from src.markets.models import Availability


class PrelaunchContractViolation(ValueError):
    """The read contradicts the chain's own clock for the creation block."""


def largest_identical_value_cluster(edges: tuple[FundingEdge, ...]) -> tuple[int, int | None]:
    """Distinct recipients of the one exact value most of them received.

    A recipient paid that value twice is still one recipient. Ties go to the
    smallest value, so the answer never depends on input order.
    """
    by_value: dict[int, set[str]] = defaultdict(set)
    for edge in edges:
        by_value[edge.native_value_raw].add(edge.recipient)
    if not by_value:
        return 0, None
    value, recipients = min(by_value.items(), key=lambda item: (-len(item[1]), item[0]))
    return len(recipients), value


def max_unique_recipients_in_interval(
    edges: tuple[FundingEdge, ...], observed: dict[str, datetime]
) -> int:
    """Most distinct recipients within any half-open ``ROLLING_INTERVAL`` span."""
    timed = sorted((observed[edge.tx_hash], edge.recipient) for edge in edges)
    counts: dict[str, int] = defaultdict(int)
    best = start = 0
    for when, recipient in timed:
        counts[recipient] += 1
        while timed[start][0] <= when - ROLLING_INTERVAL:
            gone = timed[start][1]
            counts[gone] -= 1
            if not counts[gone]:
                del counts[gone]
            start += 1
        best = max(best, len(counts))
    return best


def window_facts(
    name: PrelaunchWindow,
    edges: tuple[FundingEdge, ...],
    observed: dict[str, datetime],
    creation_timestamp: datetime,
    history_ended: bool,
    oldest_observed_at: datetime | None,
) -> PrelaunchWindowFacts:
    cutoff = creation_timestamp - name.lookback
    inside = tuple(edge for edge in edges if observed[edge.tx_hash] >= cutoff)
    recipients = {edge.recipient for edge in inside}
    per_recipient: dict[str, int] = defaultdict(int)
    for edge in inside:
        per_recipient[edge.recipient] += 1
    cluster, value = largest_identical_value_cluster(inside)
    proven = history_ended or (oldest_observed_at is not None and oldest_observed_at < cutoff)
    times = sorted(observed[edge.tx_hash] for edge in inside)
    return PrelaunchWindowFacts(
        window=name,
        lookback_seconds=int(name.lookback.total_seconds()),
        cutoff_at=cutoff,
        coverage=FundingCoverage.COMPLETE if proven else FundingCoverage.LOWER_BOUND,
        funding_tx_count=len(inside),
        unique_funded_address_count=len(recipients),
        total_native_funding_raw=sum(edge.native_value_raw for edge in inside),
        first_funding_block=inside[0].block_number if inside else None,
        last_funding_block=inside[-1].block_number if inside else None,
        first_funding_at=times[0] if times else None,
        last_funding_at=times[-1] if times else None,
        edges_digest=edges_digest(inside),
        unique_funding_value_count=len({edge.native_value_raw for edge in inside}),
        repeated_funding_tx_count=len(inside) - len(recipients),
        max_funding_txs_per_recipient=max(per_recipient.values(), default=0),
        largest_identical_value_recipient_cluster_count=cluster,
        largest_identical_value_raw=value,
        largest_identical_value_recipient_fraction=(
            share(cluster, len(recipients)) if recipients else None
        ),
        max_unique_recipients_in_rolling_10m=max_unique_recipients_in_interval(inside, observed),
    )


def prelaunch_facts(
    read: FundingSourceResult | None,
    *,
    source: str,
    origin: OriginFacts,
    creation_timestamp: datetime | None,
    holders: HolderFacts,
    pool_control: PoolControlFacts | None,
    v4_required: bool,
    snapshot_block: int,
) -> PrelaunchFundingFacts:
    root = origin.creator_address if origin.status == Availability.AVAILABLE else None
    created = origin.creation_block
    common: dict[str, object] = {
        "source": source,
        "root_address": root,
        "origin_source": origin.source,
        "origin_verification": origin.verification.value,
        "creation_block": created,
        "creation_timestamp": creation_timestamp,
        "creation_time_source": None if creation_timestamp is None else CREATION_TIME_SOURCE,
    }

    def unavailable(
        gap: PrelaunchGap, failure: AtlasSourceFailure | None = None
    ) -> PrelaunchFundingFacts:
        return PrelaunchFundingFacts.model_validate(
            common | {"status": Availability.UNAVAILABLE, "gap": gap, "failure": failure}
        )

    if root is None or created is None:
        return unavailable(PrelaunchGap.ORIGIN_UNAVAILABLE)
    if created > snapshot_block:
        return unavailable(PrelaunchGap.WINDOW_UNAVAILABLE)
    if creation_timestamp is None:
        return unavailable(PrelaunchGap.CREATION_TIME_UNAVAILABLE)
    if (
        read is None
        or read.status != Availability.AVAILABLE
        or read.address != root
        or read.from_block != created
        or read.history_until != creation_timestamp - LONGEST_LOOKBACK
    ):
        # Not a history of this root back from this creation time.
        failure = AtlasSourceFailure.UNAVAILABLE if read is None else read.failure
        return unavailable(
            PrelaunchGap.SOURCE_UNAVAILABLE, failure or AtlasSourceFailure.INVALID_RESPONSE
        )
    if read.history_failure is not None:
        # The read answered V1; only its history was unusable.
        return unavailable(PrelaunchGap.SOURCE_UNAVAILABLE, read.history_failure)
    try:
        observed = clock_of(read, origin, creation_timestamp)
        edges = funding_edges(read.prelaunch_transactions, root, 0, created - 1)
    except (FundingDataConflict, PrelaunchContractViolation):
        return unavailable(PrelaunchGap.SOURCE_UNAVAILABLE, AtlasSourceFailure.INVALID_RESPONSE)
    windows = tuple(
        window_facts(
            name, edges, observed, creation_timestamp, read.history_ended, read.oldest_observed_at
        )
        for name in PRELAUNCH_WINDOWS
    )
    widest = tuple(edge for edge in edges if observed[edge.tx_hash] >= windows[-1].cutoff_at)
    values = common | {
        "status": Availability.AVAILABLE,
        "history_ended": read.history_ended,
        "oldest_observed_at": read.oldest_observed_at,
        "transactions_read": len(read.prelaunch_transactions),
        "windows": windows,
        "sample_edges": widest[:MAX_DURABLE_EDGES],
    }
    basis, observed_holders = holder_basis(holders, pool_control, v4_required)
    values["holder_basis"] = basis
    if basis != HolderOverlapBasis.UNKNOWN:
        recipients = {edge.recipient for edge in widest}
        funded = sum(1 for holder, _ in observed_holders if holder in recipients)
        values |= {
            "observed_holder_count": len(observed_holders),
            "creator_funded_observed_holder_count": funded,
            "creator_funded_observed_holder_fraction": (
                share(funded, len(observed_holders)) if observed_holders else None
            ),
        }
    return PrelaunchFundingFacts.model_validate(values)


def clock_of(
    read: FundingSourceResult, origin: OriginFacts, creation_timestamp: datetime
) -> dict[str, datetime]:
    """Every prelaunch row's time, checked against the chain's creation time.

    A row before the creation block cannot be later than it; the creation
    transaction, where the read happens to contain it, must carry exactly the
    creation block's time. Either contradiction voids the measurement.
    """
    observed: dict[str, datetime] = {}
    for item in read.prelaunch_transactions:
        if item.observed_at is None or item.observed_at > creation_timestamp:
            raise PrelaunchContractViolation(item.tx_hash)
        observed[item.tx_hash] = item.observed_at
    for item in (*read.transactions, *read.prelaunch_transactions):
        if item.tx_hash == origin.creation_tx_hash and item.observed_at != creation_timestamp:
            raise PrelaunchContractViolation(item.tx_hash)
    return observed
