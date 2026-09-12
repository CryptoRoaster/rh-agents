"""Semantic validation of a proposed setup against the market it claims to describe.

Schema parsing proves the shape. This proves the proposal means what its kind
says, that its numbers are about this market, and that it cites only what it was
shown.

**Nothing here repairs anything.** A proposal with its invalidation above its
entry is not reordered, a target list out of sequence is not sorted, an expiry
past the horizon is not clamped, and a level outside the envelope is not pulled
back to the edge. Every one of those would produce a setup that no one proposed
and that the audit record would attribute to the model anyway — the repair would
be invisible in exactly the place where being able to reconstruct a decision
matters most. A proposal that does not hold together is refused, and the runtime
retries.

The geometry rules are per kind rather than universal, because "invalidation
below entry" means different things for a breakout and a pullback and a single
rule would have to be wrong about one of them.
"""

from datetime import datetime
from decimal import Decimal

from src.agents.vector.models import (
    SetupKind,
    TriggerCondition,
    TriggerType,
    VectorSetupProposal,
    VectorTaskInput,
)
from src.agents.vector.policy import REQUIRED_TRIGGER, VECTOR_SETUP_V1, VectorSetupPolicy


class VectorValidationError(Exception):
    """A safe reason code for a proposal that does not hold together."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def trigger_for(proposal: VectorSetupProposal, task_input: VectorTaskInput) -> TriggerCondition:
    """The machine-evaluable condition this proposal implies.

    Derived from the geometry rather than proposed separately, so a setup cannot
    describe one thing and be watched for another. A future PULSE evaluates this
    and never reads the summary.
    """
    kind = proposal.kind
    if REQUIRED_TRIGGER[kind] == TriggerType.PRICE_IN_RANGE:
        return TriggerCondition(
            type=TriggerType.PRICE_IN_RANGE,
            zone_low=proposal.entry_low,
            zone_high=proposal.entry_high,
            valid_from=task_input.evaluated_at,
            expires_at=proposal.expires_at,
        )
    return TriggerCondition(
        type=REQUIRED_TRIGGER[kind],
        reference_price=proposal.entry_high,
        valid_from=task_input.evaluated_at,
        expires_at=proposal.expires_at,
    )


def _check_geometry(proposal: VectorSetupProposal) -> None:
    """Whether the levels mean what the setup kind says they mean."""
    if proposal.kind == SetupKind.BREAKOUT_LONG:
        if proposal.entry_low != proposal.entry_high:
            # A breakout is one level being crossed, not a band being entered.
            raise VectorValidationError("BREAKOUT_REQUIRES_A_SINGLE_LEVEL")
        reference = proposal.entry_high
    else:
        reference = proposal.entry_low
    if proposal.invalidation_price >= reference:
        # A long idea that is already wrong at the price it enters is not a
        # setup, whichever way round the numbers were meant.
        raise VectorValidationError("INVALIDATION_NOT_BELOW_ENTRY")
    # Targets are ascending and distinct by schema, so checking the lowest one is
    # checking all of them. There is deliberately no separate target-versus-
    # invalidation rule: for both long shapes the invalidation sits below the
    # entry, so a target above the entry is above the invalidation already. A
    # second rule that can never fire is not a second safeguard — it is an
    # untestable branch that would read like one.
    if proposal.targets[0] <= proposal.entry_high:
        raise VectorValidationError("TARGET_NOT_ABOVE_ENTRY")


def _check_envelope(
    proposal: VectorSetupProposal, reference: Decimal, policy: VectorSetupPolicy
) -> None:
    """Whether the numbers are plausibly about this market at all.

    A structural safeguard against a lost decimal point or an invented figure,
    not a judgement about what price is reasonable. An observed price of 1.00 and
    an entry of 1000000 is not an aggressive view; it is a proposal about a
    different asset.
    """
    low, high = policy.envelope(reference)
    levels = (
        proposal.entry_low,
        proposal.entry_high,
        proposal.invalidation_price,
        *proposal.targets,
    )
    if any(level < low or level > high for level in levels):
        raise VectorValidationError("LEVEL_OUTSIDE_PRICE_ENVELOPE")


def _check_lifetime(
    proposal: VectorSetupProposal, now: datetime, policy: VectorSetupPolicy
) -> None:
    lifetime = proposal.expires_at - now
    if lifetime < policy.min_setup_lifetime:
        raise VectorValidationError("SETUP_LIFETIME_TOO_SHORT")
    if lifetime > policy.max_setup_lifetime:
        # Not clamped to the maximum: a setup that wanted to live for a week is
        # not the same proposal as one bounded to four hours.
        raise VectorValidationError("SETUP_LIFETIME_TOO_LONG")


def validate_proposal(
    proposal: VectorSetupProposal,
    task_input: VectorTaskInput,
    policy: VectorSetupPolicy = VECTOR_SETUP_V1,
) -> None:
    """Reject a proposal that strays from its kind, its market or its inputs."""
    if proposal.side not in policy.supported_sides:
        raise VectorValidationError("UNSUPPORTED_SIDE")
    if proposal.kind not in policy.supported_kinds:
        raise VectorValidationError("UNSUPPORTED_SETUP_KIND")
    if len(proposal.targets) > policy.max_targets:
        raise VectorValidationError("TOO_MANY_TARGETS")

    if not set(proposal.cited_observation_ids) <= task_input.market.observation_ids:
        # A model may cite only observations it was actually shown. Anything
        # else is invented market data, however plausible the identifier looks.
        raise VectorValidationError("UNKNOWN_OBSERVATION_REFERENCE")
    if not set(proposal.cited_evidence_ids) <= task_input.evidence_ids:
        raise VectorValidationError("UNKNOWN_EVIDENCE_REFERENCE")

    _check_geometry(proposal)
    _check_envelope(proposal, task_input.latest_price, policy)
    _check_lifetime(proposal, task_input.evaluated_at, policy)
