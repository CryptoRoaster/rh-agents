"""Semantic validation of ORBIT output against the input it was actually given.

Model output is untrusted external input, however strong the model. Schema
parsing proves the shape; this proves the content is consistent with the data
ORBIT saw. Anything inconsistent is an invalid result, never AVAILABLE evidence.
"""

from decimal import Decimal

from src.agents.orbit.models import (
    ObservedMeasurement,
    OrbitAssessment,
    OrbitReasonCode,
    OrbitTaskInput,
)
from src.markets.models import Availability


class OrbitValidationError(Exception):
    """A safe reason code for output that contradicts its own input."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


# Claims that assert a measurement was actually observed, versus claims that
# assert it was not. Asserting one while the input says the other is a fabrication.
PRESENCE_CLAIMS: dict[str, frozenset[OrbitReasonCode]] = {
    "price": frozenset({OrbitReasonCode.PRICE_AVAILABLE}),
    "liquidity": frozenset(
        {
            OrbitReasonCode.LIQUIDITY_PRESENT,
            OrbitReasonCode.LIQUIDITY_ZERO,
            OrbitReasonCode.LIQUIDITY_BELOW_DISCOVERY_FLOOR,
        }
    ),
    "volume": frozenset({OrbitReasonCode.VOLUME_PRESENT, OrbitReasonCode.VOLUME_ZERO}),
}

ABSENCE_CLAIMS: dict[str, dict[Availability, OrbitReasonCode]] = {
    "price": {
        Availability.UNKNOWN: OrbitReasonCode.PRICE_UNKNOWN,
        Availability.UNAVAILABLE: OrbitReasonCode.PRICE_UNAVAILABLE,
    },
    "liquidity": {
        Availability.UNKNOWN: OrbitReasonCode.LIQUIDITY_UNKNOWN,
        Availability.UNAVAILABLE: OrbitReasonCode.LIQUIDITY_UNAVAILABLE,
    },
    "volume": {
        Availability.UNKNOWN: OrbitReasonCode.VOLUME_UNKNOWN,
        Availability.UNAVAILABLE: OrbitReasonCode.VOLUME_UNAVAILABLE,
    },
}


def _check_measurement(
    name: str, measurement: ObservedMeasurement, claimed: frozenset[OrbitReasonCode]
) -> None:
    available = measurement.status == Availability.AVAILABLE
    if not available and claimed & PRESENCE_CLAIMS[name]:
        # Reporting a value for something never observed is the exact failure the
        # UNKNOWN semantics exist to prevent.
        raise OrbitValidationError("FABRICATED_AVAILABILITY")
    if available and claimed & frozenset(ABSENCE_CLAIMS[name].values()):
        raise OrbitValidationError("CONTRADICTED_AVAILABILITY")
    for status, code in ABSENCE_CLAIMS[name].items():
        if code in claimed and measurement.status != status:
            raise OrbitValidationError("CONTRADICTED_AVAILABILITY")


def validate_assessment(assessment: OrbitAssessment, task_input: OrbitTaskInput) -> None:
    """Reject output that strays from the market, the data or the observations given."""
    candidate = task_input.candidate
    if assessment.pair_id != candidate.pair_id:
        raise OrbitValidationError("MARKET_MISMATCH")
    if assessment.chain != candidate.chain:
        raise OrbitValidationError("CHAIN_MISMATCH")
    if not set(assessment.cited_observation_ids) <= candidate.observation_ids:
        # A model may only cite identifiers it was actually shown.
        raise OrbitValidationError("UNKNOWN_OBSERVATION_REFERENCE")

    claimed = frozenset(assessment.reason_codes) | frozenset(assessment.data_gaps)
    for name, measurement in (
        ("price", candidate.price),
        ("liquidity", candidate.liquidity),
        ("volume", candidate.volume),
    ):
        _check_measurement(name, measurement, claimed)

    liquidity = candidate.liquidity.value_usd
    if OrbitReasonCode.LIQUIDITY_ZERO in claimed and liquidity != Decimal(0):
        raise OrbitValidationError("CONTRADICTED_VALUE")
    if OrbitReasonCode.LIQUIDITY_BELOW_DISCOVERY_FLOOR in claimed and (
        liquidity is None or liquidity >= task_input.discovery_liquidity_floor_usd
    ):
        raise OrbitValidationError("CONTRADICTED_VALUE")
    volume = candidate.volume.value_usd
    if OrbitReasonCode.VOLUME_ZERO in claimed and volume != Decimal(0):
        raise OrbitValidationError("CONTRADICTED_VALUE")
    if OrbitReasonCode.FIXTURE_DATA in claimed and not candidate.is_fixture:
        raise OrbitValidationError("CONTRADICTED_PROVENANCE")
