"""Assembly of the SIGNAL view: admission, structure, sampling and the digest.

No model participates here. The reader fixes the window from policy, asks the
configured source once, drops what is stale or not about this token, computes the
deterministic structure and draws a bounded representative sample. Only then is
anything shown to a model.

The sample matters as much as the metrics. Thousands of posts cannot go into a
prompt, and *which* few do is a decision with a bias attached: ranking by
engagement would hand the reading to whoever is loudest, which is precisely the
input a promotional campaign manufactures. Selection here is deterministic and
diversity-first instead.
"""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from src.agents.signal.models import (
    CollectionCoverage,
    ObservationCollection,
    ObservationKind,
    SignalObservation,
    SignalQualityFeatures,
    SignalRepresentative,
    SignalStructuralAssessment,
    SignalTaskInput,
    SignalWindow,
)
from src.agents.signal.policy import SIGNAL_QUALITY_V1, SignalQualityPolicy, assess_structure
from src.agents.signal.ports import SignalObservationReadPort, SignalSourceUnavailable
from src.agents.signal.quality import admit, compute_features, content_hash
from src.core.clock import Clock, SystemClock
from src.core.numbers import canonical_decimal
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import EvidenceEnvelope, EvidenceType, TradeCase

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Reposts carry no text of their own, so they are attention rather than an
# authored position and never enter the sample a sentiment reading is drawn from.
SAMPLED_KINDS = frozenset({ObservationKind.ORIGINAL, ObservationKind.REPLY})

SelectionReason = Literal["DUPLICATE_CLUSTER", "AUTHOR_DIVERSITY", "RECENCY_FILL"]


class TradeCaseIdentitySource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


def token_address_of(base_asset_id: str) -> str | None:
    """The base token's contract address when the identifier carries a usable one.

    Absence is tolerated rather than fatal: without an address no observation can
    reach the strongest binding, which the structural assessment already records
    as a gap. Coercing a bytes32 pool id into an address to avoid that would be
    far worse than the gap.
    """
    candidate = base_asset_id.rsplit(":", 1)[-1]
    if ADDRESS.fullmatch(candidate) is None:
        return None
    if int(candidate[2:], 16) == 0:
        return None
    return candidate.lower()


def sample(
    observations: tuple[SignalObservation, ...],
    features: SignalQualityFeatures,
    limit: int,
) -> tuple[SignalRepresentative, ...]:
    """A deterministic, diversity-first representative set.

    Three passes, in this order and for this reason. Duplicate clusters come
    first, because a campaign's text is the single most informative thing about
    the set and one copy of it says everything a hundred would. Then one
    observation from each author not yet represented, so the sample widens across
    people rather than deepening on the loudest. Then recency fills what is left.

    Every pass skips text the sample already contains. A slot spent on a sentence
    the model has already read carries no information, and how often it was
    repeated is already a measurement. The consequence is deliberate: a set of
    fifty posts saying one thing yields a sample of one, which is the honest size
    of what there was to read.

    Engagement is deliberately not a selection key anywhere: it would
    systematically overrepresent viral and influencer content, which is the shape
    of exactly the manipulation this worker is supposed to notice.
    """
    candidates = {item.observation_id: item for item in observations if item.kind in SAMPLED_KINDS}
    # Newest first, ties broken on the canonical identifier. Two passes over a
    # stable sort rather than a negated timestamp: a float key would be a silent
    # precision cliff on exactly the sub-second clustering a campaign produces.
    ordered = sorted(candidates.values(), key=lambda item: str(item.observation_id))
    ordered.sort(key=lambda item: item.created_at, reverse=True)
    chosen: list[SignalRepresentative] = []
    taken: set[UUID] = set()
    authors: set[str] = set()
    seen_text: set[str] = set()

    def take(item: SignalObservation, reason: SelectionReason) -> None:
        digest = content_hash(item.content)
        chosen.append(
            SignalRepresentative(
                observation_id=item.observation_id,
                source=item.source,
                author_id=item.author_id,
                author_key=item.author_key,
                kind=item.kind,
                created_at=item.created_at,
                content_hash=digest,
                binding_basis=item.binding_basis,
                selection_reason=reason,
            )
        )
        taken.add(item.observation_id)
        authors.add(item.author_key)
        seen_text.add(digest)

    for cluster in features.duplicate_clusters:
        if len(chosen) >= limit:
            break
        item = candidates.get(cluster.representative_id)
        if item is not None and item.observation_id not in taken:
            take(item, "DUPLICATE_CLUSTER")
    for item in ordered:
        if len(chosen) >= limit:
            break
        if (
            item.observation_id not in taken
            and item.author_key not in authors
            and content_hash(item.content) not in seen_text
        ):
            take(item, "AUTHOR_DIVERSITY")
    for item in ordered:
        if len(chosen) >= limit:
            break
        if item.observation_id not in taken and content_hash(item.content) not in seen_text:
            take(item, "RECENCY_FILL")
    return tuple(chosen)


@dataclass(frozen=True)
class SignalContextReader:
    """Builds the SIGNAL view from the configured source. Contains no reasoning."""

    cases: TradeCaseIdentitySource
    source: SignalObservationReadPort
    policy: SignalQualityPolicy = SIGNAL_QUALITY_V1
    max_observations: int = 500
    max_model_observations: int = 25
    # Namespaced author keys a trusted project-identity mapping says belong to
    # this project. Empty by default and empty in practice: no such registry
    # exists yet, so no adapter can assert the project's own voice. Building one
    # is a deliberate future decision, not something a provider label supplies.
    verified_project_authors: frozenset[str] = frozenset()
    clock: Clock = SystemClock()

    async def sentiment_context(self, trade_case_id: UUID, task_id: UUID) -> SignalTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        market = trade_case.market
        now = self.clock.now()
        window = SignalWindow(start=now - self.policy.window, end=now)
        token_address = token_address_of(market.base_asset_id)
        unavailable = False
        try:
            collected = await self.source.observations(
                chain=market.chain,
                pair_id=market.pair_id,
                token_address=token_address,
                window=window,
            )
        except SignalSourceUnavailable:
            # A source that cannot answer is an explicit absence, never an empty
            # feed that could be mistaken for a quiet one.
            collected = ObservationCollection()
            unavailable = True
        observations = collected.observations[: self.max_observations]
        coverage = (
            CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET
            if len(collected.observations) > self.max_observations
            else collected.coverage
        )
        admission = admit(
            observations,
            window=window,
            chain=market.chain,
            token_address=token_address,
            admissible_bases=self.policy.admissible_bases,
            verified_project_authors=self.verified_project_authors,
            coverage=coverage,
        )
        features = compute_features(
            admission, window=window, burst_interval=self.policy.burst_interval
        )
        structure = assess_structure(features, self.policy, source_unavailable=unavailable)
        representatives = sample(admission.admitted, features, self.max_model_observations)
        excerpts = {item.observation_id: item.content for item in admission.admitted}
        current = active_evidence(await self.cases.evidence(trade_case_id))
        existing = current.get(EvidenceType.SENTIMENT)
        return SignalTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            chain=market.chain,
            network=market.network,
            pair_id=market.pair_id,
            base_asset_id=market.base_asset_id,
            token_address=token_address,
            features=features,
            structure=structure,
            representatives=representatives,
            excerpts=tuple(excerpts[item.observation_id] for item in representatives),
            evaluated_at=now,
            supersedes_evidence_id=existing.evidence_id if existing is not None else None,
        )


def _features_document(features: SignalQualityFeatures) -> dict[str, object]:
    """Deterministic metrics, with decimals in a canonical textual form.

    Window bounds are absent on purpose. They move with the clock on every pass,
    so including them would make one unchanged set of posts fingerprint
    differently every time it is read. The window's *duration* is policy and is
    recorded beside it; the observations themselves are what identify the input.
    """
    return {
        "observation_count": features.observation_count,
        "unique_author_count": features.unique_author_count,
        "unique_authoring_count": features.unique_authoring_count,
        "original_count": features.original_count,
        "repost_count": features.repost_count,
        "reply_count": features.reply_count,
        "unique_content_count": features.unique_content_count,
        "duplicate_share": canonical_decimal(features.duplicate_share),
        "largest_duplicate_cluster_share": canonical_decimal(
            features.largest_duplicate_cluster_share
        ),
        "top1_author_share": canonical_decimal(features.top1_author_share),
        "top5_author_share": canonical_decimal(features.top5_author_share),
        "burst_share": canonical_decimal(features.burst_share),
        "strong_binding_count": features.strong_binding_count,
        "weak_binding_count": features.weak_binding_count,
        "excluded_ambiguous_count": features.excluded_ambiguous_count,
        "excluded_outside_window_count": features.excluded_outside_window_count,
        "excluded_unbound_count": features.excluded_unbound_count,
        "received_count": features.received_count,
        "coverage": features.coverage.value,
        "source_count": features.source_count,
        "sources": [
            {
                "source": entry.source.value,
                "observation_count": entry.observation_count,
                "unique_author_count": entry.unique_author_count,
            }
            for entry in features.sources
        ],
        "duplicate_clusters": [
            {
                "content_hash": cluster.content_hash,
                "observation_count": cluster.observation_count,
                "author_count": cluster.author_count,
            }
            for cluster in features.duplicate_clusters
        ],
        # Source-created times, which are semantically part of the observation.
        "latest_observation_at": (
            None
            if features.latest_observation_at is None
            else features.latest_observation_at.isoformat()
        ),
        "oldest_observation_at": (
            None
            if features.oldest_observation_at is None
            else features.oldest_observation_at.isoformat()
        ),
        "content_hash_algorithm": features.content_hash_algorithm,
    }


def _structure_document(structure: SignalStructuralAssessment) -> dict[str, object]:
    return {
        "policy_version": structure.policy_version,
        "data_quality": structure.data_quality.value,
        "attention_level": structure.attention_level.value,
        "organic_breadth": structure.organic_breadth.value,
        "manipulation_concern": structure.manipulation_concern.value,
        "gaps": [gap.value for gap in structure.gaps],
    }


def signal_document(task_input: SignalTaskInput, window_seconds: int) -> dict[str, object]:
    """The bounded fact document behind both the digest and the prompt payload.

    Built once so the fingerprint always covers precisely what the model saw. No
    post text appears here: representatives are identified by hash, which keeps
    the digest stable, keeps third-party prose out of a persisted record, and
    still changes the moment the underlying content changes.
    """
    return {
        "chain": task_input.chain,
        "network": task_input.network,
        "pair_id": task_input.pair_id,
        "base_asset_id": task_input.base_asset_id,
        "token_address": task_input.token_address,
        "window_seconds": window_seconds,
        "metrics": _features_document(task_input.features),
        "structure": _structure_document(task_input.structure),
        "representatives": [
            {
                "observation_id": str(item.observation_id),
                "source": item.source.value,
                "author_key": item.author_key,
                "kind": item.kind.value,
                "created_at": item.created_at.isoformat(),
                "content_hash": item.content_hash,
                "binding_basis": item.binding_basis.value,
                "selection_reason": item.selection_reason,
            }
            for item in task_input.representatives
        ],
    }


def signal_input_digest(
    task_input: SignalTaskInput, policy: SignalQualityPolicy = SIGNAL_QUALITY_V1
) -> str:
    """Canonical fingerprint of exactly what SIGNAL was given.

    The same posts produce the same digest however often they are fetched, and a
    different relevant post changes it. Fetch receipts, latency and request
    identifiers are excluded deliberately: none of them is part of the input, and
    all of them would add noise that defeats the comparison.
    """
    canonical = json.dumps(
        signal_document(task_input, int(policy.window.total_seconds())),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def reasoning_payload(
    task_input: SignalTaskInput, policy: SignalQualityPolicy = SIGNAL_QUALITY_V1
) -> dict[str, object]:
    """The quoted data document handed to the provider. It contains no instructions.

    Excerpts are attached to their representatives here and nowhere else. They
    exist for the duration of one call, because reading tone requires reading
    language; they are not part of the digest and are never written to evidence.
    """
    document = signal_document(task_input, int(policy.window.total_seconds()))
    entries = document["representatives"]
    assert isinstance(entries, list)
    for entry, excerpt in zip(entries, task_input.excerpts, strict=True):
        assert isinstance(entry, dict)
        entry["text"] = excerpt
    return {"social_observations": document}
