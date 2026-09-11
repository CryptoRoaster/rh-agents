"""Phase 2E end to end: verified providers, real normalization, unchanged authority.

Phase 2D could not reach CLEAR, because no holder source existed. These tests
drive the real adapters over fixture transports through the real collector and
the real policy, and prove both directions: a complete, fresh, token-matched
holder fact now satisfies the holder domain, and nothing about that weakens a
blocker, a staleness rule or a provider outage.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.atlas.context import (
    AtlasSnapshotBuilder,
    atlas_snapshot_digest,
    snapshot_document,
    source_skew,
)
from src.agents.atlas.models import (
    ZERO_ADDRESS,
    AtlasDomain,
    AtlasReasonCode,
    AtlasSourceFailure,
    AtlasVerdict,
    HolderCompleteness,
    HolderObservationBasis,
    OriginVerification,
)
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.sources.blockscout import (
    BlockscoutConfig,
    BlockscoutContractOriginSource,
    BlockscoutHolderSource,
)
from src.agents.atlas.sources.moralis import MoralisConfig, MoralisHolderSource
from src.agents.atlas.sources.routing import RoutedHolderSource, RoutedOriginSource
from src.core.clock import FixedClock
from src.markets.models import Availability
from tests.atlas.conftest import (
    TOKEN,
    StubContracts,
    StubOrigins,
    StubVerifier,
    chain_snapshot,
    contract_facts,
    market_identity,
    origin_facts,
)
from tests.atlas.fake_http import RecordingRoutes, json_response
from tests.atlas.test_blockscout import CREATION_TX, head, metadata, page
from tests.atlas.test_bsc_sources import creation_payload, owners

CHAIN_BLOCK = 1_000_000
HEAD_BLOCK = 999_998
SUPPLY = 10**24

BLOCKSCOUT = BlockscoutConfig(
    base_url="https://api.blockscout.test", chain_id=4663, api_key="proapi_testkey", max_pages=2
)
MORALIS = MoralisConfig(
    base_url="https://deep-index.moralis.test/api/v2.2", api_key="moralis_testkey", max_pages=2
)


def blockscout_routes(*, holders=None, head_block=HEAD_BLOCK, head_offset=90, now=None):
    timestamp = (now - timedelta(seconds=head_offset)).isoformat().replace("+00:00", "Z")
    return RecordingRoutes(
        {
            "/main-page/blocks": lambda request: json_response(
                head(height=head_block, timestamp=timestamp)
            ),
            "/holders": lambda request: json_response(holders if holders else page(12)),
            "/v2/api": lambda request: json_response(creation_payload(address=TOKEN)),
            "/api/v2/tokens/" + TOKEN: lambda request: json_response(metadata()),
        }
    )


def robinhood_builder(
    now, *, recording=None, contract=None, verifier=None, block_offset=60, clock_at=None
):
    recording = recording if recording is not None else blockscout_routes(now=now)
    factory = recording.transport_factory()
    return AtlasSnapshotBuilder(
        contracts=StubContracts(
            chain_snapshot(
                now,
                block=CHAIN_BLOCK,
                block_timestamp=now - timedelta(seconds=block_offset),
                fetched_at=now,
            ),
            contract if contract is not None else contract_facts(block=CHAIN_BLOCK),
        ),
        holders=RoutedHolderSource(
            sources={
                "robinhood": BlockscoutHolderSource(
                    config=BLOCKSCOUT, chain="robinhood", transport_factory=factory
                )
            }
        ),
        origins=RoutedOriginSource(
            sources={
                "robinhood": BlockscoutContractOriginSource(
                    config=BLOCKSCOUT, chain="robinhood", transport_factory=factory
                )
            }
        ),
        verifier=verifier,
        clock=FixedClock(clock_at if clock_at is not None else now),
    )


def bsc_builder(now, *, holders=None, observed_offset=30):
    recording = RecordingRoutes(
        {
            "/owners": lambda request: json_response(holders if holders else owners(12)),
            "/v2/api": lambda request: json_response(creation_payload(address=TOKEN)),
        }
    )
    factory = recording.transport_factory()
    return AtlasSnapshotBuilder(
        contracts=StubContracts(
            chain_snapshot(
                now,
                chain="bsc",
                chain_id=56,
                block=CHAIN_BLOCK,
                block_timestamp=now - timedelta(seconds=60),
                fetched_at=now,
            ),
            contract_facts(block=CHAIN_BLOCK),
        ),
        holders=RoutedHolderSource(
            sources={
                "bsc": MoralisHolderSource(
                    config=MORALIS,
                    chain="bsc",
                    clock=FixedClock(now - timedelta(seconds=observed_offset)),
                    transport_factory=factory,
                )
            }
        ),
        origins=RoutedOriginSource(sources={}),
        clock=FixedClock(now),
    )


async def build(builder, now, *, chain="robinhood"):
    from uuid import uuid4

    return await builder.build(uuid4(), uuid4(), market_identity(chain=chain))


async def test_robinhood_holder_intelligence_now_lets_atlas_reach_clear(now):
    """The scenario Phase 2D could not produce at all."""
    snapshot = await build(robinhood_builder(now), now)
    assert snapshot.holders.status == Availability.AVAILABLE
    assert snapshot.holders.completeness == HolderCompleteness.COMPLETE
    assert snapshot.holders.observation_basis == HolderObservationBasis.SOURCE_BLOCK
    assert snapshot.holders.snapshot_block == HEAD_BLOCK
    assert snapshot.holders.holder_block_delta == CHAIN_BLOCK - HEAD_BLOCK
    assert snapshot.holders.total_supply_raw == SUPPLY
    assert snapshot.holders.top1_share == Decimal("0.05")

    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict == AtlasVerdict.CLEAR
    assert decision.blockers == ()
    assert decision.data_gaps == ()
    assert decision.domain_status[AtlasDomain.HOLDERS.value] == "AVAILABLE"


async def test_bsc_holder_intelligence_reaches_the_same_domain_semantics(now):
    snapshot = await build(bsc_builder(now), now, chain="bsc")
    assert snapshot.holders.status == Availability.AVAILABLE
    # A different vendor, a weaker provenance basis, the same domain outcome.
    assert snapshot.holders.observation_basis == HolderObservationBasis.RESPONSE_TIME
    assert snapshot.holders.snapshot_block is None
    assert evaluate_snapshot(snapshot, now).verdict == AtlasVerdict.CLEAR


async def test_a_provider_side_exclusion_is_carried_into_the_fact_and_the_digest(now):
    """Blockscout filters the zero address out of its holder list.

    That is not something a reader should have to infer from a burn total of
    zero, so the exclusion travels with the fact, reaches the fact document the
    digest is taken over, and withholds the adjustment that would otherwise rest
    on a burn figure that could never have seen the sink.
    """
    snapshot = await build(robinhood_builder(now), now)
    assert snapshot.holders.completeness == HolderCompleteness.COMPLETE
    assert snapshot.holders.excluded_addresses == (ZERO_ADDRESS,)
    assert snapshot.holders.burned_raw is None
    assert snapshot.holders.top10_share_excluding_burn is None
    # The raw metric is untouched: a filtered row cannot be added back, and
    # nothing else is removed from it.
    assert snapshot.holders.top1_share == Decimal("0.05")
    document = snapshot_document(snapshot)
    holders = document["holders"]
    assert isinstance(holders, dict)
    assert holders["excluded_addresses"] == [ZERO_ADDRESS]


async def test_a_vendor_that_filters_nothing_still_reports_its_burn_adjustment(now):
    """The withholding is about the exclusion, not about the vendor's name."""
    snapshot = await build(bsc_builder(now), now, chain="bsc")
    assert snapshot.holders.excluded_addresses == ()
    assert snapshot.holders.completeness == HolderCompleteness.COMPLETE
    assert snapshot.holders.burned_raw is not None


async def test_equivalent_distributions_normalize_identically_across_vendors(now):
    robinhood = await build(robinhood_builder(now), now)
    bsc = await build(bsc_builder(now), now, chain="bsc")
    assert robinhood.holders.top1_share == bsc.holders.top1_share
    assert robinhood.holders.top5_share == bsc.holders.top5_share
    assert robinhood.holders.top10_share == bsc.holders.top10_share
    # Only the provenance differs, and it differs honestly.
    assert robinhood.holders.source != bsc.holders.source


async def test_complete_holder_data_never_weakens_a_contract_blocker(now):
    snapshot = await build(
        robinhood_builder(now, contract=contract_facts(block=CHAIN_BLOCK, code_present=False)),
        now,
    )
    assert snapshot.holders.status == Availability.AVAILABLE
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict == AtlasVerdict.BLOCKED
    assert AtlasReasonCode.CONTRACT_CODE_ABSENT in decision.blockers


async def test_a_provider_outage_returns_the_case_to_insufficient_data(now):
    recording = RecordingRoutes({"": lambda request: json_response({"error": "down"}, status=503)})
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.status == Availability.UNAVAILABLE
    assert snapshot.holders.failure == AtlasSourceFailure.UNAVAILABLE
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE in decision.data_gaps


async def test_an_unconfigured_chain_is_still_explicitly_unavailable(now):
    builder = robinhood_builder(now)
    empty = AtlasSnapshotBuilder(
        contracts=builder.contracts,
        holders=RoutedHolderSource(sources={}),
        origins=RoutedOriginSource(sources={}),
        clock=FixedClock(now),
    )
    snapshot = await build(empty, now)
    decision = evaluate_snapshot(snapshot, now)
    assert AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED in decision.data_gaps


async def test_an_indexer_far_behind_the_chain_cannot_be_rescued_by_a_fresh_fetch(now):
    """The holder snapshot is judged by when the source observed, not when we asked."""
    recording = blockscout_routes(now=now, head_block=990_000, head_offset=20 * 60)
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.holder_block_delta == 10_000
    # We fetched a moment ago; that fact deliberately does not participate.
    assert snapshot.collected_at == now
    decision = evaluate_snapshot(snapshot, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED in decision.data_gaps
    assert AtlasReasonCode.SNAPSHOT_STALE in decision.data_gaps


async def test_reported_skew_is_measured_between_observations_not_between_fetches(now):
    """`source_skew` must answer the same question the policy asks.

    Fetch times would put both operands within milliseconds of each other however
    old either fact is, which would turn the reported spread into a measurement
    of our own scheduling.
    """
    recording = blockscout_routes(now=now, head_block=990_000, head_offset=20 * 60)
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.chain.observed_at == now
    assert source_skew(snapshot) == timedelta(minutes=20) - timedelta(seconds=60)
    assert AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED in evaluate_snapshot(snapshot, now).data_gaps


async def test_a_receipt_anchored_source_reports_a_spread_that_means_less(now):
    """The same number, a weaker meaning — which is why the basis travels with it.

    Against a BSC fact this bounds the pinned block's age at the moment of the
    answer. It is not a source-to-source skew and cannot see a lagging indexer,
    so assurance is gated on the basis rather than on this figure.
    """
    snapshot = await build(bsc_builder(now, observed_offset=0), now, chain="bsc")
    assert snapshot.holders.observation_basis == HolderObservationBasis.RESPONSE_TIME
    assert source_skew(snapshot) == timedelta(seconds=60)


async def test_holder_data_about_another_token_is_never_accepted(now):
    other = "0x" + "99" * 20
    recording = blockscout_routes(now=now)
    recording.routes["/api/v2/tokens/" + TOKEN] = lambda request: json_response(
        {**metadata(), "address_hash": other}
    )
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.failure == AtlasSourceFailure.TOKEN_MISMATCH
    assert evaluate_snapshot(snapshot, now).verdict == AtlasVerdict.INSUFFICIENT_DATA


async def test_holders_cannot_hold_more_than_the_chain_says_exists(now):
    oversized = page(12, top=SUPPLY)
    recording = blockscout_routes(now=now, holders=oversized)
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.failure == AtlasSourceFailure.SUPPLY_INCONSISTENT


async def test_a_creation_claim_is_confirmed_against_the_chain_receipt(now):
    snapshot = await build(
        robinhood_builder(now, verifier=StubVerifier(created=TOKEN, creator_is_contract=True)),
        now,
    )
    assert snapshot.origin.status == Availability.AVAILABLE
    assert snapshot.origin.creation_tx_hash == CREATION_TX
    assert snapshot.origin.verification == OriginVerification.RECEIPT_CONFIRMED
    # Deployed by code: recorded as a fact, not read as a developer wallet.
    assert snapshot.origin.creator_is_contract is True


async def test_a_creation_transaction_that_made_another_contract_fails_closed(now):
    snapshot = await build(
        robinhood_builder(now, verifier=StubVerifier(created="0x" + "99" * 20)), now
    )
    assert snapshot.origin.status == Availability.UNAVAILABLE
    assert snapshot.origin.failure == AtlasSourceFailure.INVALID_RESPONSE


async def test_an_unverifiable_claim_stays_unverified_rather_than_confirmed(now):
    snapshot = await build(robinhood_builder(now, verifier=StubVerifier(created=None)), now)
    assert snapshot.origin.status == Availability.AVAILABLE
    assert snapshot.origin.verification == OriginVerification.UNVERIFIED


async def test_the_digest_follows_the_facts_and_not_the_fetch(now):
    first = await build(robinhood_builder(now), now)
    later = await build(robinhood_builder(now, clock_at=now + timedelta(minutes=1)), now)
    # Unchanged provider state fetched a minute later: identical fingerprint, so
    # re-collecting cannot make an old observation look like a new one.
    assert later.collected_at != first.collected_at
    assert atlas_snapshot_digest(first) == atlas_snapshot_digest(later)

    moved = blockscout_routes(now=now, holders=page(12, top=6 * 10**22))
    changed = await build(robinhood_builder(now, recording=moved), now)
    assert atlas_snapshot_digest(changed) != atlas_snapshot_digest(first)


@pytest.mark.parametrize("head_block", [HEAD_BLOCK, 999_000])
async def test_the_indexer_head_is_always_recorded_as_lag(now, head_block):
    recording = blockscout_routes(now=now, head_block=head_block)
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.snapshot_block == head_block
    assert snapshot.holders.holder_block_delta == CHAIN_BLOCK - head_block


async def test_a_stub_origin_without_a_verifier_is_left_untouched(now):
    builder = robinhood_builder(now)
    plain = AtlasSnapshotBuilder(
        contracts=builder.contracts,
        holders=builder.holders,
        origins=StubOrigins(origin_facts()),
        clock=FixedClock(now),
    )
    snapshot = await build(plain, now)
    assert snapshot.origin.verification == OriginVerification.NOT_ATTEMPTED


async def test_an_indexer_ahead_of_the_pinned_block_is_the_ordinary_case(now):
    """The pinned block trails the head by the confirmation lag, so the delta is negative."""
    recording = blockscout_routes(now=now, head_block=CHAIN_BLOCK + 12, head_offset=5)
    snapshot = await build(robinhood_builder(now, recording=recording), now)
    assert snapshot.holders.holder_block_delta == -12
    # A signed distance, not a one-directional lag, and well within policy.
    assert evaluate_snapshot(snapshot, now).verdict == AtlasVerdict.CLEAR
