"""From a source's transaction list and established ATLAS facts to FundingGraphFacts.

Pure and deterministic. The edges are filtered by the V1 definition only; the
holder overlap reads the holder basis the rest of ATLAS already established --
the reconciled economic holders for a V4 token, the normalized raw holders for
any other -- and never builds a second interpretation of either.
"""

import hashlib
from decimal import Decimal, localcontext

from src.agents.atlas.funding.models import (
    MAX_DURABLE_EDGES,
    FundingEdge,
    FundingGap,
    FundingGraphFacts,
    FundingSourceResult,
    FundingTransaction,
    HolderOverlapBasis,
    PrelaunchFundingFacts,
)
from src.agents.atlas.models import ContractFacts, HolderFacts, OriginFacts
from src.agents.atlas.primitives import AtlasSourceFailure
from src.agents.atlas.sources.normalize import TOP_N
from src.agents.atlas.v4.models import UNATTRIBUTED_POOL_BALANCE, PoolControlFacts
from src.core.numbers import quantize
from src.markets.models import Availability


class FundingDataConflict(ValueError):
    """The source named one transaction hash twice with different contents."""


def share(amount: int, denominator: int) -> Decimal:
    with localcontext() as context:
        context.prec = 78
        return min(Decimal(1), quantize(Decimal(amount) / Decimal(denominator)))


def funding_edges(
    transactions: tuple[FundingTransaction, ...], root: str, start: int, end: int
) -> tuple[FundingEdge, ...]:
    """Every V1 edge, once, in canonical order (block, then tx hash).

    Counted only: a successful transaction from exactly ``root`` to a concrete
    address other than ``root``, carrying native value, inside ``[start, end]``.
    A hash the source repeats identically counts once; a hash it repeats with
    different contents makes the whole read untrustworthy.
    """
    by_hash: dict[str, FundingTransaction] = {}
    for item in transactions:
        known = by_hash.get(item.tx_hash)
        if known is not None and known != item:
            raise FundingDataConflict(item.tx_hash)
        by_hash[item.tx_hash] = item
    edges = [
        FundingEdge(
            funder=item.sender,
            recipient=item.recipient,
            tx_hash=item.tx_hash,
            block_number=item.block_number,
            native_value_raw=item.native_value_raw,
        )
        for item in by_hash.values()
        if item.succeeded
        and item.sender == root
        and item.recipient is not None
        and item.recipient != root
        and item.native_value_raw > 0
        and start <= item.block_number <= end
    ]
    return tuple(sorted(edges, key=lambda edge: (edge.block_number, edge.tx_hash)))


def edges_digest(edges: tuple[FundingEdge, ...]) -> str:
    lines = "\n".join(
        f"{edge.block_number}:{edge.tx_hash}:{edge.funder}:{edge.recipient}:{edge.native_value_raw}"
        for edge in edges
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def holder_basis(
    holders: HolderFacts, pool_control: PoolControlFacts | None, v4_required: bool
) -> tuple[HolderOverlapBasis, tuple[tuple[str, int], ...]]:
    """The observed holders the overlap is measured against, largest first.

    A token whose concentration must be read through V4 pool control is
    measured only against its reconciled economic holders; when those are not
    established the basis is unknown -- raw rows, in which the PoolManager can
    stand for every pool, are never a substitute.
    """
    if v4_required:
        if pool_control is None or pool_control.status != Availability.AVAILABLE:
            return HolderOverlapBasis.UNKNOWN, ()
        return HolderOverlapBasis.ECONOMIC, tuple(
            (item.holder, item.balance_raw)
            for item in pool_control.economic_top_holders
            if item.holder != UNATTRIBUTED_POOL_BALANCE
        )
    if holders.status != Availability.AVAILABLE:
        return HolderOverlapBasis.UNKNOWN, ()
    return HolderOverlapBasis.RAW, tuple(
        (row.address, row.balance_raw) for row in holders.top_holders
    )


def funding_graph(
    read: FundingSourceResult | None,
    *,
    source: str,
    origin: OriginFacts,
    contract: ContractFacts,
    holders: HolderFacts,
    pool_control: PoolControlFacts | None,
    v4_required: bool,
    snapshot_block: int,
    prelaunch: PrelaunchFundingFacts | None = None,
) -> FundingGraphFacts:
    """V1 over ``[creation_block, snapshot_block]``; ``prelaunch`` rides along unchanged."""
    root = origin.creator_address if origin.status == Availability.AVAILABLE else None
    common: dict[str, object] = {
        "prelaunch": prelaunch,
        "source": source,
        "root_address": root,
        "origin_source": origin.source,
        "origin_verification": origin.verification.value,
        "factory_address": origin.factory_address,
        "creation_block": origin.creation_block,
        "snapshot_block": snapshot_block,
    }

    def unavailable(
        gap: FundingGap, failure: AtlasSourceFailure | None = None, requests: int = 0
    ) -> FundingGraphFacts:
        return FundingGraphFacts.model_validate(
            common
            | {
                "status": Availability.UNAVAILABLE,
                "gap": gap,
                "failure": failure,
                "requests_made": requests,
            }
        )

    if root is None:
        return unavailable(FundingGap.ORIGIN_UNAVAILABLE)
    created = origin.creation_block
    if created is None or created > snapshot_block:
        return unavailable(FundingGap.WINDOW_UNAVAILABLE)
    if read is None:
        return unavailable(FundingGap.SOURCE_NOT_CONFIGURED)
    if (
        read.status != Availability.AVAILABLE
        or read.address != root
        or read.from_block != created
        or read.to_block != snapshot_block
        or read.coverage is None
    ):
        # Unavailable, or an answer about some other address or window: either
        # way not a measurement of this root over this window.
        return unavailable(
            FundingGap.SOURCE_UNAVAILABLE,
            read.failure or AtlasSourceFailure.INVALID_RESPONSE,
            read.requests_made,
        )
    try:
        edges = funding_edges(read.transactions, root, created, snapshot_block)
    except FundingDataConflict:
        return unavailable(
            FundingGap.SOURCE_UNAVAILABLE, AtlasSourceFailure.INVALID_RESPONSE, read.requests_made
        )
    recipients = {edge.recipient for edge in edges}
    basis, observed = holder_basis(holders, pool_control, v4_required)
    values = common | {
        "status": Availability.AVAILABLE,
        "coverage": read.coverage,
        "requests_made": read.requests_made,
        "transactions_read": len(read.transactions),
        "direct_funding_tx_count": len(edges),
        "unique_direct_funded_address_count": len(recipients),
        "total_direct_native_funding_raw": sum(edge.native_value_raw for edge in edges),
        "first_funding_block": edges[0].block_number if edges else None,
        "last_funding_block": edges[-1].block_number if edges else None,
        "edges_digest": edges_digest(edges),
        "sample_edges": edges[:MAX_DURABLE_EDGES],
        "holder_basis": basis,
    }
    if basis != HolderOverlapBasis.UNKNOWN:
        funded = [(holder, balance) for holder, balance in observed if holder in recipients]
        supply = contract.total_supply_raw if contract.status == Availability.AVAILABLE else None
        values |= {
            "observed_holder_count": len(observed),
            "creator_funded_observed_holder_count": len(funded),
            "creator_funded_observed_holder_fraction": (
                share(len(funded), len(observed)) if observed else None
            ),
            "creator_funded_observed_supply_fraction": (
                share(sum(balance for _, balance in funded), supply) if supply else None
            ),
            "creator_funded_observed_top10_count": sum(
                1 for holder, _ in observed[:TOP_N] if holder in recipients
            ),
            "creator_funded_observed_holders": tuple(holder for holder, _ in funded),
        }
    return FundingGraphFacts.model_validate(values)
