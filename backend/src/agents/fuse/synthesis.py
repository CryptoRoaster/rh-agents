"""The deterministic synthesis. No model, no score, no vote.

Given the bounded views the context assembled, this derives four lists and one
disposition. It is a pure function: the same evidence always yields the same
synthesis, which is what makes the output fingerprint meaningful and what lets
a reader reproduce a conclusion rather than trust it.

The ordering of the derivation is the argument of the whole phase.

1. **Gaps first.** Something required that could not be read leaves the question
   unanswered, and an unanswered question is not a negative answer.
2. **Then blockers.** A source that measured something bad says so, and nothing
   downstream of this point can remove what it said.
3. **Only then factors.** Support and caution are observations about evidence
   that is already admissible. They never accumulate into a verdict, because
   there is no arithmetic here to accumulate with.

Step three cannot influence steps one and two. That is not a convention — the
disposition is computed from the first two lists before any factor is consulted,
and the contract refuses a synthesis whose disposition disagrees with them.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from functools import partial

from src.agents.fuse.models import (
    BlockerOrigin,
    EvidenceSynthesis,
    FuseDisposition,
    FuseReasonCode,
    FuseSourceEvidence,
    FuseTaskInput,
    GapOrigin,
    HardBlocker,
    OnchainView,
    SentimentView,
    SourceReference,
    SynthesisFactor,
    TradeSetupView,
    UnresolvedGap,
)
from src.agents.fuse.policy import (
    CONCERNING_MANIPULATION,
    DEGRADED_SENTIMENT_QUALITY,
    FUSE_SYNTHESIS_V1,
    NEGATIVE_DISCOVERY,
    ONCHAIN_AXIS_LABELS,
    POSITIVE_DISCOVERY,
    THIN_BREADTH,
    THIN_HISTORY_BARS,
    FuseSynthesisPolicy,
)
from src.orchestration.workflow.models import EvidenceAcceptance

Factor = Callable[[str, str], SynthesisFactor]

GAP_STATEMENTS: dict[GapOrigin, str] = {
    GapOrigin.MISSING: "has produced no current evidence",
    GapOrigin.NOT_AVAILABLE: "could not establish its finding",
    GapOrigin.STALE: "produced evidence that is no longer current",
    GapOrigin.INSUFFICIENT: "produced evidence that does not settle its question",
}


def _gaps(task_input: FuseTaskInput) -> tuple[UnresolvedGap, ...]:
    """What is required and could not be used.

    Every one of these is a reason the case cannot be judged rather than a
    finding about the asset. An UNKNOWN holder count says nothing bad about a
    token; it says we do not know, and the system's standing rule is that not
    knowing fails closed.
    """
    gaps: list[UnresolvedGap] = []
    for item in task_input.missing:
        detail = f" ({item.detail})" if item.detail else ""
        gaps.append(
            UnresolvedGap(
                code=f"{item.role.value}_{item.origin.value}",
                role=item.role,
                evidence_type=item.evidence_type,
                origin=item.origin,
                safety_critical=item.safety_critical,
                evidence_id=item.evidence_id,
                statement=f"{item.role.value} {GAP_STATEMENTS[item.origin]}{detail}.",
            )
        )
    for source in task_input.sources:
        reference = source.reference
        if reference.acceptance != EvidenceAcceptance.INSUFFICIENT:
            continue
        gaps.append(
            UnresolvedGap(
                code=f"{reference.role.value}_INSUFFICIENT",
                role=reference.role,
                evidence_type=reference.evidence_type,
                origin=GapOrigin.INSUFFICIENT,
                safety_critical=reference.safety_critical,
                evidence_id=reference.evidence_id,
                statement=(f"{reference.role.value} {GAP_STATEMENTS[GapOrigin.INSUFFICIENT]}."),
            )
        )
    return tuple(gaps)


def _blockers(task_input: FuseTaskInput) -> tuple[HardBlocker, ...]:
    """What one or more sources measured and found disqualifying.

    Derived only from a source's own committed verdict: its acceptance, or a
    blocking code it published itself. Nothing here forms an opinion about
    whether a finding should block — the specialist already decided that, and
    re-deciding it would be a second authority over the same question.
    """
    blockers: list[HardBlocker] = []
    for source in task_input.sources:
        reference = source.reference
        if reference.acceptance == EvidenceAcceptance.BLOCKED:
            blockers.append(
                HardBlocker(
                    code=f"{reference.role.value}_EVIDENCE_BLOCKED",
                    role=reference.role,
                    evidence_type=reference.evidence_type,
                    evidence_id=reference.evidence_id,
                    origin=BlockerOrigin.SOURCE_ACCEPTANCE,
                    statement=(f"{reference.role.value} recorded a finding that blocks this case."),
                )
            )
        if source.onchain is not None:
            blockers.extend(_onchain_blockers(source, source.onchain))
    return tuple(blockers)


def _onchain_blockers(source: FuseSourceEvidence, view: OnchainView) -> tuple[HardBlocker, ...]:
    """ATLAS's own findings, named individually so the reason survives.

    A case blocked for holder concentration and a case blocked for a mutable
    contract are blocked for different reasons, and collapsing both into
    "ATLAS blocked" would lose the only part anybody acts on.
    """
    reference = source.reference
    found: list[HardBlocker] = []
    for field, label in ONCHAIN_AXIS_LABELS.items():
        if getattr(view, field) != "FAIL":
            continue
        found.append(
            HardBlocker(
                code=f"ATLAS_{field.upper()}_FAIL",
                role=reference.role,
                evidence_type=reference.evidence_type,
                evidence_id=reference.evidence_id,
                origin=BlockerOrigin.SOURCE_VERDICT,
                statement=f"ATLAS measured a failure of {label}.",
            )
        )
    for code in view.blockers:
        found.append(
            HardBlocker(
                code=f"ATLAS_{code}"[:80],
                role=reference.role,
                evidence_type=reference.evidence_type,
                evidence_id=reference.evidence_id,
                origin=BlockerOrigin.SOURCE_VERDICT,
                statement=f"ATLAS published the blocking finding {code}.",
            )
        )
    return tuple(found)


def _factor(reference: SourceReference, code: str, statement: str) -> SynthesisFactor:
    """One attributed observation. Always about one source, never about a total."""
    return SynthesisFactor(
        code=code,
        role=reference.role,
        evidence_type=reference.evidence_type,
        evidence_id=reference.evidence_id,
        statement=statement,
    )


def _factors(
    task_input: FuseTaskInput,
) -> tuple[tuple[SynthesisFactor, ...], tuple[SynthesisFactor, ...]]:
    """Everything worth saying about admissible evidence, in two lists.

    This is the only genuinely synthetic part, and it is still not a judgement:
    each factor is attributed to one source and states one thing that source
    said. Two lists rather than one signed scale, because a caution and a
    support are not opposite ends of anything — a case can hold both at once and
    frequently does, and averaging them would erase exactly the tension that
    made them worth recording.
    """
    support: list[SynthesisFactor] = []
    caution: list[SynthesisFactor] = []

    for source in task_input.sources:
        reference = source.reference
        factor = partial(_factor, reference)

        if (view := source.discovery) is not None:
            if view.classification in POSITIVE_DISCOVERY:
                support.append(
                    factor(
                        "DISCOVERY_INTEREST",
                        f"ORBIT classified the candidate {view.classification}"
                        + (f" at {view.strength} strength." if view.strength else "."),
                    )
                )
            elif view.classification in NEGATIVE_DISCOVERY:
                caution.append(
                    factor(
                        "DISCOVERY_WEAK",
                        f"ORBIT classified the candidate {view.classification}.",
                    )
                )
            if view.data_gaps:
                caution.append(
                    factor(
                        "DISCOVERY_DATA_GAPS",
                        "ORBIT reached its finding with gaps: "
                        + ", ".join(view.data_gaps[:4])
                        + ".",
                    )
                )

        if (onchain := source.onchain) is not None:
            support.extend(_onchain_support(onchain, factor))
            caution.extend(_onchain_caution(onchain, factor))

        if (sentiment := source.sentiment) is not None:
            support.extend(_sentiment_support(sentiment, factor))
            caution.extend(_sentiment_caution(sentiment, factor))

        if (setup := source.trade_setup) is not None:
            caution.extend(_setup_caution(setup, factor))

    return tuple(support), tuple(caution)


def _onchain_support(view: OnchainView, factor: Factor) -> list[SynthesisFactor]:
    passing = [
        label for field, label in ONCHAIN_AXIS_LABELS.items() if getattr(view, field) == "PASS"
    ]
    if len(passing) != len(ONCHAIN_AXIS_LABELS):
        return []
    return [factor("ONCHAIN_CLEAR", "ATLAS found no integrity failure on any axis.")]


def _onchain_caution(view: OnchainView, factor: Factor) -> list[SynthesisFactor]:
    """What admissible ATLAS evidence is still worth qualifying.

    There is deliberately no branch here for an UNKNOWN integrity axis, and its
    absence is the point: the workflow refuses to record on-chain evidence as
    AVAILABLE while any domain is unestablished, so such a source never reaches
    this function — it arrives as a gap and fails closed, which is a stronger
    outcome than a caution. A branch for it would be one that can never fire,
    and a rule that cannot fire is not a second safeguard but an untested claim
    that one exists.
    """
    found = []
    if view.data_gaps:
        found.append(
            factor(
                "ONCHAIN_DATA_GAPS",
                "ATLAS reported data gaps: " + ", ".join(view.data_gaps[:4]) + ".",
            )
        )
    return found


def _sentiment_support(view: SentimentView, factor: Factor) -> list[SynthesisFactor]:
    if view.assessment != "POSITIVE":
        return []
    detail = f" with {view.attention_level} attention" if view.attention_level else ""
    return [factor("SENTIMENT_POSITIVE", f"SIGNAL assessed sentiment POSITIVE{detail}.")]


def _sentiment_caution(view: SentimentView, factor: Factor) -> list[SynthesisFactor]:
    """Where the quality of a reading is recorded next to the reading.

    A positive assessment drawn from a concentrated campaign produces both a
    support factor and a caution factor, and that is the correct output. The
    tension is the finding; resolving it to a single neutral word would throw
    away the only thing a reader needed to know.
    """
    found = []
    if view.assessment == "NEGATIVE":
        found.append(factor("SENTIMENT_NEGATIVE", "SIGNAL assessed sentiment NEGATIVE."))
    if view.assessment == "UNKNOWN":
        found.append(factor("SENTIMENT_UNKNOWN", "SIGNAL could not establish a sentiment reading."))
    if view.data_quality in DEGRADED_SENTIMENT_QUALITY:
        found.append(
            factor(
                "SENTIMENT_QUALITY_DEGRADED",
                f"SIGNAL reported {view.data_quality} social data quality, "
                "so its assessment carries less weight than its wording suggests.",
            )
        )
    if view.manipulation_concern in CONCERNING_MANIPULATION:
        found.append(
            factor(
                "SENTIMENT_MANIPULATION_CONCERN",
                f"SIGNAL reported {view.manipulation_concern} manipulation concern.",
            )
        )
    if view.organic_breadth in THIN_BREADTH:
        found.append(
            factor(
                "SENTIMENT_BREADTH_THIN",
                f"SIGNAL reported {view.organic_breadth} organic breadth.",
            )
        )
    if view.gaps:
        found.append(
            factor(
                "SENTIMENT_GAPS",
                "SIGNAL reported gaps: " + ", ".join(view.gaps[:4]) + ".",
            )
        )
    return found


def _setup_caution(view: TradeSetupView, factor: Factor) -> list[SynthesisFactor]:
    """What is worth saying about a setup that VECTOR already judged sound.

    Nothing here re-decides validity. The setup passed VECTOR's own sufficiency
    gate or it would not exist; this records how thin the ground under it is, so
    a reader is not left to assume a full window.
    """
    found = []
    if view.history_bar_count is not None and view.history_bar_count < THIN_HISTORY_BARS:
        timeframe = f" {view.history_timeframe}" if view.history_timeframe else ""
        found.append(
            factor(
                "SETUP_HISTORY_THIN",
                f"VECTOR drew the setup from {view.history_bar_count}{timeframe} bars, "
                "fewer than a full window.",
            )
        )
    return found


def _disposition(
    blockers: tuple[HardBlocker, ...],
    gaps: tuple[UnresolvedGap, ...],
    caution: tuple[SynthesisFactor, ...],
) -> FuseDisposition:
    """The verdict, computed from the first two lists before any factor is read.

    Order is authority. A blocker outranks everything because a measured danger
    is not offset by anything observed elsewhere; a gap outranks the remaining
    two because an unanswered question is not an answer. Only once neither
    applies do the soft observations decide between COHERENT and CAUTION — and
    by then they are choosing between two readings that are both admissible,
    which is the only decision they are competent to make.
    """
    if blockers:
        return FuseDisposition.BLOCKED
    if gaps:
        return FuseDisposition.INSUFFICIENT
    if caution:
        return FuseDisposition.CAUTION
    return FuseDisposition.COHERENT


def input_digest(task_input: FuseTaskInput) -> str:
    """A canonical fingerprint of exactly what this synthesis was built from.

    Covers the case identity, the policy, and every current source by identity
    and fingerprint — plus the bounded facts actually presented, so a digest
    proves not only *which* evidence was read but *what it said*.

    Deliberately excludes the lease, the worker, the attempt and the wall clock.
    Two runs over the same evidence must produce the same digest, or the digest
    would measure when the work happened rather than what it was about.
    """
    entries = sorted(
        (
            source.reference.evidence_type.value,
            source.reference.role.value,
            str(source.reference.evidence_id),
            source.reference.submission_fingerprint,
            source.reference.status.value,
            source.reference.acceptance.value,
            source.model_dump_json(exclude={"reference"}),
        )
        for source in task_input.sources
    )
    absent = sorted(
        (item.evidence_type.value, item.role.value, item.origin.value, item.detail or "")
        for item in task_input.missing
    )
    canonical = json.dumps(
        {
            "trade_case_id": str(task_input.trade_case_id),
            "workflow_version": task_input.workflow_version,
            "policy_version": task_input.policy_version,
            "sources": [
                {
                    "evidence_type": entry[0],
                    "producer_role": entry[1],
                    "evidence_id": entry[2],
                    "submission_fingerprint": entry[3],
                    "status": entry[4],
                    "acceptance": entry[5],
                    "view": entry[6],
                }
                for entry in entries
            ],
            "missing": [
                {
                    "evidence_type": entry[0],
                    "producer_role": entry[1],
                    "origin": entry[2],
                    "detail": entry[3],
                }
                for entry in absent
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def synthesize(
    task_input: FuseTaskInput,
    now: datetime,
    policy: FuseSynthesisPolicy = FUSE_SYNTHESIS_V1,
) -> EvidenceSynthesis | FuseReasonCode:
    """Read the admissible evidence and say what it adds up to.

    Returns a reason code rather than a synthesis when there is nothing
    defensible to record — which is a different outcome from a synthesis that
    found problems. "The evidence says this case is blocked" is a finding worth
    storing; "there was nothing to read" is not.
    """
    if not task_input.sources and not policy.synthesize_over_gaps:
        return FuseReasonCode.NO_ADMISSIBLE_EVIDENCE
    if not task_input.sources:
        # Every source is missing. There is no evidence to synthesize and the
        # workflow's own required-evidence blockers already say so far more
        # directly than a synthesis of nothing would.
        return FuseReasonCode.NO_ADMISSIBLE_EVIDENCE

    gaps = _gaps(task_input)
    blockers = _blockers(task_input)
    support, caution = _factors(task_input)
    disposition = _disposition(blockers, gaps, caution)

    # Freshness is composed from the sources, never from this moment. A
    # synthesis written now over evidence that expires in a minute expires in a
    # minute; re-running it cannot buy the underlying facts more time, and if it
    # could, a summariser would be able to launder stale evidence into fresh
    # evidence simply by running again.
    observed_at = min(source.reference.observed_at for source in task_input.sources)
    valid_until = min(source.reference.valid_until for source in task_input.sources)
    if valid_until <= observed_at:
        return FuseReasonCode.SOURCES_NOT_CONCURRENT

    return EvidenceSynthesis(
        policy_version=policy.version,
        disposition=disposition,
        hard_blockers=blockers,
        unresolved_gaps=gaps,
        support_factors=support,
        caution_factors=caution,
        sources=tuple(source.reference for source in task_input.sources),
        observed_at=observed_at,
        valid_until=valid_until,
        evaluated_at=now,
        input_digest=input_digest(task_input),
    )
