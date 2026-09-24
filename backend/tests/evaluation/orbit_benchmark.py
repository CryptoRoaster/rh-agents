"""Offline scoring of one ORBIT assessment against one suite case. Test-only.

Two levels, reported separately and never merged into one flag:

1. Contract / domain validity -- the existing hard requirements. The output must
   parse as `OrbitAssessment` and pass the unchanged production
   `validate_assessment(output, task_input)`. A failure here is DOMAIN_INVALID.

2. Benchmark quality -- expectations this suite adds on top, which are
   deliberately *not* production validation: the expected classification, the
   required reason codes, the exact data gaps and the measurement-level
   citations. A failure here, on a domain-valid answer, is BENCHMARK_MISS.

Strength and summary are recorded as observations and never decide a verdict.
This module lives under `tests/` on purpose: ORBIT quality rules do not belong
in the generic Codex harness under `src/evaluation/`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError

from src.agents.orbit.models import (
    OrbitAssessment,
    OrbitClassification,
    OrbitReasonCode,
    OrbitStrength,
)
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from tests.evaluation.fixtures.orbit_suite import OrbitSuiteCase

SCHEMA_INVALID = "SCHEMA_INVALID"


class Verdict(StrEnum):
    PASS = "PASS"
    BENCHMARK_MISS = "BENCHMARK_MISS"
    DOMAIN_INVALID = "DOMAIN_INVALID"


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    case_slug: str

    # Level 1: contract.
    domain_valid: bool
    domain_reason: str | None

    # Level 2: benchmark quality. Computed even for a domain-invalid answer so a
    # report can say what else was wrong, but never able to rescue it.
    classification_match: bool
    required_reason_codes_present: bool
    missing_reason_codes: frozenset[OrbitReasonCode]
    data_gaps_match: bool
    required_citations_present: bool
    missing_citation_ids: frozenset[UUID]

    # Observed only.
    observed_classification: OrbitClassification | None
    observed_strength: OrbitStrength | None
    summary: str | None

    @property
    def benchmark_criteria_met(self) -> bool:
        return (
            self.classification_match
            and self.required_reason_codes_present
            and self.data_gaps_match
            and self.required_citations_present
        )

    @property
    def benchmark_pass(self) -> bool:
        # A benchmark pass presupposes a valid contract; the converse never holds.
        return self.domain_valid and self.benchmark_criteria_met

    @property
    def verdict(self) -> Verdict:
        if not self.domain_valid:
            return Verdict.DOMAIN_INVALID
        if not self.benchmark_criteria_met:
            return Verdict.BENCHMARK_MISS
        return Verdict.PASS


def evaluate(case: OrbitSuiteCase, assessment: OrbitAssessment) -> BenchmarkResult:
    try:
        validate_assessment(assessment, case.task_input)
    except OrbitValidationError as error:
        domain_valid, domain_reason = False, error.reason_code
    else:
        domain_valid, domain_reason = True, None

    claimed = frozenset(assessment.reason_codes) | frozenset(assessment.data_gaps)
    missing_codes = case.required_reason_codes - claimed
    missing_citations = case.required_citation_ids - frozenset(assessment.cited_observation_ids)
    return BenchmarkResult(
        case_slug=case.slug,
        domain_valid=domain_valid,
        domain_reason=domain_reason,
        classification_match=assessment.classification == case.expected_classification,
        required_reason_codes_present=not missing_codes,
        missing_reason_codes=missing_codes,
        # Exact: naming a gap that is not one, or omitting a real one, both miss.
        data_gaps_match=frozenset(assessment.data_gaps) == case.expected_data_gaps,
        required_citations_present=not missing_citations,
        missing_citation_ids=missing_citations,
        observed_classification=assessment.classification,
        observed_strength=assessment.strength,
        summary=assessment.summary,
    )


def evaluate_document(case: OrbitSuiteCase, document: Mapping[str, object]) -> BenchmarkResult:
    """Score raw model output, where failing the schema is itself DOMAIN_INVALID."""
    try:
        assessment = OrbitAssessment.model_validate(document)
    except ValidationError:
        return BenchmarkResult(
            case_slug=case.slug,
            domain_valid=False,
            domain_reason=SCHEMA_INVALID,
            classification_match=False,
            required_reason_codes_present=False,
            missing_reason_codes=case.required_reason_codes,
            data_gaps_match=False,
            required_citations_present=False,
            missing_citation_ids=case.required_citation_ids,
            observed_classification=None,
            observed_strength=None,
            summary=None,
        )
    return evaluate(case, assessment)
