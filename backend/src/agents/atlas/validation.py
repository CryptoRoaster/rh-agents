"""Semantic validation of ATLAS model output against the facts it was given.

Model output is untrusted external input. The verdict never depends on it, but
an advisory finding that invents an address or contradicts an availability would
still pollute the audit record, so it is refused.
"""

from src.agents.atlas.context import AtlasTaskInput
from src.agents.atlas.models import AtlasAssessment, AtlasReasonCode
from src.agents.atlas.policy import AtlasPolicy
from src.markets.models import Availability


class AtlasValidationError(Exception):
    """A safe reason code for output that strays from its own input."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


# Which acknowledged gap corresponds to which domain actually being unavailable.
GAP_REQUIRES_UNAVAILABLE = {
    AtlasReasonCode.HOLDER_FACTS_UNAVAILABLE: "holders",
    AtlasReasonCode.HOLDER_SOURCE_NOT_CONFIGURED: "holders",
    AtlasReasonCode.ORIGIN_FACTS_UNAVAILABLE: "origin",
    AtlasReasonCode.ORIGIN_SOURCE_NOT_CONFIGURED: "origin",
    AtlasReasonCode.CONTRACT_FACTS_UNAVAILABLE: "contract",
}


def validate_assessment(
    assessment: AtlasAssessment, task_input: AtlasTaskInput, policy: AtlasPolicy
) -> None:
    snapshot = task_input.snapshot
    known = snapshot.addresses
    for finding in assessment.findings:
        for address in finding.referenced_addresses:
            if address not in known:
                # A model may only discuss addresses it was actually shown.
                raise AtlasValidationError("UNKNOWN_ADDRESS_REFERENCE")
    for gap in assessment.acknowledged_data_gaps:
        domain = GAP_REQUIRES_UNAVAILABLE.get(gap)
        if domain is None:
            continue
        status = {
            "holders": snapshot.holders.status,
            "origin": snapshot.origin.status,
            "contract": snapshot.contract.status,
        }[domain]
        if status == Availability.AVAILABLE:
            # Claiming a gap that does not exist misrepresents the record just as
            # much as hiding one that does.
            raise AtlasValidationError("CONTRADICTED_AVAILABILITY")
