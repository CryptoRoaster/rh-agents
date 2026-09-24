"""ORBIT evaluation suite v1, offline.

No model, no provider, no credential and no network: an autouse guard makes any
outbound socket connection fail the test. Every assessment here is hand-built to
exercise one property of the scorer or of the case matrix.
"""

import json
import socket
from collections.abc import Iterator
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from src.agents.orbit.context import reasoning_payload
from src.agents.orbit.models import (
    OrbitAssessment,
    OrbitClassification,
    OrbitReasonCode,
    OrbitStrength,
    OrbitTaskInput,
)
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS, ORBIT_PROMPT_HASH
from src.markets.models import Availability
from tests.evaluation.fixtures import orbit_candidate
from tests.evaluation.fixtures.orbit_suite import (
    DISCOVERY_FLOOR_USD,
    HOSTILE_BASE_SYMBOL,
    HOSTILE_PROVIDER,
    HOSTILE_VENUE,
    SUITE,
    OrbitSuiteCase,
    case,
    citations_for,
    measured_by,
)
from tests.evaluation.orbit_benchmark import (
    SCHEMA_INVALID,
    Verdict,
    evaluate,
    evaluate_document,
)

C = OrbitReasonCode

# The suite compares models under one prompt. A silent prompt change would make
# results from before and after incomparable, so it must be a visible failure.
ORBIT_PROMPT_SHA256 = "d823b391500c583928f9aafa0a2be829fde61cb722b7f2d3504a1b53b372615d"

SLUGS = (
    "positive_complete",
    "liquidity_below_floor",
    "liquidity_zero",
    "liquidity_unknown",
    "price_unavailable",
    "unknown_liquidity_zero_volume",
    "hostile_metadata_positive",
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the offline ORBIT suite must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def good_assessment(suite_case: OrbitSuiteCase, **overrides: Any) -> OrbitAssessment:
    """A minimal answer that meets exactly the case's expectations."""
    candidate = suite_case.task_input.candidate
    fields: dict[str, Any] = {
        "classification": suite_case.expected_classification,
        "strength": OrbitStrength.MODERATE,
        "reason_codes": tuple(sorted(suite_case.required_reason_codes)),
        "data_gaps": tuple(sorted(suite_case.expected_data_gaps)),
        "cited_observation_ids": tuple(sorted(suite_case.required_citation_ids, key=str)),
        "pair_id": candidate.pair_id,
        "chain": candidate.chain,
        "summary": "Synthetic offline test summary.",
    }
    fields.update(overrides)
    return OrbitAssessment(**fields)


def payload_text(task_input: OrbitTaskInput) -> str:
    return json.dumps(reasoning_payload(task_input), sort_keys=True)


def case_scoped_ids(suite_case: OrbitSuiteCase) -> tuple[UUID, ...]:
    task_input = suite_case.task_input
    return (
        *task_input.candidate.observation_ids,
        task_input.trade_case_id,
        task_input.task_id,
        task_input.discovery_reference,
    )


# --- Case matrix -------------------------------------------------------------


def test_suite_has_exactly_seven_cases() -> None:
    assert len(SUITE) == 7
    assert tuple(c.slug for c in SUITE) == SLUGS


def test_slugs_are_unique() -> None:
    assert len({c.slug for c in SUITE}) == len(SUITE)


def test_case_scoped_ids_are_unique_across_the_suite_and_the_probe_fixture() -> None:
    ids = [identifier for c in SUITE for identifier in case_scoped_ids(c)]
    assert len(ids) == len(set(ids)) == 7 * 7
    probe_input = orbit_candidate.task_input()
    probe_ids = {
        *probe_input.candidate.observation_ids,
        probe_input.trade_case_id,
        probe_input.task_id,
        probe_input.discovery_reference,
    }
    assert not set(ids) & probe_ids


def test_ids_are_reproducible() -> None:
    from tests.evaluation.fixtures import orbit_suite

    rebuilt = orbit_suite._task_input(1)
    assert rebuilt == case("positive_complete").task_input


def test_pair_ids_are_distinct_per_case() -> None:
    assert len({c.task_input.candidate.pair_id for c in SUITE}) == len(SUITE)


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_task_input_is_a_valid_orbit_task_input(suite_case: OrbitSuiteCase) -> None:
    rebuilt = OrbitTaskInput.model_validate(suite_case.task_input.model_dump())
    assert rebuilt == suite_case.task_input


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_every_case_is_a_fixture(suite_case: OrbitSuiteCase) -> None:
    assert suite_case.task_input.candidate.is_fixture is True


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_expected_gaps_are_exactly_the_unobserved_measurements(
    suite_case: OrbitSuiteCase,
) -> None:
    candidate = suite_case.task_input.candidate
    derived = set()
    for name in ("price", "liquidity", "volume"):
        status = getattr(candidate, name).status
        if status != Availability.AVAILABLE:
            derived.add(OrbitReasonCode(f"{name.upper()}_{status.value}"))
    assert suite_case.expected_data_gaps == derived


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_required_citations_are_the_measurements_the_expectations_name(
    suite_case: OrbitSuiteCase,
) -> None:
    candidate = suite_case.task_input.candidate
    expected = citations_for(
        suite_case.task_input, suite_case.required_reason_codes | suite_case.expected_data_gaps
    )
    assert suite_case.required_citation_ids == expected
    assert candidate.snapshot_id not in suite_case.required_citation_ids
    # Every case in v1 names all three measurements.
    assert suite_case.required_citation_ids == {
        candidate.price.observation_id,
        candidate.liquidity.observation_id,
        candidate.volume.observation_id,
    }


def test_measurement_mapping() -> None:
    assert measured_by(C.PRICE_UNAVAILABLE) == "price"
    assert measured_by(C.LIQUIDITY_BELOW_DISCOVERY_FLOOR) == "liquidity"
    assert measured_by(C.VOLUME_ZERO) == "volume"
    assert measured_by(C.FIXTURE_DATA) is None


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_expectations_are_satisfiable_under_the_production_validator(
    suite_case: OrbitSuiteCase,
) -> None:
    result = evaluate(suite_case, good_assessment(suite_case))
    assert result.domain_valid, result.domain_reason
    assert result.benchmark_pass
    assert result.verdict is Verdict.PASS


# --- Model input carries no expectation --------------------------------------


@pytest.mark.parametrize("suite_case", SUITE, ids=SLUGS)
def test_no_benchmark_expectation_reaches_the_model_payload(suite_case: OrbitSuiteCase) -> None:
    payload = reasoning_payload(suite_case.task_input)
    assert set(payload) == {"market_observation"}
    text = payload_text(suite_case.task_input)

    evaluator_only = [
        suite_case.slug,
        suite_case.description,
        suite_case.expected_classification.value,
        *(code.value for code in suite_case.required_reason_codes),
        *(code.value for code in suite_case.expected_data_gaps),
    ]
    if suite_case.notes:
        evaluator_only.append(suite_case.notes)
    for secret in evaluator_only:
        assert secret not in text
    document = payload["market_observation"]
    assert isinstance(document, dict)
    for key in document:
        for word in ("expected", "required", "benchmark", "slug", "description", "notes"):
            assert word not in key


def test_hostile_metadata_is_carried_as_data_values_only() -> None:
    hostile = case("hostile_metadata_positive")
    document = reasoning_payload(hostile.task_input)["market_observation"]
    assert isinstance(document, dict)
    assert document["base_symbol"] == HOSTILE_BASE_SYMBOL
    assert document["venue"] == HOSTILE_VENUE
    assert document["provider"] == HOSTILE_PROVIDER

    # Same measurements as the positive control; only ids and metadata differ.
    control = reasoning_payload(case("positive_complete").task_input)["market_observation"]
    assert isinstance(control, dict)
    for name in ("price", "liquidity", "volume"):
        assert document[name]["status"] == control[name]["status"]  # type: ignore[index]
        assert document[name]["value_usd"] == control[name]["value_usd"]  # type: ignore[index]
    assert document["chain"] == control["chain"]
    assert document["discovery_liquidity_floor_usd"] == control["discovery_liquidity_floor_usd"]

    # The control channel is untouched by the case.
    for text in (HOSTILE_BASE_SYMBOL, HOSTILE_VENUE, HOSTILE_PROVIDER):
        assert text not in ORBIT_INSTRUCTIONS
    assert sha256(ORBIT_INSTRUCTIONS.encode()).hexdigest() == ORBIT_PROMPT_SHA256
    assert ORBIT_PROMPT_HASH == ORBIT_PROMPT_SHA256


def test_hostile_case_scores_like_the_positive_control() -> None:
    hostile = case("hostile_metadata_positive")
    result = evaluate(hostile, good_assessment(hostile))
    assert result.benchmark_pass
    # An answer that let the metadata rename the market is a contract failure.
    renamed = good_assessment(hostile, pair_id=HOSTILE_BASE_SYMBOL)
    assert evaluate(hostile, renamed).domain_reason == "MARKET_MISMATCH"


# --- Domain validity versus benchmark quality --------------------------------


def test_positive_control_canonical_answer_passes() -> None:
    positive = case("positive_complete")
    result = evaluate(
        positive,
        good_assessment(positive, classification=OrbitClassification.INTERESTING),
    )
    assert result.domain_valid
    assert result.classification_match
    assert result.required_reason_codes_present
    assert result.data_gaps_match
    assert result.required_citations_present
    assert result.benchmark_pass


def test_below_floor_interesting_is_domain_valid_but_a_benchmark_miss() -> None:
    """The separation this suite exists for.

    The production validator does not judge classification, so INTERESTING on
    liquidity at half the discovery floor is contract-valid. It is still a poor
    answer, and the benchmark says so without touching production validation.
    """
    below = case("liquidity_below_floor")
    assert below.task_input.candidate.liquidity.value_usd is not None
    assert below.task_input.candidate.liquidity.value_usd < DISCOVERY_FLOOR_USD

    answer = good_assessment(
        below,
        classification=OrbitClassification.INTERESTING,
        strength=OrbitStrength.STRONG,
        reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT),
    )
    result = evaluate(below, answer)
    assert result.domain_valid is True
    assert result.classification_match is False
    assert result.benchmark_pass is False
    assert result.verdict is Verdict.BENCHMARK_MISS
    assert result.missing_reason_codes == {C.LIQUIDITY_BELOW_DISCOVERY_FLOOR}


def test_liquidity_unknown_without_the_gap_is_a_benchmark_miss() -> None:
    unknown = case("liquidity_unknown")
    # Classification INSUFFICIENT_DATA would force a gap by schema, so the
    # realistic miss is a confident answer that simply skips the missing input.
    answer = good_assessment(
        unknown,
        classification=OrbitClassification.NOT_INTERESTING,
        reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_PRESENT, C.LIQUIDITY_UNKNOWN),
        data_gaps=(),
    )
    result = evaluate(unknown, answer)
    assert result.domain_valid
    assert result.data_gaps_match is False
    assert result.classification_match is False
    assert result.verdict is Verdict.BENCHMARK_MISS


def test_liquidity_unknown_reported_as_unavailable_is_domain_invalid() -> None:
    unknown = case("liquidity_unknown")
    answer = good_assessment(unknown, data_gaps=(C.LIQUIDITY_UNAVAILABLE,))
    result = evaluate(unknown, answer)
    assert result.domain_valid is False
    assert result.domain_reason == "CONTRADICTED_AVAILABILITY"
    assert result.data_gaps_match is False
    assert result.verdict is Verdict.DOMAIN_INVALID


def test_price_unavailable_reported_as_unknown_is_domain_invalid() -> None:
    unavailable = case("price_unavailable")
    answer = good_assessment(unavailable, data_gaps=(C.PRICE_UNKNOWN,))
    result = evaluate(unavailable, answer)
    assert result.domain_reason == "CONTRADICTED_AVAILABILITY"
    assert result.verdict is Verdict.DOMAIN_INVALID


def test_zero_liquidity_accepts_liquidity_zero_and_the_floor_code() -> None:
    zero = case("liquidity_zero")
    only_zero = evaluate(zero, good_assessment(zero))
    assert only_zero.benchmark_pass
    with_floor = evaluate(
        zero,
        good_assessment(
            zero,
            reason_codes=(
                C.PRICE_AVAILABLE,
                C.LIQUIDITY_ZERO,
                C.LIQUIDITY_BELOW_DISCOVERY_FLOOR,
                C.VOLUME_PRESENT,
            ),
        ),
    )
    assert with_floor.benchmark_pass


def test_unknown_liquidity_with_zero_volume_accepted() -> None:
    mixed = case("unknown_liquidity_zero_volume")
    answer = good_assessment(
        mixed,
        reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_ZERO),
        data_gaps=(C.LIQUIDITY_UNKNOWN,),
    )
    result = evaluate(mixed, answer)
    assert result.domain_valid
    assert result.benchmark_pass


def test_liquidity_zero_claimed_for_unknown_liquidity_is_fabrication() -> None:
    mixed = case("unknown_liquidity_zero_volume")
    answer = good_assessment(
        mixed,
        reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_ZERO, C.LIQUIDITY_ZERO),
    )
    result = evaluate(mixed, answer)
    assert result.domain_valid is False
    assert result.domain_reason == "FABRICATED_AVAILABILITY"
    assert result.benchmark_pass is False


def test_snapshot_only_citation_is_domain_valid_but_misses_citations() -> None:
    positive = case("positive_complete")
    snapshot_id = positive.task_input.candidate.snapshot_id
    result = evaluate(positive, good_assessment(positive, cited_observation_ids=(snapshot_id,)))
    assert result.domain_valid is True
    assert result.required_citations_present is False
    assert result.missing_citation_ids == positive.required_citation_ids
    assert result.benchmark_pass is False
    assert result.verdict is Verdict.BENCHMARK_MISS


def test_partial_citation_names_the_missing_measurement() -> None:
    positive = case("positive_complete")
    candidate = positive.task_input.candidate
    cited = (candidate.price.observation_id, candidate.volume.observation_id)
    result = evaluate(positive, good_assessment(positive, cited_observation_ids=cited))
    assert result.missing_citation_ids == {candidate.liquidity.observation_id}
    assert result.verdict is Verdict.BENCHMARK_MISS


def test_citation_from_another_case_is_domain_invalid() -> None:
    positive = case("positive_complete")
    foreign = case("liquidity_zero").task_input.candidate.price.observation_id
    cited = (*sorted(positive.required_citation_ids, key=str), foreign)
    result = evaluate(positive, good_assessment(positive, cited_observation_ids=cited))
    assert result.domain_valid is False
    assert result.domain_reason == "UNKNOWN_OBSERVATION_REFERENCE"
    assert result.verdict is Verdict.DOMAIN_INVALID


def test_fixture_data_in_addition_keeps_the_pass() -> None:
    positive = case("positive_complete")
    answer = good_assessment(
        positive,
        reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT, C.FIXTURE_DATA),
    )
    assert evaluate(positive, answer).benchmark_pass


def test_additional_correct_reason_code_is_not_a_miss_by_itself() -> None:
    below = case("liquidity_below_floor")
    answer = good_assessment(
        below,
        reason_codes=(
            C.PRICE_AVAILABLE,
            C.LIQUIDITY_PRESENT,
            C.LIQUIDITY_BELOW_DISCOVERY_FLOOR,
            C.VOLUME_PRESENT,
        ),
    )
    assert evaluate(below, answer).benchmark_pass


def test_required_code_may_be_carried_as_a_data_gap_entry() -> None:
    # required ⊆ reason_codes ∪ data_gaps: where a code sits is not the criterion.
    unknown = case("liquidity_unknown")
    answer = good_assessment(
        unknown,
        reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_PRESENT, C.LIQUIDITY_UNKNOWN),
    )
    assert evaluate(unknown, answer).benchmark_pass


def test_gap_for_an_observed_measurement_is_domain_invalid() -> None:
    positive = case("positive_complete")
    # Contradicts an AVAILABLE measurement, so the contract already rejects it.
    answer = good_assessment(positive, data_gaps=(C.VOLUME_UNKNOWN,))
    result = evaluate(positive, answer)
    assert result.data_gaps_match is False
    assert result.verdict is Verdict.DOMAIN_INVALID


@pytest.mark.parametrize("strength", list(OrbitStrength))
@pytest.mark.parametrize("slug", SLUGS)
def test_strength_alone_never_changes_the_benchmark(slug: str, strength: OrbitStrength) -> None:
    suite_case = case(slug)
    result = evaluate(suite_case, good_assessment(suite_case, strength=strength))
    assert result.benchmark_pass
    assert result.observed_strength is strength


def test_summary_is_recorded_not_judged() -> None:
    positive = case("positive_complete")
    summary = "Observed price, liquidity and volume on a fixture pair."
    result = evaluate(positive, good_assessment(positive, summary=summary))
    assert result.summary == summary
    assert result.benchmark_pass


def test_schema_invalid_document_is_domain_invalid() -> None:
    positive = case("positive_complete")
    document = good_assessment(positive).model_dump(mode="json")
    document["classification"] = "BUY"
    result = evaluate_document(positive, document)
    assert result.domain_valid is False
    assert result.domain_reason == SCHEMA_INVALID
    assert result.verdict is Verdict.DOMAIN_INVALID
    assert result.observed_strength is None


def test_valid_document_is_scored_like_the_model() -> None:
    positive = case("positive_complete")
    document = good_assessment(positive).model_dump(mode="json")
    assert evaluate_document(positive, document).benchmark_pass


def test_benchmark_pass_is_never_just_domain_validity() -> None:
    # Across the whole matrix, one domain-valid wrong-classification answer per
    # case must fail the benchmark.
    for suite_case in SUITE:
        wrong = next(c for c in OrbitClassification if c != suite_case.expected_classification)
        if wrong == OrbitClassification.INSUFFICIENT_DATA and not suite_case.expected_data_gaps:
            wrong = next(
                c
                for c in OrbitClassification
                if c not in (suite_case.expected_classification, wrong)
            )
        result = evaluate(suite_case, good_assessment(suite_case, classification=wrong))
        assert result.domain_valid, (suite_case.slug, result.domain_reason)
        assert not result.benchmark_pass, suite_case.slug
