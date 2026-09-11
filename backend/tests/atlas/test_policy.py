"""The deterministic safety core: facts in, verdict out, no model anywhere."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.atlas.models import (
    ZERO_ADDRESS,
    AtlasDomain,
    AtlasReasonCode,
    AtlasSourceFailure,
    AtlasVerdict,
    HolderObservationBasis,
    ProxyObservation,
)
from src.agents.atlas.policy import ATLAS_POLICY_V1, AtlasPolicy, evaluate_snapshot
from src.markets.models import Availability
from tests.atlas.conftest import (
    chain_snapshot,
    contract_facts,
    holder_facts,
    origin_facts,
    snapshot,
)

# Holder intelligence is required by policy, so a snapshot can only be CLEAR when
# a holder source actually answered.
COMPLETE = ATLAS_POLICY_V1


def test_complete_fresh_facts_are_clear(now):
    decision = evaluate_snapshot(snapshot(now), now)
    assert decision.verdict == AtlasVerdict.CLEAR
    assert decision.blockers == ()
    assert decision.data_gaps == ()
    assert decision.policy_version == "atlas-policy-v2"


# ------------------------------------------------------- known bad != unknown


def test_measured_violation_blocks_while_facts_stay_available(now):
    """The central distinction: this token was measured, and it is dangerous."""
    absent_code = snapshot(now, contract=contract_facts(code_present=False))
    decision = evaluate_snapshot(absent_code, now)
    assert decision.verdict == AtlasVerdict.BLOCKED
    assert AtlasReasonCode.CONTRACT_CODE_ABSENT in decision.blockers
    # The fact itself was perfectly obtainable; nothing here is unknown.
    assert decision.data_gaps == ()
    assert absent_code.availability(AtlasDomain.CONTRACT) == Availability.AVAILABLE


def test_unobtainable_fact_is_insufficient_not_dangerous(now):
    missing = snapshot(
        now,
        holders=holder_facts(
            now, status=Availability.UNAVAILABLE, failure=AtlasSourceFailure.NOT_CONFIGURED
        ),
    )
    decision = evaluate_snapshot(missing, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED in decision.data_gaps
    # Not knowing is never reported as a measured violation.
    assert decision.blockers == ()


def test_a_known_violation_outranks_a_missing_fact(now):
    both = snapshot(
        now,
        contract=contract_facts(code_present=False),
        holders=holder_facts(now, status=Availability.UNKNOWN),
    )
    decision = evaluate_snapshot(both, now)
    assert decision.verdict == AtlasVerdict.BLOCKED
    assert decision.blockers and decision.data_gaps


# ------------------------------------------------------------ zero vs unknown


def test_available_zero_supply_blocks_and_unknown_supply_does_not(now):
    zero = evaluate_snapshot(snapshot(now, contract=contract_facts(total_supply_raw=0)), now)
    assert zero.verdict == AtlasVerdict.BLOCKED
    assert AtlasReasonCode.TOTAL_SUPPLY_ZERO in zero.blockers

    unknown = evaluate_snapshot(snapshot(now, contract=contract_facts(total_supply_raw=None)), now)
    assert unknown.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.TOTAL_SUPPLY_UNKNOWN in unknown.data_gaps
    # A measured zero and an unmeasured supply are never the same conclusion.
    assert AtlasReasonCode.TOTAL_SUPPLY_ZERO not in unknown.blockers


# ------------------------------------------------------------- chain identity


@pytest.mark.parametrize("chain_id", [56, 1, 999])
def test_wrong_chain_id_fails_closed(now, chain_id):
    wrong = snapshot(now, chain=chain_snapshot(now, chain_id=chain_id))
    decision = evaluate_snapshot(wrong, now)
    assert decision.verdict == AtlasVerdict.BLOCKED
    assert AtlasReasonCode.CHAIN_ID_MISMATCH in decision.blockers


def test_unrecognised_chain_fails_closed(now):
    other = snapshot(
        now,
        market=__import__("tests.atlas.conftest", fromlist=["market_identity"]).market_identity(
            chain="ethereum"
        ),
        chain=chain_snapshot(now, chain="ethereum", chain_id=1),
    )
    assert evaluate_snapshot(other, now).verdict == AtlasVerdict.BLOCKED


# ------------------------------------------------------------ freshness / skew


def test_stale_snapshot_is_insufficient(now):
    decision = evaluate_snapshot(snapshot(now), now + timedelta(minutes=11))
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.SNAPSHOT_STALE in decision.data_gaps


def test_source_skew_beyond_policy_is_insufficient(now):
    skewed = snapshot(now, holders=holder_facts(now, observed_at=now - timedelta(minutes=6)))
    decision = evaluate_snapshot(skewed, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED in decision.data_gaps


def test_skew_within_policy_is_accepted(now):
    close = snapshot(now, holders=holder_facts(now, observed_at=now - timedelta(minutes=4)))
    assert evaluate_snapshot(close, now).verdict == AtlasVerdict.CLEAR


# ------------------------------------------------- provenance assurance gating


def test_paper_policy_accepts_the_weaker_response_anchor_deliberately(now):
    """RESPONSE_TIME is accepted here, and that is a named decision.

    It is sound for PAPER evaluation and recorded as the weaker basis it is.
    What it is not is equivalent to block-anchored provenance: it cannot detect
    an indexer running behind, which is why the acceptance is a policy field
    rather than an implicit consequence of what a vendor happens to return.
    """
    assert HolderObservationBasis.RESPONSE_TIME in ATLAS_POLICY_V1.accepted_holder_observation_bases
    response_anchored = snapshot(
        now,
        holders=holder_facts(
            now,
            observation_basis=HolderObservationBasis.RESPONSE_TIME,
            snapshot_block=None,
            holder_block_delta=None,
        ),
    )
    assert evaluate_snapshot(response_anchored, now).verdict == AtlasVerdict.CLEAR


def test_a_policy_demanding_block_provenance_rejects_a_response_anchor(now):
    """The PRE-LIVE invariant, expressed as one field a successor policy sets."""
    block_pinned_only = replace(
        ATLAS_POLICY_V1,
        accepted_holder_observation_bases=frozenset({HolderObservationBasis.SOURCE_BLOCK}),
    )
    response_anchored = snapshot(
        now,
        holders=holder_facts(
            now,
            observation_basis=HolderObservationBasis.RESPONSE_TIME,
            snapshot_block=None,
            holder_block_delta=None,
        ),
    )
    decision = evaluate_snapshot(response_anchored, now, block_pinned_only)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE in decision.data_gaps
    # An answer arrived and was well formed; it is the assurance that is short.
    assert response_anchored.availability(AtlasDomain.HOLDERS) == Availability.AVAILABLE


def test_a_policy_accepting_no_observation_basis_at_all_is_refused():
    with pytest.raises(ValueError):
        replace(ATLAS_POLICY_V1, accepted_holder_observation_bases=frozenset())


# --------------------------------------------- threshold vs source exclusions


def test_a_filtered_holder_set_is_still_measured_while_no_threshold_judges_it(now):
    """Today the metric is observed and recorded; nothing decides on it."""
    assert ATLAS_POLICY_V1.max_top10_concentration is None
    filtered = snapshot(now, holders=holder_facts(now, excluded_addresses=(ZERO_ADDRESS,)))
    decision = evaluate_snapshot(filtered, now)
    assert decision.verdict == AtlasVerdict.CLEAR
    assert filtered.holders.top10_share == Decimal("0.30")


def test_a_threshold_may_not_silently_judge_a_metric_with_source_exclusions(now):
    """The fail-open a limit exists to prevent.

    Every provider exclusion removes supply from the numerator while the
    denominator stays full on-chain supply, so the metric can only understate
    concentration. A threshold that read such a figure as a pass would approve a
    distribution it never saw. Nothing reconciles an exclusion today, so an
    exclusion makes the case insufficient instead.
    """
    with_threshold = replace(ATLAS_POLICY_V1, max_top10_concentration=Decimal("0.90"))
    filtered = snapshot(now, holders=holder_facts(now, excluded_addresses=(ZERO_ADDRESS,)))
    decision = evaluate_snapshot(filtered, now, with_threshold)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE in decision.data_gaps
    # The same policy over an unfiltered holder set decides normally.
    unfiltered = snapshot(now, holders=holder_facts(now))
    assert evaluate_snapshot(unfiltered, now, with_threshold).verdict == AtlasVerdict.CLEAR


def test_an_exceeded_limit_still_blocks_even_on_an_understated_metric(now):
    """Understated and already over the line means the true figure is over it too."""
    with_threshold = replace(ATLAS_POLICY_V1, max_top10_concentration=Decimal("0.10"))
    filtered = snapshot(now, holders=holder_facts(now, excluded_addresses=(ZERO_ADDRESS,)))
    decision = evaluate_snapshot(filtered, now, with_threshold)
    assert decision.verdict == AtlasVerdict.BLOCKED
    assert AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED in decision.blockers


# ------------------------------------------------------- required vs optional


def test_origin_is_not_required_so_its_absence_does_not_block(now):
    """Creator provenance has no verified source, and policy does not require it."""
    assert AtlasDomain.ORIGIN not in ATLAS_POLICY_V1.required_domains
    without_origin = snapshot(
        now,
        origin=origin_facts(
            status=Availability.UNAVAILABLE, failure=AtlasSourceFailure.NOT_CONFIGURED
        ),
    )
    assert evaluate_snapshot(without_origin, now).verdict == AtlasVerdict.CLEAR


def test_required_contract_domain_absence_blocks(now):
    decision = evaluate_snapshot(
        snapshot(
            now,
            contract=contract_facts(
                status=Availability.UNAVAILABLE, failure=AtlasSourceFailure.TIMEOUT
            ),
        ),
        now,
    )
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE in decision.data_gaps


# ------------------------------------------------------ concentration boundary


def concentration_policy(limit: str) -> AtlasPolicy:
    return AtlasPolicy(
        version="atlas-policy-test",
        expected_chain_ids=dict(ATLAS_POLICY_V1.expected_chain_ids),
        required_domains=ATLAS_POLICY_V1.required_domains,
        snapshot_validity=ATLAS_POLICY_V1.snapshot_validity,
        max_source_skew=ATLAS_POLICY_V1.max_source_skew,
        max_top10_concentration=Decimal(limit),
        block_on_proxy_admin=True,
    )


@pytest.mark.parametrize(
    "share,blocked",
    [("0.49", False), ("0.50", False), ("0.500000000000000001", True), ("0.51", True)],
)
def test_concentration_boundary_is_exclusive_and_exact(now, share, blocked):
    """At the limit is allowed; above it blocks. Decimal throughout, no float."""
    policy = concentration_policy("0.50")
    subject = snapshot(now, holders=holder_facts(now, top10=share))
    decision = evaluate_snapshot(subject, now, policy)
    assert (AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED in decision.blockers) is blocked


def test_the_shipped_policy_measures_concentration_without_blocking_on_it(now):
    """A financially meaningful threshold is a product decision, not an invention.

    The metric is collected and recorded; the blocker stays disabled until a limit
    is chosen deliberately. Required holder data being absent still blocks.
    """
    assert ATLAS_POLICY_V1.max_top10_concentration is None
    extreme = snapshot(now, holders=holder_facts(now, top10="0.99"))
    assert evaluate_snapshot(extreme, now).verdict == AtlasVerdict.CLEAR


def test_proxy_admin_blocks_only_when_policy_says_so(now):
    proxied = snapshot(
        now,
        contract=contract_facts(proxy=ProxyObservation.EIP1967_DETECTED, admin="0x" + "d4" * 20),
    )
    assert evaluate_snapshot(proxied, now).verdict == AtlasVerdict.CLEAR
    strict = evaluate_snapshot(proxied, now, concentration_policy("0.99"))
    assert AtlasReasonCode.PROXY_ADMIN_PRESENT in strict.blockers


def test_empty_proxy_slots_are_not_a_proof_of_absence(now):
    facts = contract_facts(proxy=ProxyObservation.EIP1967_SLOTS_EMPTY)
    # The observation is recorded as "these documented slots were empty", which is
    # a weaker statement than "this is not a proxy".
    assert facts.proxy == ProxyObservation.EIP1967_SLOTS_EMPTY
    assert facts.proxy != ProxyObservation.NOT_CHECKED


def test_invalid_policies_are_rejected():
    for field, value in (
        ("snapshot_validity", timedelta(0)),
        ("max_source_skew", timedelta(seconds=-1)),
        ("max_top10_concentration", Decimal("0")),
        ("max_top10_concentration", Decimal("1.5")),
        ("required_domains", frozenset()),
    ):
        base = {
            "version": "t",
            "expected_chain_ids": {"robinhood": 4663},
            "required_domains": frozenset({AtlasDomain.CONTRACT}),
            "snapshot_validity": timedelta(minutes=1),
            "max_source_skew": timedelta(minutes=1),
            "max_top10_concentration": None,
            "block_on_proxy_admin": False,
        }
        with pytest.raises(ValueError):
            AtlasPolicy(**{**base, field: value})


# ------------------------------------------------- freshness cannot be refetched


def test_refetching_an_unchanged_snapshot_does_not_renew_its_freshness(now):
    """The collector running again is not evidence that the world moved.

    A provider that keeps returning its 10:00 snapshot is still describing 10:00,
    no matter how recently it was asked.
    """
    old_observation = now - timedelta(minutes=30)
    stale = snapshot(
        now,
        chain=chain_snapshot(now, block_timestamp=old_observation, fetched_at=now),
        holders=holder_facts(now, observed_at=old_observation),
        # The collector ran just now, which must not matter.
        collected_at=now,
    )
    decision = evaluate_snapshot(stale, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA
    assert AtlasReasonCode.SNAPSHOT_STALE in decision.data_gaps


def test_freshness_follows_chain_time_not_fetch_time(now):
    """A block mined long ago is old however recently it was read."""
    old_block = snapshot(
        now,
        chain=chain_snapshot(now, block_timestamp=now - timedelta(minutes=20), fetched_at=now),
        holders=holder_facts(now, observed_at=now - timedelta(minutes=20)),
    )
    assert AtlasReasonCode.SNAPSHOT_STALE in evaluate_snapshot(old_block, now).data_gaps

    fresh_block = snapshot(
        now,
        chain=chain_snapshot(now, block_timestamp=now - timedelta(minutes=2)),
        holders=holder_facts(now, observed_at=now - timedelta(minutes=2)),
    )
    assert evaluate_snapshot(fresh_block, now).verdict == AtlasVerdict.CLEAR


def test_the_oldest_contributing_source_decides(now):
    """One current source does not rescue another that is out of date."""
    mixed = snapshot(
        now,
        chain=chain_snapshot(now, block_timestamp=now),
        holders=holder_facts(now, observed_at=now - timedelta(minutes=30)),
    )
    assert mixed.oldest_source_observation == now - timedelta(minutes=30)
    decision = evaluate_snapshot(mixed, now)
    assert decision.verdict == AtlasVerdict.INSUFFICIENT_DATA


# ----------------------------------------------- factual truth under a blocker


def test_a_blocker_never_erases_the_record_of_a_missing_fact(now):
    """Precedence applies to the decision, not to what the snapshot says happened.

    An operator must still be able to see both that the contract is broken and
    that holder data was never obtained.
    """
    mixed = snapshot(
        now,
        contract=contract_facts(code_present=False),
        holders=holder_facts(
            now, status=Availability.UNAVAILABLE, failure=AtlasSourceFailure.NOT_CONFIGURED
        ),
    )
    decision = evaluate_snapshot(mixed, now)
    assert decision.verdict == AtlasVerdict.BLOCKED
    # The known violation is reported...
    assert AtlasReasonCode.CONTRACT_CODE_ABSENT in decision.blockers
    # ...and the unresolved required fact is still recorded, not discarded.
    assert AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED in decision.data_gaps
    assert decision.domain_status["HOLDERS"] == Availability.UNAVAILABLE.value
    assert decision.domain_status["CONTRACT"] == Availability.AVAILABLE.value
    # Nothing anywhere claims the holder domain was available.
    assert mixed.holders.top1_share is None
    assert mixed.holders.failure == AtlasSourceFailure.NOT_CONFIGURED
