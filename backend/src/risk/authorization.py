"""Deterministic workflow-facing interpretation of a SENTINEL RiskDecision.

Phase 0 ``RiskOutcome`` stays the authoritative SENTINEL verdict and is never
widened here. This module derives the single workflow-facing classification of
an already-final decision. It holds no clock, no I/O and no LLM dependency, and
it fails closed on anything it does not explicitly allow.

``LIMITED`` never rewrites history into an approval. A Phase 0 ``REJECT`` means
the requested intent was rejected. ``LIMITED`` states only that the rejection
was exclusively a resizable sizing rejection and that the decision carries a
strictly positive bounded capacity under which a resized request may be
reconsidered, subject to mandatory revalidation at the execution boundary.
"""

from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum

from src.core.models import RiskDecision, RiskOutcome


class RiskAuthorization(StrEnum):
    """Workflow-facing reading of a deterministic SENTINEL decision."""

    APPROVED = "APPROVED"
    LIMITED = "LIMITED"
    REJECTED = "REJECTED"


# The only Phase 0 reason codes that describe a purely arithmetic budget
# shortfall against the requested BUY size, where src.risk.engine also computes
# a conservative smaller capacity. Every other emitted code names a safety,
# freshness, integrity, mode, accounting, liquidity or pause condition that
# resizing cannot cure. INSUFFICIENT_POSITION is deliberately absent: it is the
# SELL-side shortfall, for which Phase 0 never computes additional capacity.
RESIZABLE_SIZING_REASON_CODES = frozenset(
    {
        "INSUFFICIENT_CASH",
        "MAX_EXPOSURE",
        "MAX_POSITION_SIZE",
    }
)


def classify_risk_authorization(
    outcome: RiskOutcome,
    reason_codes: Iterable[str],
    *,
    position_size_limit_usd: Decimal,
    max_additional_notional_usd: Decimal,
) -> RiskAuthorization:
    """Classify a final SENTINEL decision. Anything unproven becomes REJECTED."""
    match outcome:
        case RiskOutcome.APPROVE:
            return RiskAuthorization.APPROVED
        case RiskOutcome.PAUSE_SYSTEM:
            # A pause is a system-wide stop. No sizing field may soften it.
            return RiskAuthorization.REJECTED
        case RiskOutcome.REJECT:
            pass
        case _:
            # An outcome added later is unknown here and cannot authorize.
            return RiskAuthorization.REJECTED
    codes = frozenset(reason_codes)
    if not codes or not codes <= RESIZABLE_SIZING_REASON_CODES:
        return RiskAuthorization.REJECTED
    if max_additional_notional_usd <= 0 or position_size_limit_usd <= 0:
        return RiskAuthorization.REJECTED
    return RiskAuthorization.LIMITED


def classify_decision(decision: RiskDecision) -> RiskAuthorization:
    return classify_risk_authorization(
        decision.outcome,
        decision.reason_codes,
        position_size_limit_usd=decision.position_size_limit_usd,
        max_additional_notional_usd=decision.max_additional_notional_usd,
    )
