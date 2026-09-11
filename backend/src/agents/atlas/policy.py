"""Versioned deterministic ATLAS safety policy.

This module decides whether a candidate is safe enough to proceed. It contains
no model, reads no prompt, and nothing it produces can be argued with. Thresholds
live here as code, never in prompt text and never chosen by a model.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.agents.atlas.models import (
    AtlasDomain,
    AtlasOnchainSnapshot,
    AtlasReasonCode,
    AtlasSafetyDecision,
    AtlasVerdict,
    HolderCompleteness,
    HolderObservationBasis,
    ProxyObservation,
)
from src.markets.models import Availability

UNAVAILABLE_REASONS: dict[AtlasDomain, AtlasReasonCode] = {
    AtlasDomain.CONTRACT: AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE,
    AtlasDomain.HOLDERS: AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE,
    AtlasDomain.ORIGIN: AtlasReasonCode.ORIGIN_FACTS_UNAVAILABLE,
}

NOT_CONFIGURED_REASONS: dict[AtlasDomain, AtlasReasonCode] = {
    AtlasDomain.HOLDERS: AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED,
    AtlasDomain.ORIGIN: AtlasReasonCode.ORIGIN_SOURCE_NOT_CONFIGURED,
}


@dataclass(frozen=True)
class AtlasPolicy:
    version: str
    expected_chain_ids: dict[str, int]
    required_domains: frozenset[AtlasDomain]
    snapshot_validity: timedelta
    max_source_skew: timedelta
    # None disables the threshold blocker while still measuring the metric. A
    # concentration limit is a product decision with real financial meaning, so
    # an invented number would be worse than an explicit absence.
    max_top10_concentration: Decimal | None
    block_on_proxy_admin: bool
    # Which holder-coverage proofs are good enough to measure a top-ten share.
    # A source that could not establish its own coverage never satisfies the
    # holder domain, however cleanly it returned HTTP 200.
    accepted_holder_completeness: frozenset[HolderCompleteness] = frozenset(
        {HolderCompleteness.COMPLETE, HolderCompleteness.TOP_N_ONLY}
    )
    # Which holder provenance qualities are good enough to act on. Accepting
    # RESPONSE_TIME is a deliberate PAPER-mode decision, not an oversight: a
    # response receipt proves when a representation arrived, never that the
    # indexed state behind it is that recent, so it cannot detect an indexer
    # running behind. A LIVE policy narrows this to SOURCE_BLOCK by changing one
    # field, which is why the acceptance is a named policy input rather than an
    # implicit consequence of a provider's capabilities.
    accepted_holder_observation_bases: frozenset[HolderObservationBasis] = frozenset(
        {HolderObservationBasis.SOURCE_BLOCK, HolderObservationBasis.RESPONSE_TIME}
    )

    def __post_init__(self) -> None:
        if self.snapshot_validity <= timedelta(0) or self.max_source_skew < timedelta(0):
            raise ValueError("Snapshot validity must be positive and skew non-negative")
        if HolderCompleteness.UNKNOWN in self.accepted_holder_completeness:
            raise ValueError("Unproven holder coverage can never satisfy the holder domain")
        if not self.accepted_holder_observation_bases:
            raise ValueError("At least one holder observation basis must be acceptable")
        if self.max_top10_concentration is not None and not (
            Decimal(0) < self.max_top10_concentration <= Decimal(1)
        ):
            raise ValueError("A concentration limit must fall in (0, 1]")
        if not self.required_domains:
            raise ValueError("At least one fact domain must be required")


# Provisional PAPER-mode policy. Holder intelligence is required because
# autonomous trading cannot be justified without it. Phase 2E connects verified
# holder sources, so this requirement is now satisfiable — but only by facts that
# are fresh, token-matched and provably complete enough to support the metric.
# Where no provider is configured the domain stays unavailable and ATLAS still
# cannot reach CLEAR, which is the same fail-closed behaviour as before.
#
# ``max_top10_concentration`` stays disabled deliberately. A concentration limit
# is a product decision with real financial meaning, and enabling one merely
# because the data finally exists would invent a threshold nobody chose. With it
# disabled, a PASS on the holder domain means the data-quality prerequisite was
# met — not that the distribution was judged safe.
#
# PRE-LIVE INVARIANT. This policy accepts RESPONSE_TIME holder provenance, which
# is sound for PAPER evaluation and insufficient for autonomous execution: it
# cannot detect a lagging indexer. Before LIVE_AUTONOMOUS is enabled, safety
# critical holder data must carry provenance able to expose material indexer lag
# — a successor policy must drop RESPONSE_TIME from
# ``accepted_holder_observation_bases``, unless a provider contract gives an
# independently trustworthy current-state freshness guarantee that is reviewed
# and approved on its own merits. Nothing here enables live mode.
ATLAS_POLICY_V2 = AtlasPolicy(
    version="atlas-policy-v2",
    expected_chain_ids={"robinhood": 4663, "bsc": 56},
    required_domains=frozenset({AtlasDomain.CONTRACT, AtlasDomain.HOLDERS}),
    snapshot_validity=timedelta(minutes=10),
    max_source_skew=timedelta(minutes=5),
    max_top10_concentration=None,
    block_on_proxy_admin=False,
)

# The name Phase 2D shipped under. Kept as an alias so existing call sites and
# evidence readers keep working; the policy itself is versioned in its payload.
ATLAS_POLICY_V1 = ATLAS_POLICY_V2


def _data_gaps(
    snapshot: AtlasOnchainSnapshot, now: datetime, policy: AtlasPolicy
) -> list[AtlasReasonCode]:
    gaps: list[AtlasReasonCode] = []
    # Anchored to when the sources observed reality, not to when the collector
    # ran. Re-fetching an unchanged provider snapshot cannot renew its freshness.
    age = now - snapshot.oldest_source_observation
    if age < timedelta(0) or age > policy.snapshot_validity:
        gaps.append(AtlasReasonCode.SNAPSHOT_STALE)
    # Facts read from different sources are never atomic. Measure the spread
    # between the observations themselves, again not between fetches.
    #
    # The two operands carry different epistemic weight and that is handled
    # deliberately rather than averaged away. Against a SOURCE_BLOCK holder
    # anchor this is a true source-to-source skew. Against a RESPONSE_TIME anchor
    # it bounds only how far the pinned block lags the moment of the answer, so
    # it is kept as a bound *and* the basis itself must be one the policy accepts
    # below — the weaker source is gated on assurance, not on this number.
    if snapshot.holders.observed_at is not None:
        skew = abs(snapshot.chain.block_timestamp - snapshot.holders.observed_at)
        if skew > policy.max_source_skew:
            gaps.append(AtlasReasonCode.SNAPSHOT_SKEW_EXCEEDED)
    for domain in sorted(policy.required_domains, key=lambda item: item.value):
        status = snapshot.availability(domain)
        if status == Availability.AVAILABLE:
            continue
        failure = {
            AtlasDomain.CONTRACT: snapshot.contract.failure,
            AtlasDomain.HOLDERS: snapshot.holders.failure,
            AtlasDomain.ORIGIN: snapshot.origin.failure,
        }[domain]
        not_configured = NOT_CONFIGURED_REASONS.get(domain)
        if failure is not None and failure.value == "NOT_CONFIGURED" and not_configured:
            gaps.append(not_configured)
        else:
            gaps.append(UNAVAILABLE_REASONS[domain])
    if (
        AtlasDomain.CONTRACT in policy.required_domains
        and snapshot.contract.status == Availability.AVAILABLE
        and snapshot.contract.total_supply_raw is None
    ):
        gaps.append(AtlasReasonCode.TOTAL_SUPPLY_UNKNOWN)
    if (
        AtlasDomain.HOLDERS in policy.required_domains
        and snapshot.holders.status == Availability.AVAILABLE
        and (
            snapshot.holders.completeness not in policy.accepted_holder_completeness
            or snapshot.holders.observation_basis not in policy.accepted_holder_observation_bases
            or snapshot.holders.top10_share is None
        )
    ):
        # An answer arrived, but not one the metric can be computed from. Policy
        # names the minimum facts explicitly rather than trusting a status flag.
        gaps.append(AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE)
    return gaps


def _blockers(snapshot: AtlasOnchainSnapshot, policy: AtlasPolicy) -> list[AtlasReasonCode]:
    """Only facts that were actually established can block. Absence is a gap."""
    blockers: list[AtlasReasonCode] = []
    expected = policy.expected_chain_ids.get(snapshot.chain.chain)
    if expected is None or snapshot.chain.chain_id != expected:
        # An unrecognised or mismatched chain is a hard stop: address equality
        # means nothing across chains.
        blockers.append(AtlasReasonCode.CHAIN_ID_MISMATCH)
    contract = snapshot.contract
    if contract.status == Availability.AVAILABLE:
        if contract.code_present is False:
            blockers.append(AtlasReasonCode.CONTRACT_CODE_ABSENT)
        if contract.total_supply_raw == 0:
            # An available zero supply is a measured fact, distinct from unknown.
            blockers.append(AtlasReasonCode.TOTAL_SUPPLY_ZERO)
        if (
            policy.block_on_proxy_admin
            and contract.proxy == ProxyObservation.EIP1967_DETECTED
            and contract.admin_address is not None
        ):
            blockers.append(AtlasReasonCode.PROXY_ADMIN_PRESENT)
    holders = snapshot.holders
    if (
        policy.max_top10_concentration is not None
        and holders.status == Availability.AVAILABLE
        and holders.top10_share is not None
        and holders.top10_share > policy.max_top10_concentration
    ):
        blockers.append(AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED)
    return blockers


def evaluate_snapshot(
    snapshot: AtlasOnchainSnapshot, now: datetime, policy: AtlasPolicy = ATLAS_POLICY_V2
) -> AtlasSafetyDecision:
    """Derive the authoritative safety verdict. No model input participates.

    A known violation outranks a missing fact: both stop the case, but "we
    measured this and it is dangerous" is the more precise statement and is
    reported as such, with any gaps recorded alongside.

    CLEAR means every required fact was established, fresh and internally
    consistent, and no configured deterministic blocker fired. It is not a claim
    that the token is economically safe, and no threshold that is switched off
    can be read as one that passed.
    """
    blockers = _blockers(snapshot, policy)
    gaps = _data_gaps(snapshot, now, policy)
    if blockers:
        verdict = AtlasVerdict.BLOCKED
    elif gaps:
        verdict = AtlasVerdict.INSUFFICIENT_DATA
    else:
        verdict = AtlasVerdict.CLEAR
    return AtlasSafetyDecision(
        verdict=verdict,
        policy_version=policy.version,
        blockers=tuple(dict.fromkeys(blockers)),
        data_gaps=tuple(dict.fromkeys(gaps)),
        domain_status={domain.value: snapshot.availability(domain).value for domain in AtlasDomain},
    )
