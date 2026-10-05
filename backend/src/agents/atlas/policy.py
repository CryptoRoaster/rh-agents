"""Versioned deterministic ATLAS safety policy.

This module decides whether a candidate is safe enough to proceed. It contains
no model, reads no prompt, and nothing it produces can be argued with. Thresholds
live here as code, never in prompt text and never chosen by a model.
"""

from dataclasses import dataclass, replace
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
from src.agents.atlas.v4.models import PoolControlGap
from src.markets.models import Availability

UNAVAILABLE_REASONS: dict[AtlasDomain, AtlasReasonCode] = {
    AtlasDomain.CONTRACT: AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE,
    AtlasDomain.HOLDERS: AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE,
    AtlasDomain.ORIGIN: AtlasReasonCode.ORIGIN_FACTS_UNAVAILABLE,
}

# Every detailed pool-control cause, folded into the three stable policy codes.
POOL_CONTROL_REASONS: dict[PoolControlGap, AtlasReasonCode] = {
    PoolControlGap.DEPLOYMENT_NOT_CONFIGURED: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.DEPLOYMENT_UNVERIFIED: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.CREATION_BLOCK_UNKNOWN: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.CENSUS_UNAVAILABLE: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.CENSUS_BOUNDS_EXCEEDED: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.POOL_KEY_MISMATCH: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    PoolControlGap.MARKET_POOL_NOT_FOUND: AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE,
    # The census held; the raw distribution it is added to did not.
    PoolControlGap.HOLDER_BASIS_UNAVAILABLE: AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE,
    PoolControlGap.POSITION_FACTS_INCOMPLETE: AtlasReasonCode.V4_POSITION_FACTS_INCOMPLETE,
    PoolControlGap.POSITION_OWNER_UNKNOWN: AtlasReasonCode.V4_POSITION_FACTS_INCOMPLETE,
    PoolControlGap.POOL_BALANCE_UNATTRIBUTED: AtlasReasonCode.V4_POOL_BALANCE_UNATTRIBUTED,
    PoolControlGap.POSITION_CONTROL_UNRESOLVED: AtlasReasonCode.V4_POSITION_CONTROL_UNRESOLVED,
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
    #
    # Enabling one is not only choosing a number. A threshold must know which
    # source exclusions the metric carries and whether they were reconciled; it
    # may never assume the holder set was unfiltered. That is enforced in
    # ``_data_gaps`` rather than left to whoever sets the field.
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
    # Whether a V4 token's holder domain waits on its economic concentration.
    # It does for an entry, which is judged on concentration. A sale is not —
    # SENTINEL binds concentration to purchases only — so the read behind an
    # exit must not make a position unsellable because a pool census, which
    # only an entry needs, is missing.
    pool_control_binds: bool = True

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

# The same policy, read for a sale. Identical for every market that is not a
# V4 pool; for one that is, the economic concentration an entry waits on does
# not hold a sale hostage. Same version: no threshold differs, only which
# reasons bind the consumer.
ATLAS_EXIT_POLICY_V2 = replace(ATLAS_POLICY_V2, pool_control_binds=False)


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
    if (
        policy.max_top10_concentration is not None
        and snapshot.holders.status == Availability.AVAILABLE
        and snapshot.holders.unresolved_exclusions
    ):
        # A threshold judges a distribution, so it may only judge one that
        # actually covers it. Every exclusion a provider applies removes supply
        # from the numerator while the denominator stays full on-chain supply,
        # so the metric can only ever *understate* concentration — and an
        # understated metric silently passing a limit is the one failure mode a
        # limit exists to prevent. Only an exclusion read back on-chain at the
        # holder block is covered; every unresolved one makes the case
        # insufficient rather than clear. A blocker still fires on the same metric, because
        # exceeding a limit on an understated figure means the true figure
        # exceeds it too.
        gaps.append(AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE)
    if policy.pool_control_binds:
        gaps.extend(_pool_control_gaps(snapshot))
    return gaps


def _pool_control_gaps(snapshot: AtlasOnchainSnapshot) -> list[AtlasReasonCode]:
    """A V4 token's holder domain is unestablished until its pool control is.

    The raw top-ten of a V4 token can miss every unit sitting in a liquidity
    position, so for such a token the raw figure is not a weaker version of the
    answer but possibly the wrong one. Without a complete economic view the
    holder domain is a gap — never a pass on the raw number. Other markets keep
    exactly the holder path they had.
    """
    if not snapshot.pool_control_required:
        return []
    control = snapshot.pool_control
    if control is None:
        return [AtlasReasonCode.V4_POOL_CENSUS_UNAVAILABLE]
    if control.status == Availability.AVAILABLE:
        return []
    gap = control.gap or PoolControlGap.CENSUS_UNAVAILABLE
    return [POOL_CONTROL_REASONS[gap]]


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
        # A token read through V4 pool control is judged on its economic
        # figure below, exactly as SENTINEL judges it. Its raw top ten counts
        # the PoolManager -- every pool's reserves -- as one holder, which says
        # nothing about who controls that supply.
        and not (policy.pool_control_binds and snapshot.pool_control_required)
        and holders.status == Availability.AVAILABLE
        and holders.top10_share is not None
        and holders.top10_share > policy.max_top10_concentration
    ):
        blockers.append(AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED)
    control = snapshot.pool_control
    if (
        policy.max_top10_concentration is not None
        and policy.pool_control_binds
        and control is not None
        and control.economic_top10_share is not None
        and control.economic_top10_share > policy.max_top10_concentration
    ):
        # The economic figure never understates, so exceeding a limit on it is
        # an established violation even where the raw figure stays below.
        blockers.append(AtlasReasonCode.HOLDER_CONCENTRATION_EXCEEDED)
    if (
        policy.max_top10_concentration is not None
        and policy.pool_control_binds
        and control is not None
        and control.economic_top10_floor is not None
        and control.economic_top10_floor > policy.max_top10_concentration
    ):
        # Unresolved custody leaves the figure unknown, but never lowers it:
        # a floor already above the limit is a violation whoever controls the
        # rest. Below the limit the floor proves nothing and only the gap holds.
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
