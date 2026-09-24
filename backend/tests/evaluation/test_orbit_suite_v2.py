"""ORBIT evaluation suite v2, offline.

No model, provider, credential or network: an autouse guard fails any test that
opens a socket. v1 is pinned by a fingerprint so that v2 work can never move it.
"""

import inspect
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
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS
from src.agents.orbit.validation import PRESENCE_CLAIMS
from src.markets.models import Availability
from tests.evaluation.fixtures import orbit_candidate
from tests.evaluation.fixtures.orbit_suite import (
    DISCOVERY_FLOOR_USD,
    HOSTILE_BASE_SYMBOL,
    HOSTILE_PROVIDER,
    HOSTILE_VENUE,
    SUITE,
    OrbitSuiteCase,
    citations_for,
)
from tests.evaluation.fixtures.orbit_suite_v2 import (
    SUITE_V2,
    SUITE_V2_ENTRIES,
    ExpectationBasis,
    OrbitSuiteV2Entry,
    entry,
)
from tests.evaluation.orbit_benchmark import Verdict, evaluate

C = OrbitReasonCode

V2_SLUGS = (
    "price_and_liquidity_unknown",
    "price_and_volume_unavailable",
    "all_measurements_unavailable",
    "zero_liquidity_price_unavailable",
    "below_floor_price_unknown",
    "liquidity_exactly_at_floor",
    "liquidity_just_below_floor",
    "hostile_metadata_liquidity_unknown",
)
CONTRACT_OBVIOUS = {
    "price_and_liquidity_unknown",
    "price_and_volume_unavailable",
    "all_measurements_unavailable",
    "hostile_metadata_liquidity_unknown",
}
BENCHMARK_POLICY = {
    "zero_liquidity_price_unavailable",
    "below_floor_price_unknown",
    "liquidity_exactly_at_floor",
    "liquidity_just_below_floor",
}

# Pins the whole of v1 -- slugs, texts, expectations, citations and every
# task-input field -- as the v1 N=3 campaign ran against it.
V1_FINGERPRINT = "81eec473cc7b2cc3d76f6313e84b0aa4816eb0e4f832c99faa981de60400a4ca"
ORBIT_PROMPT_SHA256 = "d823b391500c583928f9aafa0a2be829fde61cb722b7f2d3504a1b53b372615d"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the offline ORBIT suite must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def good_assessment(suite_case: OrbitSuiteCase, **overrides: Any) -> OrbitAssessment:
    """A minimal answer meeting exactly the case's expectations.

    The schema needs at least one reason code; where a case requires none, the
    expected gaps fill that slot, which is what a careful answer would do.
    """
    candidate = suite_case.task_input.candidate
    reasons = suite_case.required_reason_codes or suite_case.expected_data_gaps
    fields: dict[str, Any] = {
        "classification": suite_case.expected_classification,
        "strength": OrbitStrength.WEAK,
        "reason_codes": tuple(sorted(reasons)),
        "data_gaps": tuple(sorted(suite_case.expected_data_gaps)),
        "cited_observation_ids": tuple(sorted(suite_case.required_citation_ids, key=str)),
        "pair_id": candidate.pair_id,
        "chain": candidate.chain,
        "summary": "Synthetic offline test summary.",
    }
    fields.update(overrides)
    return OrbitAssessment(**fields)


def case_scoped_ids(suite_case: OrbitSuiteCase) -> set[UUID]:
    task_input = suite_case.task_input
    return {
        *task_input.candidate.observation_ids,
        task_input.trade_case_id,
        task_input.task_id,
        task_input.discovery_reference,
    }


def v1_fingerprint() -> str:
    items = [
        {
            "slug": c.slug,
            "description": c.description,
            "notes": c.notes,
            "expected": c.expected_classification.value,
            "required": sorted(x.value for x in c.required_reason_codes),
            "gaps": sorted(x.value for x in c.expected_data_gaps),
            "citations": sorted(str(x) for x in c.required_citation_ids),
            "input": c.task_input.model_dump(mode="json"),
        }
        for c in SUITE
    ]
    return sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


# --- v1 stays frozen -----------------------------------------------------------


def test_v1_is_unchanged() -> None:
    assert v1_fingerprint() == V1_FINGERPRINT
    assert len(SUITE) == 7
    price = next(c for c in SUITE if c.slug == "price_unavailable")
    # The v1 policy assumption stays exactly as the v1 campaign scored it.
    assert price.expected_classification is OrbitClassification.INSUFFICIENT_DATA


def test_prompt_digest_is_unchanged() -> None:
    assert sha256(ORBIT_INSTRUCTIONS.encode()).hexdigest() == ORBIT_PROMPT_SHA256


# --- v2 matrix -----------------------------------------------------------------


def test_v2_has_exactly_the_planned_cases() -> None:
    assert tuple(c.slug for c in SUITE_V2) == V2_SLUGS
    assert len({c.slug for c in SUITE_V2}) == 8


def test_v2_slugs_do_not_reuse_v1_slugs() -> None:
    assert not {c.slug for c in SUITE_V2} & {c.slug for c in SUITE}


def test_v2_ids_are_unique_and_disjoint_from_v1_and_the_probe() -> None:
    v2_ids = [i for c in SUITE_V2 for i in sorted(case_scoped_ids(c), key=str)]
    assert len(v2_ids) == len(set(v2_ids)) == 8 * 7
    v1_ids = {i for c in SUITE for i in case_scoped_ids(c)}
    probe = orbit_candidate.task_input()
    probe_ids = {
        *probe.candidate.observation_ids,
        probe.trade_case_id,
        probe.task_id,
        probe.discovery_reference,
    }
    assert not set(v2_ids) & v1_ids
    assert not set(v2_ids) & probe_ids
    pairs = {c.task_input.candidate.pair_id for c in (*SUITE, *SUITE_V2)}
    assert len(pairs) == 15


@pytest.mark.parametrize("suite_case", SUITE_V2, ids=V2_SLUGS)
def test_v2_inputs_are_valid_fixture_task_inputs(suite_case: OrbitSuiteCase) -> None:
    assert (
        OrbitTaskInput.model_validate(suite_case.task_input.model_dump()) == suite_case.task_input
    )
    assert suite_case.task_input.candidate.is_fixture is True
    assert suite_case.task_input.discovery_liquidity_floor_usd == DISCOVERY_FLOOR_USD


@pytest.mark.parametrize("suite_case", SUITE_V2, ids=V2_SLUGS)
def test_v2_gaps_are_exactly_the_unobserved_measurements(suite_case: OrbitSuiteCase) -> None:
    candidate = suite_case.task_input.candidate
    derived = {
        OrbitReasonCode(f"{name.upper()}_{getattr(candidate, name).status.value}")
        for name in ("price", "liquidity", "volume")
        if getattr(candidate, name).status != Availability.AVAILABLE
    }
    assert suite_case.expected_data_gaps == derived


@pytest.mark.parametrize("suite_case", SUITE_V2, ids=V2_SLUGS)
def test_v2_citations_are_measurement_level(suite_case: OrbitSuiteCase) -> None:
    candidate = suite_case.task_input.candidate
    assert suite_case.required_citation_ids == citations_for(
        suite_case.task_input, suite_case.required_reason_codes | suite_case.expected_data_gaps
    )
    assert suite_case.required_citation_ids == {
        candidate.price.observation_id,
        candidate.liquidity.observation_id,
        candidate.volume.observation_id,
    }
    assert candidate.snapshot_id not in suite_case.required_citation_ids


@pytest.mark.parametrize("suite_case", SUITE_V2, ids=V2_SLUGS)
def test_v2_expectations_are_satisfiable(suite_case: OrbitSuiteCase) -> None:
    result = evaluate(suite_case, good_assessment(suite_case))
    assert result.domain_valid, result.domain_reason
    assert result.benchmark_pass
    assert result.verdict is Verdict.PASS


# --- Model input ---------------------------------------------------------------


@pytest.mark.parametrize("v2_entry", SUITE_V2_ENTRIES, ids=V2_SLUGS)
def test_expectations_and_basis_never_reach_the_payload(v2_entry: OrbitSuiteV2Entry) -> None:
    suite_case = v2_entry.case
    payload = reasoning_payload(suite_case.task_input)
    assert set(payload) == {"market_observation"}
    text = json.dumps(payload, sort_keys=True)
    evaluator_only = [
        suite_case.slug,
        suite_case.description,
        suite_case.expected_classification.value,
        v2_entry.basis.value,
        *(e.value for e in ExpectationBasis),
        *(c.value for c in suite_case.required_reason_codes),
        *(c.value for c in suite_case.expected_data_gaps),
    ]
    if suite_case.notes:
        evaluator_only.append(suite_case.notes)
    if v2_entry.policy_hypothesis:
        evaluator_only.append(v2_entry.policy_hypothesis)
    for secret in evaluator_only:
        assert secret not in text


# --- Expectation basis -----------------------------------------------------------


def test_basis_assignment() -> None:
    assert {
        e.case.slug for e in SUITE_V2_ENTRIES if e.basis is ExpectationBasis.CONTRACT_OBVIOUS
    } == CONTRACT_OBVIOUS
    assert {
        e.case.slug for e in SUITE_V2_ENTRIES if e.basis is ExpectationBasis.BENCHMARK_POLICY
    } == BENCHMARK_POLICY


def test_every_policy_case_states_its_hypothesis() -> None:
    for v2_entry in SUITE_V2_ENTRIES:
        if v2_entry.basis is ExpectationBasis.BENCHMARK_POLICY:
            assert v2_entry.policy_hypothesis
        else:
            assert v2_entry.policy_hypothesis is None


def test_no_basis_claims_to_be_a_production_requirement() -> None:
    assert {b.value for b in ExpectationBasis} == {"CONTRACT_OBVIOUS", "BENCHMARK_POLICY"}
    assert "PRODUCTION_CONTRACT_REQUIREMENT" not in {b.value for b in ExpectationBasis}


@pytest.mark.parametrize("v2_entry", SUITE_V2_ENTRIES, ids=V2_SLUGS)
def test_production_validation_enforces_no_classification(v2_entry: OrbitSuiteV2Entry) -> None:
    """Whatever the basis, a different classification stays contract-valid.

    That is what makes every expected classification a benchmark judgment and
    none of them a production rule -- including the CONTRACT_OBVIOUS ones.
    """
    suite_case = v2_entry.case
    for other in OrbitClassification:
        if other is suite_case.expected_classification:
            continue
        if other is OrbitClassification.INSUFFICIENT_DATA and not suite_case.expected_data_gaps:
            continue  # the schema needs a named gap for INSUFFICIENT_DATA
        result = evaluate(suite_case, good_assessment(suite_case, classification=other))
        assert result.domain_valid, (suite_case.slug, other, result.domain_reason)
        assert result.verdict is Verdict.BENCHMARK_MISS


@pytest.mark.parametrize("v2_entry", SUITE_V2_ENTRIES, ids=V2_SLUGS)
def test_basis_never_changes_the_score(v2_entry: OrbitSuiteV2Entry) -> None:
    suite_case = v2_entry.case
    good = evaluate(suite_case, good_assessment(suite_case))
    assert good.benchmark_pass
    # The scorer takes the case and the answer only; there is no parameter
    # through which the basis could act.
    assert list(inspect.signature(evaluate).parameters) == ["case", "assessment"]


# --- Boundary behaviour ----------------------------------------------------------


def test_exact_floor_below_floor_claim_is_domain_invalid() -> None:
    at_floor = entry("liquidity_exactly_at_floor").case
    assert at_floor.task_input.candidate.liquidity.value_usd == DISCOVERY_FLOOR_USD
    answer = good_assessment(
        at_floor,
        classification=OrbitClassification.NOT_INTERESTING,
        reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_BELOW_DISCOVERY_FLOOR, C.VOLUME_PRESENT),
    )
    result = evaluate(at_floor, answer)
    assert result.domain_valid is False
    assert result.domain_reason == "CONTRADICTED_VALUE"


def test_just_below_floor_code_is_valid_and_required() -> None:
    just_below = entry("liquidity_just_below_floor").case
    liquidity = just_below.task_input.candidate.liquidity.value_usd
    assert liquidity is not None and liquidity < DISCOVERY_FLOOR_USD
    assert C.LIQUIDITY_BELOW_DISCOVERY_FLOOR in just_below.required_reason_codes
    assert evaluate(just_below, good_assessment(just_below)).benchmark_pass
    without = good_assessment(
        just_below, reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT)
    )
    result = evaluate(just_below, without)
    assert result.domain_valid
    assert result.missing_reason_codes == {C.LIQUIDITY_BELOW_DISCOVERY_FLOOR}
    assert result.verdict is Verdict.BENCHMARK_MISS


@pytest.mark.parametrize(
    "claim", sorted(code for codes in PRESENCE_CLAIMS.values() for code in codes)
)
def test_all_unavailable_rejects_every_presence_claim(claim: OrbitReasonCode) -> None:
    none_observed = entry("all_measurements_unavailable").case
    assert not none_observed.required_reason_codes
    answer = good_assessment(
        none_observed, reason_codes=(*sorted(none_observed.expected_data_gaps), claim)
    )
    result = evaluate(none_observed, answer)
    assert result.domain_valid is False
    assert result.domain_reason == "FABRICATED_AVAILABILITY"


def test_all_unavailable_accepts_fixture_data_as_the_only_reason() -> None:
    none_observed = entry("all_measurements_unavailable").case
    answer = good_assessment(none_observed, reason_codes=(C.FIXTURE_DATA,))
    assert evaluate(none_observed, answer).benchmark_pass


def test_all_unavailable_confused_with_unknown_is_domain_invalid() -> None:
    none_observed = entry("all_measurements_unavailable").case
    answer = good_assessment(
        none_observed,
        data_gaps=(C.PRICE_UNKNOWN, C.LIQUIDITY_UNAVAILABLE, C.VOLUME_UNAVAILABLE),
    )
    assert evaluate(none_observed, answer).domain_reason == "CONTRADICTED_AVAILABILITY"


def test_multiple_gaps_must_all_be_named() -> None:
    both = entry("price_and_liquidity_unknown").case
    only_one = good_assessment(both, data_gaps=(C.LIQUIDITY_UNKNOWN,))
    result = evaluate(both, only_one)
    assert result.domain_valid
    assert result.data_gaps_match is False
    assert result.verdict is Verdict.BENCHMARK_MISS


def test_zero_liquidity_with_missing_price_accepts_the_floor_code() -> None:
    zero = entry("zero_liquidity_price_unavailable").case
    answer = good_assessment(
        zero,
        reason_codes=(C.LIQUIDITY_ZERO, C.LIQUIDITY_BELOW_DISCOVERY_FLOOR, C.VOLUME_PRESENT),
    )
    assert evaluate(zero, answer).benchmark_pass


def test_hostile_metadata_with_unknown_liquidity() -> None:
    hostile = entry("hostile_metadata_liquidity_unknown").case
    document = reasoning_payload(hostile.task_input)["market_observation"]
    assert isinstance(document, dict)
    assert document["base_symbol"] == HOSTILE_BASE_SYMBOL
    assert document["venue"] == HOSTILE_VENUE
    assert document["provider"] == HOSTILE_PROVIDER
    assert document["liquidity"]["status"] == "UNKNOWN"  # type: ignore[index]
    assert document["liquidity"]["value_usd"] is None  # type: ignore[index]

    fabricated = good_assessment(
        hostile, reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT)
    )
    assert evaluate(hostile, fabricated).domain_reason == "FABRICATED_AVAILABILITY"

    swallowed = good_assessment(
        hostile,
        classification=OrbitClassification.INTERESTING,
        reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_PRESENT),
        data_gaps=(),
    )
    result = evaluate(hostile, swallowed)
    assert result.domain_valid
    assert result.data_gaps_match is False
    assert result.classification_match is False
    assert result.verdict is Verdict.BENCHMARK_MISS

    renamed = good_assessment(hostile, pair_id=HOSTILE_BASE_SYMBOL)
    assert evaluate(hostile, renamed).domain_reason == "MARKET_MISMATCH"
