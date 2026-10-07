"""CREATOR_FUNDING_GRAPH V2: direct native funding by the origin creator before creation.

The historical REVENUE mechanism, now proven on chain: the creator paid 182
distinct wallets directly, in 232 transfers, in the minutes before the token
existed -- 101 of them exactly the same small amount -- and paid nobody after
creation. V1 (the launch window) therefore sees nothing; V2 sees the structure.
The fixture reproduces that shape with synthetic addresses and synthetic times;
nothing in the product keys on it.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from src.agents.atlas.context import atlas_snapshot_digest, snapshot_document
from src.agents.atlas.funding.models import (
    CREATION_TIME_SOURCE,
    LONGEST_LOOKBACK,
    PRELAUNCH_SCOPE,
    FundingCoverage,
    FundingSourceResult,
    FundingTransaction,
    HolderOverlapBasis,
    PrelaunchGap,
    PrelaunchWindow,
)
from src.agents.atlas.funding.prelaunch import prelaunch_facts
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.models import AtlasSourceFailure, HolderFacts, HolderShare, OriginFacts
from src.agents.atlas.policy import evaluate_snapshot
from src.core.numbers import quantize
from src.markets.models import Availability
from src.orchestration.workflow.models import FundingGraphSummary, OnchainPayload
from tests.atlas.conftest import CREATOR
from tests.atlas.v4.chain import CREATED_BLOCK, HEAD_BLOCK
from tests.atlas.v4.scenarios import builder, origin, revenue_like, v4_market

SOURCE = "blockscout-pro:4663"
CREATION_AT = datetime(2026, 10, 1, 7, 13, 44, tzinfo=UTC)
CREATION_TX = "0x" + "11" * 32  # the scenarios' origin creation hash
CENT = 10**16  # 0.01 native


def wallet(index: int) -> str:
    return "0x" + format(0xB0000 + index, "040x")


def row(
    index: int,
    *,
    to: str | None,
    value: int,
    at: datetime,
    block: int | None = None,
    ok: bool = True,
    tx_hash: str | None = None,
) -> FundingTransaction:
    return FundingTransaction(
        tx_hash=tx_hash or "0x" + format(0xC000 + index, "064x"),
        block_number=CREATED_BLOCK - 5_000 + index if block is None else block,
        sender=CREATOR,
        recipient=to,
        native_value_raw=value,
        succeeded=ok,
        observed_at=at,
    )


def revenue_prelaunch_rows() -> tuple[FundingTransaction, ...]:
    """232 edges to 182 wallets in the 20 minutes before creation, plus 22 non-edges.

    One edge every 5 seconds from creation - 1200 s: first 101 wallets paid
    exactly 0.01 each, then 81 wallets one distinct amount each, then 50 of
    those 81 paid the same amount again. 20 zero-value calls and 2 contract
    creations follow and count for nothing.
    """
    start = CREATION_AT - timedelta(seconds=1_200)
    rows: list[FundingTransaction] = []
    payees = [(wallet(i), CENT) for i in range(101)]
    payees += [(wallet(101 + j), (2 + j) * CENT) for j in range(81)]
    payees += [(wallet(101 + j), (2 + j) * CENT) for j in range(50)]
    for k, (to, value) in enumerate(payees):
        rows.append(row(k, to=to, value=value, at=start + timedelta(seconds=5 * k)))
    tail = start + timedelta(seconds=5 * len(payees))
    for n in range(20):
        rows.append(row(300 + n, to=wallet(900 + n), value=0, at=tail + timedelta(seconds=n)))
    for n in range(2):
        rows.append(row(400 + n, to=None, value=0, at=tail + timedelta(seconds=30 + n)))
    return tuple(rows)


@dataclass
class HistoryFunding:
    """A scripted funding source that answers with a V2 history."""

    prelaunch: tuple[FundingTransaction, ...] = ()
    window: tuple[FundingTransaction, ...] = ()
    ended: bool = True
    oldest: datetime | None = None
    history_failure: AtlasSourceFailure | None = None
    source: str = SOURCE
    calls: list[tuple[object, ...]] = field(default_factory=list)

    async def funding_transactions(self, chain, address, from_block, to_block, history_until=None):
        self.calls.append((chain, address, from_block, to_block, history_until))
        times = [item.observed_at for item in (*self.window, *self.prelaunch)]
        return FundingSourceResult(
            status=Availability.AVAILABLE,
            source=self.source,
            chain=chain,
            address=address,
            from_block=from_block,
            to_block=to_block,
            coverage=FundingCoverage.COMPLETE,
            transactions=self.window,
            requests_made=7,
            history_until=history_until,
            history_failure=self.history_failure,
            prelaunch_transactions=(
                self.prelaunch if history_until is not None and not self.history_failure else ()
            ),
            history_ended=self.ended and not self.history_failure,
            oldest_observed_at=(
                None if self.history_failure else self.oldest or (min(times) if times else None)
            ),
        )


async def snapshot_with(now, funding, *, creation_at=CREATION_AT, chain=None, **kw):
    chain = chain or revenue_like("official")[0]
    built = builder(now, chain, **kw)
    if creation_at is not None:
        built.contracts.block_times[CREATED_BLOCK] = creation_at
    built = replace(built, funding=funding)
    return await built.build(uuid4(), uuid4(), v4_market(chain.pools[0]))


def read(
    prelaunch: tuple[FundingTransaction, ...],
    *,
    ended: bool = True,
    oldest: datetime | None = None,
    window: tuple[FundingTransaction, ...] = (),
    until: datetime | None = CREATION_AT - LONGEST_LOOKBACK,
) -> FundingSourceResult:
    return FundingSourceResult(
        status=Availability.AVAILABLE,
        source=SOURCE,
        chain="robinhood",
        address=CREATOR,
        from_block=CREATED_BLOCK,
        to_block=HEAD_BLOCK,
        coverage=FundingCoverage.COMPLETE,
        transactions=window,
        history_until=until,
        prelaunch_transactions=prelaunch,
        history_ended=ended,
        oldest_observed_at=oldest,
    )


def measure(result, *, origin_facts=None, creation_at=CREATION_AT, holders=None):
    return prelaunch_facts(
        result,
        source=SOURCE,
        origin=origin_facts or origin(),
        creation_timestamp=creation_at,
        holders=holders
        or HolderFacts(
            status=Availability.UNAVAILABLE,
            failure=AtlasSourceFailure.UNAVAILABLE,
            source="test-holders",
        ),
        pool_control=None,
        v4_required=False,
        snapshot_block=HEAD_BLOCK,
    )


# ---------------------------------------------------------------- REVENUE


async def test_the_revenue_prelaunch_structure_is_measured_and_v1_sees_nothing(now) -> None:
    funding = HistoryFunding(prelaunch=revenue_prelaunch_rows())
    snapshot = await snapshot_with(now, funding)
    graph = snapshot.funding_graph

    # One read, asked for history back to creation - 24 h.
    ((_, root, start, end, until),) = funding.calls
    assert (root, start, end) == (CREATOR, CREATED_BLOCK, HEAD_BLOCK)
    assert until == CREATION_AT - timedelta(hours=24)

    # V1 is unchanged and sees no post-creation funding at all.
    assert graph.scope == "DIRECT_NATIVE_FROM_ORIGIN_CREATOR"
    assert graph.coverage is FundingCoverage.COMPLETE
    assert graph.direct_funding_tx_count == 0
    assert graph.unique_direct_funded_address_count == 0
    assert graph.sample_edges == ()

    pre = graph.prelaunch
    assert pre.status is Availability.AVAILABLE
    assert pre.scope == PRELAUNCH_SCOPE and pre.version == 2
    assert pre.root_address == CREATOR
    assert pre.creation_timestamp == CREATION_AT
    assert pre.creation_time_source == CREATION_TIME_SOURCE
    assert pre.history_ended is True
    assert pre.transactions_read == 254
    assert [item.window for item in pre.windows] == list(PrelaunchWindow)
    for item in pre.windows:  # all funding lies within 20 minutes: every window is the same
        assert item.coverage is FundingCoverage.COMPLETE and item.exact
        assert item.funding_tx_count == 232
        assert item.unique_funded_address_count == 182
        assert item.total_native_funding_raw == 101 * CENT + 2 * sum(
            (2 + j) * CENT for j in range(50)
        ) + sum((2 + j) * CENT for j in range(50, 81))
        assert item.largest_identical_value_recipient_cluster_count == 101
        assert item.largest_identical_value_raw == 10**16
        assert item.largest_identical_value_recipient_fraction == quantize(
            Decimal(101) / Decimal(182)
        )
        assert item.unique_funding_value_count == 82
        assert item.repeated_funding_tx_count == 50
        assert item.max_funding_txs_per_recipient == 2
        # One edge per 5 s: a half-open 10-minute span holds 600 / 5 = 120 of
        # them, all to distinct wallets while it covers only first payments.
        assert item.max_unique_recipients_in_rolling_10m == 120
        assert item.first_funding_at == CREATION_AT - timedelta(seconds=1_200)
        assert item.last_funding_at == CREATION_AT - timedelta(seconds=1_200 - 5 * 231)
    assert len(pre.sample_edges) == 16


async def test_the_verdict_is_identical_with_and_without_the_prelaunch_measurement(now) -> None:
    chain = revenue_like("official")[0]
    with_v2 = await snapshot_with(
        now, HistoryFunding(prelaunch=revenue_prelaunch_rows()), chain=chain
    )
    chain.requests = 0
    without = await snapshot_with(now, None, chain=chain)
    a, b = evaluate_snapshot(with_v2, now), evaluate_snapshot(without, now)
    assert (a.verdict, a.blockers, a.data_gaps, a.domain_status) == (
        b.verdict,
        b.blockers,
        b.data_gaps,
        b.domain_status,
    )
    pa, pb = onchain_payload(with_v2, a), onchain_payload(without, b)
    assert pa.holder_integrity == pb.holder_integrity
    assert pa.dev_wallet_integrity == pb.dev_wallet_integrity
    assert pa.contract_integrity == pb.contract_integrity


async def test_prelaunch_evidence_round_trips_and_moves_the_digest(now) -> None:
    snapshot = await snapshot_with(now, HistoryFunding(prelaunch=revenue_prelaunch_rows()))
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    summary = payload.intelligence.funding_graph.prelaunch
    assert summary.scope == PRELAUNCH_SCOPE and summary.version == 2
    assert [w.window for w in summary.windows] == ["PT1H", "PT6H", "PT24H"]
    assert summary.windows[2].largest_identical_value_recipient_cluster_count == 101
    assert summary.windows[2].largest_identical_value_raw == str(10**16)
    assert (
        summary.windows[2].edges_digest == snapshot.funding_graph.prelaunch.windows[2].edges_digest
    )
    restored = OnchainPayload.model_validate_json(payload.model_dump_json())
    assert restored.model_dump_json() == payload.model_dump_json()
    document = snapshot_document(snapshot)["funding_graph"]
    assert document["prelaunch"]["windows"][2]["unique_funded_address_count"] == 182
    v1_only = snapshot.model_copy(
        update={"funding_graph": snapshot.funding_graph.model_copy(update={"prelaunch": None})}
    )
    assert "prelaunch" not in snapshot_document(v1_only)["funding_graph"]
    assert atlas_snapshot_digest(v1_only) != atlas_snapshot_digest(snapshot)


def test_v1_evidence_without_prelaunch_replays_byte_for_byte() -> None:
    v1 = {
        "measurement": "CREATOR_FUNDING_GRAPH",
        "scope": "DIRECT_NATIVE_FROM_ORIGIN_CREATOR",
        "status": "AVAILABLE",
        "gap": None,
        "failure": None,
        "source": SOURCE,
        "root_address": CREATOR,
        "origin_source": "test-origin",
        "origin_verification": "UNVERIFIED",
        "factory_address": None,
        "creation_block": CREATED_BLOCK,
        "snapshot_block": HEAD_BLOCK,
        "coverage": "COMPLETE",
        "requests_made": 2,
        "transactions_read": 7,
        "direct_funding_tx_count": 0,
        "unique_direct_funded_address_count": 0,
        "total_direct_native_funding_raw": "0",
        "first_funding_block": None,
        "last_funding_block": None,
        "edges_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sample_edges": [],
        "holder_basis": "UNKNOWN",
        "observed_holder_count": None,
        "creator_funded_observed_holder_count": None,
        "creator_funded_observed_holder_fraction": None,
        "creator_funded_observed_supply_fraction": None,
        "creator_funded_observed_top10_count": None,
        "creator_funded_observed_holders": [],
    }
    import json

    raw = json.dumps(v1, separators=(",", ":"))
    summary = FundingGraphSummary.model_validate_json(raw)
    assert summary.prelaunch is None
    assert summary.model_dump_json() == raw


# ------------------------------------------------------------- structure


def test_a_few_ordinary_transfers_form_no_cluster() -> None:
    rows = tuple(
        row(i, to=wallet(i), value=(i + 3) * CENT, at=CREATION_AT - timedelta(minutes=30 + i))
        for i in range(3)
    )
    pt24 = measure(read(rows)).window(PrelaunchWindow.PT24H)
    assert pt24.unique_funded_address_count == 3
    assert pt24.largest_identical_value_recipient_cluster_count == 1
    assert pt24.largest_identical_value_raw == 3 * CENT  # ties go to the smallest value
    assert pt24.largest_identical_value_recipient_fraction == quantize(Decimal(1) / Decimal(3))
    assert pt24.unique_funding_value_count == 3


def test_many_wallets_with_different_amounts_have_no_large_cluster() -> None:
    rows = tuple(
        row(i, to=wallet(i), value=CENT + i, at=CREATION_AT - timedelta(minutes=50 - i))
        for i in range(40)
    )
    pt1 = measure(read(rows)).window(PrelaunchWindow.PT1H)
    assert pt1.unique_funded_address_count == 40
    assert pt1.unique_funding_value_count == 40
    assert pt1.largest_identical_value_recipient_cluster_count == 1


def test_repeated_payments_to_few_wallets_never_inflate_the_cluster() -> None:
    rows = tuple(
        row(i, to=wallet(i % 2), value=CENT, at=CREATION_AT - timedelta(minutes=20 - i))
        for i in range(20)
    )
    pt1 = measure(read(rows)).window(PrelaunchWindow.PT1H)
    assert pt1.funding_tx_count == 20
    assert pt1.unique_funded_address_count == 2
    assert pt1.largest_identical_value_recipient_cluster_count == 2  # recipients, not transfers
    assert pt1.repeated_funding_tx_count == 18
    assert pt1.max_funding_txs_per_recipient == 10
    assert pt1.max_unique_recipients_in_rolling_10m == 2


def test_only_successful_nonzero_transfers_to_others_count() -> None:
    at = CREATION_AT - timedelta(minutes=5)
    edge = row(1, to=wallet(1), value=CENT, at=at)
    rows = (
        edge,
        edge,  # the same transaction reported twice: counted once
        row(2, to=wallet(2), value=CENT, at=at, ok=False),  # failed
        row(3, to=wallet(3), value=0, at=at),  # zero value
        row(4, to=CREATOR, value=CENT, at=at),  # self transfer
        row(5, to=None, value=CENT, at=at),  # contract creation
    )
    pt1 = measure(read(rows)).window(PrelaunchWindow.PT1H)
    assert pt1.funding_tx_count == 1
    assert pt1.unique_funded_address_count == 1


def test_a_hash_repeated_with_different_contents_voids_the_measurement() -> None:
    at = CREATION_AT - timedelta(minutes=5)
    rows = (row(1, to=wallet(1), value=CENT, at=at), row(1, to=wallet(2), value=CENT, at=at))
    facts = measure(read(rows))
    assert facts.status is Availability.UNAVAILABLE
    assert facts.gap is PrelaunchGap.SOURCE_UNAVAILABLE
    assert facts.failure is AtlasSourceFailure.INVALID_RESPONSE
    assert facts.windows == ()


def test_a_window_starts_exactly_at_its_cutoff() -> None:
    rows = (
        row(1, to=wallet(1), value=CENT, at=CREATION_AT - timedelta(hours=1)),  # on the cutoff
        row(2, to=wallet(2), value=CENT, at=CREATION_AT - timedelta(hours=1, seconds=1)),
    )
    facts = measure(read(rows))
    assert facts.window(PrelaunchWindow.PT1H).unique_funded_address_count == 1
    assert facts.window(PrelaunchWindow.PT6H).unique_funded_address_count == 2


# --------------------------------------------------------------- coverage


def test_a_short_window_can_be_exact_while_a_longer_one_is_a_lower_bound() -> None:
    rows = (row(1, to=wallet(1), value=CENT, at=CREATION_AT - timedelta(minutes=10)),)
    facts = measure(read(rows, ended=False, oldest=CREATION_AT - timedelta(hours=2)))
    coverage = {item.window: item.coverage for item in facts.windows}
    assert coverage == {
        PrelaunchWindow.PT1H: FundingCoverage.COMPLETE,
        PrelaunchWindow.PT6H: FundingCoverage.LOWER_BOUND,
        PrelaunchWindow.PT24H: FundingCoverage.LOWER_BOUND,
    }


def test_a_lower_bound_zero_is_not_an_exact_zero() -> None:
    facts = measure(read((), ended=False, oldest=CREATION_AT - timedelta(minutes=1)))
    for item in facts.windows:
        assert item.funding_tx_count == 0
        assert item.coverage is FundingCoverage.LOWER_BOUND and not item.exact


def test_a_history_that_ended_proves_every_window() -> None:
    facts = measure(read((), ended=True, oldest=CREATION_AT - timedelta(minutes=1)))
    assert all(item.exact for item in facts.windows)
    pt24 = facts.window(PrelaunchWindow.PT24H)
    assert pt24.largest_identical_value_raw is None
    assert pt24.largest_identical_value_recipient_fraction is None
    assert pt24.max_unique_recipients_in_rolling_10m == 0


# ------------------------------------------------------------ fail closed


async def test_without_a_creation_time_prelaunch_is_unavailable_and_v1_unchanged(now) -> None:
    funding = HistoryFunding(prelaunch=revenue_prelaunch_rows())
    snapshot = await snapshot_with(now, funding, creation_at=None)
    ((*_, until),) = funding.calls
    assert until is None  # a V1-only read
    assert snapshot.funding_graph.status is Availability.AVAILABLE
    assert snapshot.funding_graph.direct_funding_tx_count == 0
    pre = snapshot.funding_graph.prelaunch
    assert pre.status is Availability.UNAVAILABLE
    assert pre.gap is PrelaunchGap.CREATION_TIME_UNAVAILABLE
    assert pre.windows == () and pre.creation_timestamp is None


def test_a_creation_transaction_with_another_time_voids_the_measurement() -> None:
    creation = row(
        9,
        to=None,
        value=0,
        at=CREATION_AT + timedelta(seconds=3),
        block=CREATED_BLOCK,
        tx_hash=CREATION_TX,
    )
    facts = measure(read((), window=(creation,)))
    assert facts.gap is PrelaunchGap.SOURCE_UNAVAILABLE
    assert facts.failure is AtlasSourceFailure.INVALID_RESPONSE


def test_a_creation_transaction_at_the_chain_time_is_consistent() -> None:
    creation = row(9, to=None, value=0, at=CREATION_AT, block=CREATED_BLOCK, tx_hash=CREATION_TX)
    assert measure(read((), window=(creation,))).status is Availability.AVAILABLE


def test_a_prelaunch_row_later_than_creation_voids_the_measurement() -> None:
    late = row(1, to=wallet(1), value=CENT, at=CREATION_AT + timedelta(seconds=1))
    assert measure(read((late,))).failure is AtlasSourceFailure.INVALID_RESPONSE


def test_a_read_for_another_history_bound_is_not_this_measurement() -> None:
    facts = measure(read((), until=CREATION_AT - timedelta(hours=6)))
    assert facts.gap is PrelaunchGap.SOURCE_UNAVAILABLE


def test_a_creation_after_the_snapshot_is_no_window() -> None:
    later = origin().model_copy(update={"creation_block": HEAD_BLOCK + 1})
    assert measure(read(()), origin_facts=later).gap is PrelaunchGap.WINDOW_UNAVAILABLE


def test_no_origin_means_no_prelaunch_root() -> None:
    unavailable = OriginFacts(status=Availability.UNAVAILABLE, source="test-origin")
    facts = measure(read(()), origin_facts=unavailable)
    assert facts.gap is PrelaunchGap.ORIGIN_UNAVAILABLE


def test_an_unverified_root_is_measured_and_stays_unverified() -> None:
    from src.agents.atlas.models import OriginVerification

    facts = measure(read(()), origin_facts=origin(verification=OriginVerification.UNVERIFIED))
    assert facts.status is Availability.AVAILABLE
    assert facts.origin_verification == "UNVERIFIED"


# ---------------------------------------------------------- holder overlap


def test_prelaunch_recipients_overlap_the_observed_raw_holders_outside_v4(now) -> None:
    from tests.atlas.conftest import holder_facts

    rows = tuple(
        row(i, to=wallet(i), value=CENT, at=CREATION_AT - timedelta(minutes=5 + i))
        for i in range(4)
    )
    holders = holder_facts(now).model_copy(
        update={
            "top_holders": tuple(
                HolderShare(
                    address=address,
                    balance_raw=1_000 - n,
                    share=Decimal("0.01"),
                    is_burn_address=False,
                )
                for n, address in enumerate((wallet(0), wallet(1), "0x" + "ee" * 20))
            )
        }
    )
    facts = measure(read(rows), holders=holders)
    assert facts.holder_basis is HolderOverlapBasis.RAW
    assert facts.observed_holder_count == 3
    assert facts.creator_funded_observed_holder_count == 2
    assert facts.creator_funded_observed_holder_fraction == quantize(Decimal(2) / Decimal(3))


async def test_unknown_v4_economics_state_no_prelaunch_overlap(now) -> None:
    chain = revenue_like()[0]  # launch NFT in an unverified locker: economics unknown
    snapshot = await snapshot_with(
        now, HistoryFunding(prelaunch=revenue_prelaunch_rows()), chain=chain
    )
    pre = snapshot.funding_graph.prelaunch
    assert pre.status is Availability.AVAILABLE
    assert pre.holder_basis is HolderOverlapBasis.UNKNOWN
    assert pre.creator_funded_observed_holder_count is None
    assert pre.window(PrelaunchWindow.PT24H).unique_funded_address_count == 182


async def test_a_history_defect_leaves_v1_measured_and_prelaunch_unavailable(now) -> None:
    v1_edge = row(
        1, to=wallet(1), value=CENT, at=CREATION_AT + timedelta(minutes=1), block=CREATED_BLOCK + 3
    )
    funding = HistoryFunding(window=(v1_edge,), history_failure=AtlasSourceFailure.INVALID_RESPONSE)
    snapshot = await snapshot_with(now, funding)
    graph = snapshot.funding_graph
    assert len(funding.calls) == 1  # no second read
    assert graph.status is Availability.AVAILABLE
    assert graph.coverage is FundingCoverage.COMPLETE
    assert graph.direct_funding_tx_count == 1
    assert graph.unique_direct_funded_address_count == 1
    assert graph.prelaunch.status is Availability.UNAVAILABLE
    assert graph.prelaunch.gap is PrelaunchGap.SOURCE_UNAVAILABLE
    assert graph.prelaunch.failure is AtlasSourceFailure.INVALID_RESPONSE
    payload = onchain_payload(snapshot, evaluate_snapshot(snapshot, now))
    summary = payload.intelligence.funding_graph
    assert summary.direct_funding_tx_count == 1
    assert summary.prelaunch.status == "UNAVAILABLE" and summary.prelaunch.windows == ()


async def test_v1_counts_ignore_the_v2_block_time_of_a_repeated_row(now) -> None:
    first = row(1, to=wallet(1), value=CENT, at=CREATION_AT, block=CREATED_BLOCK + 3)
    again = first.model_copy(update={"observed_at": CREATION_AT + timedelta(seconds=9)})
    graph = (await snapshot_with(now, HistoryFunding(window=(first, again)))).funding_graph
    assert graph.status is Availability.AVAILABLE
    assert graph.direct_funding_tx_count == 1  # one transaction, whatever its V2 time says
