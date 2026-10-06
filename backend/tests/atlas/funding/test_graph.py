"""CREATOR_FUNDING_GRAPH V1 through the real ATLAS builder, with a scripted funding source.

The REVENUE mechanism: a creator directly funds a large number of distinct
addresses in the launch window, many of which then appear among the observed
economic holders. V1 measures that -- counts, overlap, coverage -- and changes
no verdict. Synthetic addresses only; nothing keys on the historical token.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from src.agents.atlas.context import atlas_snapshot_digest, snapshot_document
from src.agents.atlas.funding.models import (
    FundingCoverage,
    FundingGap,
    FundingSourceResult,
    FundingTransaction,
    HolderOverlapBasis,
)
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow, OriginVerification
from src.agents.atlas.policy import evaluate_snapshot
from src.markets.models import Availability
from src.orchestration.workflow.models import OnchainIntelligence, OnchainPayload
from tests.atlas.conftest import CREATOR, TOKEN, market_identity
from tests.atlas.v4.chain import CREATED_BLOCK, HEAD_BLOCK, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.custody import OFFICIAL_SPLITTER, fee_splitter_code
from tests.atlas.v4.scenarios import (
    MEME,
    SUPPLY,
    UNIT,
    builder,
    official_launch,
    origin,
    revenue_like,
    v4_market,
)

SOURCE = "blockscout-pro:4663"


def recipient(index: int) -> str:
    return "0x" + format(0xF0000 + index, "040x")


def tx(
    index: int,
    *,
    to: str | None,
    value: int = 10**15,
    block: int | None = None,
    ok=True,
    sender: str = CREATOR,
) -> FundingTransaction:
    return FundingTransaction(
        tx_hash="0x" + format(index, "064x"),
        block_number=CREATED_BLOCK + 20 + index if block is None else block,
        sender=sender,
        recipient=to,
        native_value_raw=value,
        succeeded=ok,
    )


@dataclass
class StubFunding:
    transactions: tuple[FundingTransaction, ...] = ()
    coverage: FundingCoverage = FundingCoverage.COMPLETE
    status: Availability = Availability.AVAILABLE
    answer_address: str | None = None
    source: str = SOURCE
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)

    async def funding_transactions(self, chain, address, from_block, to_block):
        self.calls.append((chain, address, from_block, to_block))
        if self.status != Availability.AVAILABLE:
            from src.agents.atlas.models import AtlasSourceFailure

            return FundingSourceResult(
                status=self.status, failure=AtlasSourceFailure.NOT_CONFIGURED, source=self.source
            )
        return FundingSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=chain,
            address=self.answer_address or address,
            from_block=from_block,
            to_block=to_block,
            coverage=self.coverage,
            transactions=self.transactions,
            requests_made=3,
        )


async def snapshot_with(now, chain, funding, market=None, **kw):
    from dataclasses import replace
    from uuid import uuid4

    built = builder(now, chain, **kw)
    built = replace(built, funding=funding)
    identity = market or (v4_market(chain.pools[0]) if chain is not None else market_identity())
    return await built.build(uuid4(), uuid4(), identity)


def funded_holder_rows(funded: int, unfunded: int, each: int = 2_000_000 * UNIT):
    rows = [HolderSourceRow(address=recipient(i), balance_raw=each - i) for i in range(funded)]
    rows += [
        HolderSourceRow(address="0x" + format(0xA0000 + i, "040x"), balance_raw=each // 2 - i)
        for i in range(unfunded)
    ]
    return tuple(rows)


# ------------------------------------------------------------- REVENUE-like


def revenue_funding(count: int = 184) -> StubFunding:
    """184 distinct addresses funded by the creator, a few of them twice."""
    transactions = [tx(i, to=recipient(i)) for i in range(count)]
    transactions += [tx(1_000 + i, to=recipient(i), value=5 * 10**14) for i in range(6)]
    return StubFunding(tuple(transactions))


async def test_the_revenue_mechanism_is_measured(now) -> None:
    chain, _, _ = revenue_like("official")
    rows = funded_holder_rows(funded=12, unfunded=8)
    snapshot = await snapshot_with(now, chain, revenue_funding(), rows=rows)
    graph = snapshot.funding_graph

    assert graph.status is Availability.AVAILABLE
    assert graph.coverage is FundingCoverage.COMPLETE and graph.exact
    assert graph.scope == "DIRECT_NATIVE_FROM_ORIGIN_CREATOR"
    assert graph.root_address == CREATOR
    assert graph.origin_verification == OriginVerification.RECEIPT_CONFIRMED.value
    assert graph.direct_funding_tx_count == 190
    assert graph.unique_direct_funded_address_count == 184
    assert graph.total_direct_native_funding_raw == 184 * 10**15 + 6 * 5 * 10**14
    # Overlap with the reconciled economic holders, not the raw rows.
    assert graph.holder_basis is HolderOverlapBasis.ECONOMIC
    economic = {item.holder for item in snapshot.pool_control.economic_top_holders}
    funded = economic & {recipient(i) for i in range(184)}
    assert graph.creator_funded_observed_holder_count == len(funded) > 0
    assert graph.observed_holder_count == len(economic)
    assert graph.creator_funded_observed_holder_fraction == Decimal(len(funded)) / Decimal(
        len(economic)
    )
    economic_rows = snapshot.pool_control.economic_top_holders
    balances = {item.holder: item.balance_raw for item in economic_rows}
    assert graph.creator_funded_observed_supply_fraction == pytest.approx(
        Decimal(sum(balances[h] for h in funded)) / Decimal(SUPPLY), abs=Decimal("1e-17")
    )
    assert len(graph.sample_edges) == 16


async def test_the_verdict_is_identical_with_and_without_the_graph(now) -> None:
    chain, _, _ = revenue_like("official")
    rows = funded_holder_rows(funded=12, unfunded=8)
    with_graph = await snapshot_with(now, chain, revenue_funding(), rows=rows)
    chain.requests = 0
    without = await snapshot_with(now, chain, None, rows=rows)
    assert without.funding_graph is None
    a, b = evaluate_snapshot(with_graph, now), evaluate_snapshot(without, now)
    assert (a.verdict, a.blockers, a.data_gaps, a.domain_status) == (
        b.verdict,
        b.blockers,
        b.data_gaps,
        b.domain_status,
    )
    pa = onchain_payload(with_graph, a)
    pb = onchain_payload(without, b)
    assert pa.holder_integrity == pb.holder_integrity
    assert pa.contract_integrity == pb.contract_integrity
    # Only the digest and the durable intelligence reflect the measurement.
    assert atlas_snapshot_digest(with_graph) != atlas_snapshot_digest(without)
    assert pa.intelligence.funding_graph is not None and pb.intelligence.funding_graph is None


# --------------------------------------------------------------- clean launch


async def test_a_clean_official_launch_measures_zero_funded_holders(now) -> None:
    chain, _, rows = official_launch()
    funding = StubFunding((tx(1, to="0x" + "77" * 20),))  # one unrelated payment
    snapshot = await snapshot_with(now, chain, funding, rows=rows)
    graph = snapshot.funding_graph
    assert graph.status is Availability.AVAILABLE and graph.exact
    assert graph.unique_direct_funded_address_count == 1
    assert graph.creator_funded_observed_holder_count == 0
    assert graph.creator_funded_observed_supply_fraction == 0
    assert graph.holder_basis is HolderOverlapBasis.ECONOMIC


# ---------------------------------------------------------------- coverage


async def test_a_cut_read_is_a_lower_bound_never_an_exact_zero(now) -> None:
    chain, _, _ = revenue_like("official")
    funding = StubFunding((), coverage=FundingCoverage.LOWER_BOUND)
    graph = (await snapshot_with(now, chain, funding)).funding_graph
    assert graph.status is Availability.AVAILABLE
    assert graph.coverage is FundingCoverage.LOWER_BOUND and not graph.exact
    assert graph.unique_direct_funded_address_count == 0  # what was seen, no more
    payload = onchain_payload(
        snap := await snapshot_with(now, chain, funding), evaluate_snapshot(snap, now)
    )
    assert payload.intelligence.funding_graph.counts_are_lower_bounds


async def test_a_cut_read_keeps_what_it_saw(now) -> None:
    chain, _, _ = revenue_like("official")
    funding = revenue_funding(40)
    funding.coverage = FundingCoverage.LOWER_BOUND
    graph = (await snapshot_with(now, chain, funding)).funding_graph
    assert graph.unique_direct_funded_address_count == 40
    assert graph.coverage is FundingCoverage.LOWER_BOUND


# ---------------------------------------------------------- edge definition


async def test_only_direct_successful_nonzero_native_transfers_count(now) -> None:
    chain, _, _ = revenue_like("official")
    other = "0x" + "77" * 20
    transactions = (
        tx(1, to=recipient(1)),  # counts
        tx(2, to=recipient(2), ok=False),  # failed
        tx(3, to=recipient(3), value=0),  # zero value
        tx(4, to=CREATOR),  # self transfer
        tx(5, to=None),  # contract creation
        tx(6, to=recipient(6), block=HEAD_BLOCK + 1),  # after the snapshot
        tx(7, to=recipient(7), block=CREATED_BLOCK - 1),  # before creation
        tx(8, to=recipient(8), sender=other),  # not from the creator
        tx(1, to=recipient(1)),  # the same transaction again
    )
    graph = (await snapshot_with(now, chain, StubFunding(transactions))).funding_graph
    assert graph.direct_funding_tx_count == 1
    assert graph.unique_direct_funded_address_count == 1
    assert [edge.recipient for edge in graph.sample_edges] == [recipient(1)]


async def test_a_hash_reported_twice_with_different_contents_is_refused(now) -> None:
    chain, _, _ = revenue_like("official")
    transactions = (tx(1, to=recipient(1)), tx(1, to=recipient(2)))
    graph = (await snapshot_with(now, chain, StubFunding(transactions))).funding_graph
    assert graph.status is Availability.UNAVAILABLE
    assert graph.gap is FundingGap.SOURCE_UNAVAILABLE
    assert graph.direct_funding_tx_count is None


async def test_a_recipient_funded_twice_is_one_address_and_two_transactions(now) -> None:
    chain, _, _ = revenue_like("official")
    transactions = (tx(1, to=recipient(1)), tx(2, to=recipient(1)))
    graph = (await snapshot_with(now, chain, StubFunding(transactions))).funding_graph
    assert graph.direct_funding_tx_count == 2
    assert graph.unique_direct_funded_address_count == 1


async def test_the_window_is_creation_to_snapshot(now) -> None:
    chain, _, _ = revenue_like("official")
    funding = StubFunding()
    await snapshot_with(now, chain, funding)
    ((_, root, start, end),) = funding.calls
    assert root == CREATOR and start == CREATED_BLOCK and end == HEAD_BLOCK


# -------------------------------------------------------------- failures


async def test_no_origin_means_no_graph(now) -> None:
    from src.agents.atlas.models import OriginFacts

    chain, _, _ = revenue_like("official")
    funding = StubFunding()
    unavailable = OriginFacts(status=Availability.UNAVAILABLE, source="test-origin")
    graph = (await snapshot_with(now, chain, funding, origin_facts=unavailable)).funding_graph
    assert graph.gap is FundingGap.ORIGIN_UNAVAILABLE
    assert not funding.calls


async def test_an_unverified_origin_is_still_the_root_and_says_so(now) -> None:
    chain, _, _ = revenue_like("official")
    unverified = origin(verification=OriginVerification.UNVERIFIED)
    graph = (await snapshot_with(now, chain, StubFunding(), origin_facts=unverified)).funding_graph
    assert graph.status is Availability.AVAILABLE
    assert graph.origin_verification == "UNVERIFIED"


async def test_an_unavailable_source_is_no_measurement(now) -> None:
    chain, _, _ = revenue_like("official")
    funding = StubFunding(status=Availability.UNAVAILABLE)
    graph = (await snapshot_with(now, chain, funding)).funding_graph
    assert graph.status is Availability.UNAVAILABLE
    assert graph.gap is FundingGap.SOURCE_UNAVAILABLE
    assert graph.unique_direct_funded_address_count is None


async def test_an_answer_about_another_address_is_refused(now) -> None:
    chain, _, _ = revenue_like("official")
    funding = StubFunding((tx(1, to=recipient(1)),), answer_address="0x" + "99" * 20)
    graph = (await snapshot_with(now, chain, funding)).funding_graph
    assert graph.gap is FundingGap.SOURCE_UNAVAILABLE


async def test_a_source_that_raises_is_unavailable_not_a_crash(now) -> None:
    class Broken(StubFunding):
        async def funding_transactions(self, *args):
            raise RuntimeError("boom")

    chain, _, _ = revenue_like("official")
    graph = (await snapshot_with(now, chain, Broken())).funding_graph
    assert graph.gap is FundingGap.SOURCE_UNAVAILABLE


# ------------------------------------------------------------ holder basis


async def test_unknown_v4_economics_states_no_overlap_and_never_uses_raw_holders(now) -> None:
    chain, _, _ = revenue_like()  # launch NFT in an unverified locker: economics unknown
    rows = funded_holder_rows(funded=12, unfunded=8)
    graph = (await snapshot_with(now, chain, revenue_funding(), rows=rows)).funding_graph
    assert graph.holder_basis is HolderOverlapBasis.UNKNOWN
    assert graph.creator_funded_observed_holder_count is None
    assert graph.observed_holder_count is None
    # The funding counts themselves are still measured.
    assert graph.unique_direct_funded_address_count == 184


async def test_a_non_v4_token_overlaps_with_its_raw_holders(now) -> None:
    rows = funded_holder_rows(funded=3, unfunded=7)
    graph = (
        await snapshot_with(now, None, revenue_funding(), rows=rows, market=market_identity())
    ).funding_graph
    assert graph.holder_basis is HolderOverlapBasis.RAW
    assert graph.observed_holder_count == 10
    assert graph.creator_funded_observed_holder_count == 3
    assert graph.creator_funded_observed_top10_count == 3


async def test_a_meme_meme_v4_token_overlaps_like_any_other(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(
        Pool(currency0=TOKEN, currency1=MEME, fee=3000, tick_spacing=60, tick=-1_000)
    )
    chain.mint(pool, OFFICIAL_SPLITTER, 9, 0, 6_000, 10**24)
    supply = chain.pool_balance(chain.head) + 30_000_000 * UNIT
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        HolderSourceRow(address=recipient(0), balance_raw=20_000_000 * UNIT),
        HolderSourceRow(address="0x" + "a5" * 20, balance_raw=10_000_000 * UNIT),
    )
    graph = (
        await snapshot_with(
            now,
            chain,
            revenue_funding(3),
            rows=rows,
            completeness=HolderCompleteness.COMPLETE,
            supply=supply,
            holder_count=3,
            market=v4_market(pool),
        )
    ).funding_graph
    assert graph.holder_basis is HolderOverlapBasis.ECONOMIC
    assert graph.creator_funded_observed_holder_count == 1
    assert graph.creator_funded_observed_holders == (recipient(0),)


# ------------------------------------------------------- evidence and digest


async def test_evidence_carries_a_bounded_record_and_round_trips(now) -> None:
    chain, _, _ = revenue_like("official")
    snapshot = await snapshot_with(now, chain, revenue_funding())
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    summary = payload.intelligence.funding_graph
    assert summary.unique_direct_funded_address_count == 184
    assert len(summary.sample_edges) == 16
    assert summary.edges_digest == snapshot.funding_graph.edges_digest
    restored = OnchainPayload.model_validate_json(payload.model_dump_json())
    assert restored.model_dump_json() == payload.model_dump_json()
    document = snapshot_document(snapshot)["funding_graph"]
    assert document["unique_direct_funded_address_count"] == 184


async def test_the_digest_moves_with_the_funding_facts(now) -> None:
    chain, _, _ = revenue_like("official")
    a = await snapshot_with(now, chain, revenue_funding(184))
    chain.requests = 0
    b = await snapshot_with(now, chain, revenue_funding(183))
    assert atlas_snapshot_digest(a) != atlas_snapshot_digest(b)


def test_evidence_written_before_the_graph_replays_byte_for_byte() -> None:
    legacy = {
        "verdict": "CLEAR",
        "policy_version": "atlas-policy-v2",
        "blockers": [],
        "data_gaps": [],
        "domain_status": {"CONTRACT": "AVAILABLE"},
        "chain_id": 4663,
        "block_number": 1,
        "snapshot_digest": "a" * 64,
    }
    intelligence = OnchainIntelligence.model_validate(legacy)
    assert intelligence.funding_graph is None
    assert "funding_graph" not in intelligence.model_dump(mode="json")
