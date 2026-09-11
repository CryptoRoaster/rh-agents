"""Model output is untrusted input. These prove it cannot contradict what it saw."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.agents.orbit.models import OrbitAssessment, OrbitClassification, OrbitReasonCode
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from src.markets.models import Availability
from tests.orbit.conftest import assessment_for, reader_for, unknown_snapshot, valued_snapshot


async def build(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    return await reader.candidate_context(reader.cases.trade_case.id, uuid4())


async def test_consistent_assessment_passes(task_input):
    validate_assessment(assessment_for(task_input), task_input)


# ------------------------------------------------------- hallucinated references


async def test_citing_an_observation_it_was_not_given_is_rejected(task_input):
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(assessment_for(task_input, cited=(uuid4(),)), task_input)
    assert caught.value.reason_code == "UNKNOWN_OBSERVATION_REFERENCE"


async def test_every_supplied_observation_id_is_citable(task_input):
    candidate = task_input.candidate
    for identifier in candidate.observation_ids:
        validate_assessment(assessment_for(task_input, cited=(identifier,)), task_input)


async def test_mixing_one_real_and_one_invented_reference_is_rejected(task_input):
    with pytest.raises(OrbitValidationError):
        validate_assessment(
            assessment_for(task_input, cited=(task_input.candidate.snapshot_id, uuid4())),
            task_input,
        )


# ------------------------------------------------------------------ wrong market


async def test_another_market_or_chain_is_rejected(task_input):
    for kwargs, expected in (
        ({"pair_id": "ethereum:mainnet:some-other-pair"}, "MARKET_MISMATCH"),
        ({"chain": "bsc"}, "CHAIN_MISMATCH"),
    ):
        with pytest.raises(OrbitValidationError) as caught:
            validate_assessment(assessment_for(task_input, **kwargs), task_input)
        assert caught.value.reason_code == expected


# ------------------------------------------------------- fabricated availability


@pytest.mark.parametrize(
    "field,claim",
    [
        ("price", OrbitReasonCode.PRICE_AVAILABLE),
        ("liquidity", OrbitReasonCode.LIQUIDITY_PRESENT),
        ("liquidity", OrbitReasonCode.LIQUIDITY_ZERO),
        ("liquidity", OrbitReasonCode.LIQUIDITY_BELOW_DISCOVERY_FLOOR),
        ("volume", OrbitReasonCode.VOLUME_PRESENT),
        ("volume", OrbitReasonCode.VOLUME_ZERO),
    ],
)
async def test_claiming_a_value_for_an_unobserved_measurement_is_rejected(
    snapshot, now, trace, field, claim
):
    task_input = await build(unknown_snapshot(snapshot, field), now, trace)
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(assessment_for(task_input, reason_codes=(claim,)), task_input)
    assert caught.value.reason_code == "FABRICATED_AVAILABILITY"


@pytest.mark.parametrize(
    "field,claim",
    [
        ("price", OrbitReasonCode.PRICE_UNKNOWN),
        ("liquidity", OrbitReasonCode.LIQUIDITY_UNKNOWN),
        ("volume", OrbitReasonCode.VOLUME_UNKNOWN),
    ],
)
async def test_claiming_an_observed_measurement_is_missing_is_rejected(task_input, field, claim):
    assert getattr(task_input.candidate, field).status == Availability.AVAILABLE
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(
            assessment_for(
                task_input, reason_codes=(OrbitReasonCode.PRICE_AVAILABLE,), data_gaps=(claim,)
            ),
            task_input,
        )
    assert caught.value.reason_code == "CONTRADICTED_AVAILABILITY"


async def test_unknown_and_unavailable_are_not_interchangeable(snapshot, now, trace):
    task_input = await build(
        unknown_snapshot(snapshot, "liquidity", Availability.UNAVAILABLE), now, trace
    )
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(
            assessment_for(
                task_input,
                reason_codes=(OrbitReasonCode.PRICE_AVAILABLE,),
                data_gaps=(OrbitReasonCode.LIQUIDITY_UNKNOWN,),
            ),
            task_input,
        )
    assert caught.value.reason_code == "CONTRADICTED_AVAILABILITY"


# ----------------------------------------------------------- contradicted values


async def test_zero_claim_requires_an_actual_zero(snapshot, now, trace):
    nonzero = await build(valued_snapshot(snapshot, "liquidity", Decimal("5")), now, trace)
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(
            assessment_for(nonzero, reason_codes=(OrbitReasonCode.LIQUIDITY_ZERO,)), nonzero
        )
    assert caught.value.reason_code == "CONTRADICTED_VALUE"

    zero = await build(valued_snapshot(snapshot, "liquidity", Decimal("0")), now, trace)
    validate_assessment(assessment_for(zero, reason_codes=(OrbitReasonCode.LIQUIDITY_ZERO,)), zero)


async def test_below_floor_claim_is_checked_against_the_actual_floor(snapshot, now, trace):
    above = await build(valued_snapshot(snapshot, "liquidity", Decimal("50000")), now, trace)
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(
            assessment_for(above, reason_codes=(OrbitReasonCode.LIQUIDITY_BELOW_DISCOVERY_FLOOR,)),
            above,
        )
    assert caught.value.reason_code == "CONTRADICTED_VALUE"

    below = await build(valued_snapshot(snapshot, "liquidity", Decimal("100")), now, trace)
    validate_assessment(
        assessment_for(below, reason_codes=(OrbitReasonCode.LIQUIDITY_BELOW_DISCOVERY_FLOOR,)),
        below,
    )


async def test_fixture_provenance_cannot_be_invented(snapshot, now, trace):
    real = snapshot.model_copy(update={"is_fixture": True})
    task_input = await build(real, now, trace)
    # The fixture snapshot really is fixture data, so the claim holds here.
    validate_assessment(
        assessment_for(task_input, reason_codes=(OrbitReasonCode.FIXTURE_DATA,)), task_input
    )
    lying = task_input.model_copy(
        update={"candidate": task_input.candidate.model_copy(update={"is_fixture": False})}
    )
    with pytest.raises(OrbitValidationError) as caught:
        validate_assessment(
            assessment_for(lying, reason_codes=(OrbitReasonCode.FIXTURE_DATA,)), lying
        )
    assert caught.value.reason_code == "CONTRADICTED_PROVENANCE"


# ------------------------------------------------------------------ schema limits


def test_output_schema_bounds_text_and_rejects_unknown_fields(task_input):
    base = assessment_for(task_input).model_dump()
    for field, value in (
        ("summary", "x" * 401),
        ("summary", ""),
        ("classification", "APPROVE"),
        ("strength", "CERTAIN"),
        ("reason_codes", ()),
        ("cited_observation_ids", ()),
    ):
        with pytest.raises(ValidationError):
            OrbitAssessment.model_validate({**base, field: value})
    with pytest.raises(ValidationError):
        OrbitAssessment.model_validate({**base, "position_size_usd": "1000"})


def test_insufficient_data_must_name_its_gaps(task_input):
    base = assessment_for(task_input).model_dump()
    with pytest.raises(ValidationError):
        OrbitAssessment.model_validate(
            {**base, "classification": OrbitClassification.INSUFFICIENT_DATA.value, "data_gaps": ()}
        )
    accepted = OrbitAssessment.model_validate(
        {
            **base,
            "classification": OrbitClassification.INSUFFICIENT_DATA.value,
            "data_gaps": (OrbitReasonCode.LIQUIDITY_UNKNOWN.value,),
        }
    )
    assert accepted.classification == OrbitClassification.INSUFFICIENT_DATA


def test_a_data_gap_must_describe_a_missing_input(task_input):
    base = assessment_for(task_input).model_dump()
    with pytest.raises(ValidationError):
        OrbitAssessment.model_validate(
            {**base, "data_gaps": (OrbitReasonCode.LIQUIDITY_PRESENT.value,)}
        )


def test_output_cannot_express_a_trade_at_all():
    forbidden = {"side", "quantity", "position_size_usd", "entry_price", "slippage_bps", "approve"}
    assert forbidden.isdisjoint(set(OrbitAssessment.model_fields))
