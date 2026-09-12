"""Deterministic structure of a social observation set. No model, no network.

Everything here is arithmetic over normalized observations, which is the point:
these are the figures a manipulation claim has to survive. A model can be argued
into describing a copy-paste campaign as a movement; a count of distinct authors
cannot.

Ratios are exact Decimals. No share in this module ever passes through a float.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
from uuid import UUID

from src.agents.signal.models import (
    STRONG_BINDING_BASES,
    CollectionCoverage,
    DuplicateCluster,
    MarketBindingBasis,
    ObservationKind,
    SignalObservation,
    SignalQualityFeatures,
    SignalSource,
    SignalWindow,
    SourceBreakdown,
)
from src.core.numbers import quantize

# Versioned so a later change to normalization is auditable against the hashes
# it produced. Any change to the steps below requires a new version.
CONTENT_HASH_ALGORITHM = "signal-content-v1"

# Campaigns defeat naive deduplication by appending a unique referral or tracking
# link to otherwise identical text, so links are removed before hashing. Contract
# addresses are not URLs and survive untouched, which matters: the address is
# often the only part of a post that identifies the asset at all.
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
WHITESPACE = re.compile(r"\s+")

MAX_RETAINED_CLUSTERS = 20
MAX_RETAINED_SOURCES = 10


class SignalNormalizationError(Exception):
    """Untrusted source data could not be turned into a usable observation set."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def normalize_content(text: str) -> str:
    """Conservative normalization: enough to see a copy, not enough to erase meaning.

    Unicode is folded to a canonical composition first, because a campaign can
    otherwise vary invisible forms of the same character and produce a different
    hash for identical-looking text.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return WHITESPACE.sub(" ", URL.sub(" ", folded)).strip()


def content_hash(text: str) -> str:
    return sha256(normalize_content(text).encode()).hexdigest()


def _share(part: int, whole: int) -> Decimal:
    if whole <= 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 78
        return quantize(Decimal(part) / Decimal(whole))


@dataclass(frozen=True)
class Admission:
    """Which observations may be analysed, and what was dropped on the way.

    Exclusions are counted rather than discarded silently. A set that lost ninety
    posts to ambiguous tickers is a different epistemic situation from one that
    never had them, and the gap codes downstream depend on knowing which.
    """

    admitted: tuple[SignalObservation, ...]
    received_count: int
    outside_window: int
    ambiguous: int
    unbound: int
    coverage: CollectionCoverage = CollectionCoverage.PROVIDER_RESULTS_EXHAUSTED


def admit(
    observations: tuple[SignalObservation, ...],
    *,
    window: SignalWindow,
    chain: str,
    token_address: str | None,
    admissible_bases: frozenset[MarketBindingBasis],
    verified_project_authors: frozenset[str] = frozenset(),
    coverage: CollectionCoverage = CollectionCoverage.PROVIDER_RESULTS_EXHAUSTED,
) -> Admission:
    """Keep only observations that are both current and actually about this token.

    Two independent questions. Was this published inside the window we claim to
    describe — judged on the source's own timestamp, never on when we fetched it.
    And is it bound to this market firmly enough to carry sentiment into this
    TradeCase — where a bare ticker is not, because symbols collide across chains
    and a popular name would otherwise pollute an unrelated case.

    Both strong bindings are re-checked here rather than believed, because an
    adapter asserting one is still just a label on untrusted data. An address
    binding must name this token on this chain — the same hex string elsewhere is
    a different contract. A ``VERIFIED_PROJECT_LINK`` must come from an author
    the caller supplies as belonging to the project: a post containing an
    official-looking URL, a provider that calls a link official, or a model that
    finds it convincing are none of them verification, and each would let an
    arbitrary account claim the project's voice.

    ``verified_project_authors`` holds namespaced ``author_key`` values from a
    trusted project-identity mapping. No such mapping exists in this repository
    yet, so the collector passes an empty set and every such claim is downgraded.
    """
    admitted: list[SignalObservation] = []
    seen: set[UUID] = set()
    outside_window = 0
    ambiguous = 0
    unbound = 0
    for item in observations:
        if item.observation_id in seen:
            # One post counted twice is one voice counted twice. A provider that
            # repeats an identifier has returned something we cannot interpret.
            raise SignalNormalizationError("DUPLICATE_OBSERVATION_ID")
        seen.add(item.observation_id)
        if not window.contains(item.created_at):
            outside_window += 1
            continue
        basis = _verified_basis(
            item,
            chain=chain,
            token_address=token_address,
            verified_project_authors=verified_project_authors,
        )
        if basis not in admissible_bases:
            if basis == MarketBindingBasis.AMBIGUOUS_SYMBOL:
                ambiguous += 1
            else:
                unbound += 1
            continue
        admitted.append(item)
    return Admission(
        admitted=tuple(admitted),
        received_count=len(observations),
        outside_window=outside_window,
        ambiguous=ambiguous,
        unbound=unbound,
        coverage=coverage,
    )


def _verified_basis(
    item: SignalObservation,
    *,
    chain: str,
    token_address: str | None,
    verified_project_authors: frozenset[str],
) -> MarketBindingBasis:
    """The binding we can actually stand behind, which may be weaker than claimed.

    A strong binding is a claim about identity, and identity claims are the ones
    an adapter is least entitled to make on its own. Anything that cannot be
    checked here falls to ``UNRESOLVED`` rather than being believed at the
    strength it was asserted.
    """
    if item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_EXACT:
        if token_address is None or item.binding_address != token_address:
            return MarketBindingBasis.UNRESOLVED
        if item.binding_chain != chain:
            # The same hex string on another chain is another contract entirely.
            return MarketBindingBasis.UNRESOLVED
        return MarketBindingBasis.CONTRACT_ADDRESS_EXACT
    if item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED:
        # The address still has to be this token's. What is missing is only the
        # chain, and nothing here invents one from the TradeCase — doing so would
        # make the binding prove itself.
        if token_address is None or item.binding_address != token_address:
            return MarketBindingBasis.UNRESOLVED
        return MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED
    if item.binding_basis == MarketBindingBasis.VERIFIED_PROJECT_LINK:
        if item.author_key not in verified_project_authors:
            return MarketBindingBasis.UNRESOLVED
        return MarketBindingBasis.VERIFIED_PROJECT_LINK
    return item.binding_basis


def _clusters(
    originals: tuple[SignalObservation, ...],
) -> tuple[tuple[DuplicateCluster, ...], dict[str, tuple[SignalObservation, ...]]]:
    """Group identical normalized text. Order is by size, then hash, never by arrival."""
    grouped: dict[str, list[SignalObservation]] = {}
    for item in originals:
        grouped.setdefault(content_hash(item.content), []).append(item)
    members = {
        digest: tuple(sorted(group, key=lambda item: (item.created_at, str(item.observation_id))))
        for digest, group in grouped.items()
    }
    clusters = tuple(
        DuplicateCluster(
            content_hash=digest,
            observation_count=len(group),
            author_count=len({item.author_key for item in group}),
            representative_id=group[0].observation_id,
        )
        for digest, group in sorted(members.items(), key=lambda entry: (-len(entry[1]), entry[0]))
        if len(group) >= 2
    )
    return clusters[:MAX_RETAINED_CLUSTERS], members


def _burst_count(
    clusters: tuple[DuplicateCluster, ...],
    members: dict[str, tuple[SignalObservation, ...]],
    interval: timedelta,
) -> int:
    """Duplicate observations whose whole cluster landed inside one short interval.

    Coordination leaves a timing signature that organic repetition rarely does.
    It is an indicator and nothing more: a genuinely viral phrase can also arrive
    in a rush, which is why this never becomes a verdict on its own.
    """
    total = 0
    for cluster in clusters:
        group = members[cluster.content_hash]
        if group[-1].created_at - group[0].created_at <= interval:
            total += len(group)
    return total


def compute_features(
    admission: Admission, *, window: SignalWindow, burst_interval: timedelta
) -> SignalQualityFeatures:
    """Turn an admitted set into the deterministic record the policy judges.

    Reposts and replies are counted but never treated as authored positions, and
    an author is counted once however often they posted — the two mistakes that
    would let one voice look like a crowd.

    Every identity comparison here uses ``author_key``, never the bare provider
    handle. Two platforms hand out the same identifier strings to different
    people, and merging them would both shrink the apparent crowd and inflate the
    apparent concentration — the exact pair of errors this module exists to
    prevent, arriving through the back door.
    """
    admitted = admission.admitted
    originals = tuple(item for item in admitted if item.kind == ObservationKind.ORIGINAL)
    # Resharing is an attention event, not an authored position, so concentration
    # and breadth are measured over what people actually wrote. Counting the
    # fourteen accounts that amplified one post as fourteen voices is precisely
    # how a single influencer would read as a broad conversation.
    authored = tuple(item for item in admitted if item.kind != ObservationKind.REPOST)
    clusters, members = _clusters(originals)
    duplicated = sum(cluster.observation_count for cluster in clusters)
    authors = Counter(item.author_key for item in authored)
    ranked = sorted(authors.values(), reverse=True)

    per_source: dict[SignalSource, list[SignalObservation]] = {}
    for item in admitted:
        per_source.setdefault(item.source, []).append(item)
    sources = tuple(
        SourceBreakdown(
            source=source,
            observation_count=len(group),
            unique_author_count=len({item.author_key for item in group}),
        )
        for source, group in sorted(per_source.items(), key=lambda entry: entry[0].value)
    )[:MAX_RETAINED_SOURCES]

    created = sorted(item.created_at for item in admitted)
    return SignalQualityFeatures(
        window=window,
        observation_count=len(admitted),
        unique_author_count=len({item.author_key for item in admitted}),
        unique_authoring_count=len(authors),
        original_count=len(originals),
        repost_count=sum(1 for item in admitted if item.kind == ObservationKind.REPOST),
        reply_count=sum(1 for item in admitted if item.kind == ObservationKind.REPLY),
        unique_content_count=len(members),
        duplicate_clusters=clusters,
        duplicate_share=_share(duplicated, len(originals)),
        largest_duplicate_cluster_share=_share(
            clusters[0].observation_count if clusters else 0, len(originals)
        ),
        top1_author_share=_share(ranked[0] if ranked else 0, len(authored)),
        top5_author_share=_share(sum(ranked[:5]), len(authored)),
        burst_share=_share(_burst_count(clusters, members, burst_interval), len(originals)),
        strong_binding_count=sum(
            1 for item in admitted if item.binding_basis in STRONG_BINDING_BASES
        ),
        weak_binding_count=sum(
            1 for item in admitted if item.binding_basis not in STRONG_BINDING_BASES
        ),
        excluded_ambiguous_count=admission.ambiguous,
        excluded_outside_window_count=admission.outside_window,
        excluded_unbound_count=admission.unbound,
        received_count=admission.received_count,
        coverage=admission.coverage,
        sources=sources,
        latest_observation_at=created[-1] if created else None,
        oldest_observation_at=created[0] if created else None,
        content_hash_algorithm=CONTENT_HASH_ALGORITHM,
    )
