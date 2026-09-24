"""ORBIT provider comparison runner. Test-only, call-free by default.

Compares two explicitly named provider paths on the reviewed ORBIT suite:

- ``codex-gpt-5.5``: GPT-5.5 through the Codex evaluation harness
  (`prepare_real_run` -> `require_runner()` -> one `evaluate`, fresh per sample)
- ``anthropic-claude-opus-5``: the existing `AnthropicReasoningProvider`

Both see exactly the same domain input -- `ORBIT_INSTRUCTIONS` and
`reasoning_payload(case.task_input)` -- and both outputs are judged by the same
`validate_assessment` and `orbit_benchmark.evaluate`. The provider *controls*
are not identical (see `COMPARISON_LIMITS`), and nothing here claims they are.

Without ``--execute`` the runner only prints the plan: zero model calls, no
credential read, no file written. Credentials are never consent.

    python -m tests.evaluation.orbit_compare_runner --provider both --repetitions 3

Nothing here reads or changes `Settings`, writes `.env`, or touches production
configuration. The Anthropic key is read from ``ANTHROPIC_API_KEY`` only when
``--execute`` is given, only to construct the provider, and only as a
`SecretStr`; it never reaches stdout, a record, a repr or an exception text.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TextIO
from uuid import UUID

from pydantic import SecretStr

from src.agents.orbit.context import orbit_input_digest, reasoning_payload
from src.agents.orbit.models import OrbitAssessment, OrbitTaskInput
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS, ORBIT_PROMPT_HASH
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from src.evaluation.codex.catalogs import GPT_5_5_CATALOG_SHA256, GPT_5_5_CATALOG_SLUG
from src.evaluation.codex.final_preflight import default_launcher
from src.evaluation.codex.models import (
    SUPPORTED_CLI_VERSION,
    CodexLauncher,
    DomainValidationError,
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationOutcome,
    EvaluationRequest,
)
from src.evaluation.codex.prepared_run import prepare_real_run
from src.reasoning.anthropic_provider import DEFAULT_TRANSPORT_RETRIES, AnthropicReasoningProvider
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningRequest,
    ReasoningResult,
)
from tests.evaluation.fixtures.orbit_suite import SUITE, OrbitSuiteCase
from tests.evaluation.fixtures.orbit_suite_v2 import SUITE_V2
from tests.evaluation.orbit_benchmark import evaluate

REPO_ROOT = Path(__file__).resolve().parents[3]
ANTHROPIC_KEY_VARIABLE = "ANTHROPIC_API_KEY"


class ProviderId(StrEnum):
    CODEX_GPT_5_5 = "codex-gpt-5.5"
    ANTHROPIC_CLAUDE_OPUS_5 = "anthropic-claude-opus-5"


PROVIDER_CHOICES: dict[str, tuple[ProviderId, ...]] = {
    "codex": (ProviderId.CODEX_GPT_5_5,),
    "anthropic": (ProviderId.ANTHROPIC_CLAUDE_OPUS_5,),
    # Fixed order; also the per-(case, repetition) order in a plan.
    "both": (ProviderId.CODEX_GPT_5_5, ProviderId.ANTHROPIC_CLAUDE_OPUS_5),
}

# --- Provider controls -------------------------------------------------------

CODEX_MODEL = GPT_5_5_CATALOG_SLUG
CODEX_DEADLINE_SECONDS = 180.0
CODEX_CLEANUP_RESERVE_SECONDS = 5.0

ANTHROPIC_MODEL = "claude-opus-5"
ANTHROPIC_EFFORT: str | None = None
ANTHROPIC_MAX_OUTPUT_TOKENS = 1024
ANTHROPIC_TIMEOUT_SECONDS = 180.0

CONFIGURED_MODEL = {
    ProviderId.CODEX_GPT_5_5: CODEX_MODEL,
    ProviderId.ANTHROPIC_CLAUDE_OPUS_5: ANTHROPIC_MODEL,
}

PROVIDER_CONTROLS: dict[ProviderId, dict[str, object]] = {
    ProviderId.CODEX_GPT_5_5: {
        "path": "codex evaluation harness (PreparedRealRun, isolated CODEX_HOME, Seatbelt)",
        "configured_model": CODEX_MODEL,
        "cli_version": SUPPORTED_CLI_VERSION,
        "catalog_sha256": GPT_5_5_CATALOG_SHA256,
        "effort": None,
        "deadline_seconds": CODEX_DEADLINE_SECONDS,
        "cleanup_reserve_seconds": CODEX_CLEANUP_RESERVE_SECONDS,
        # The harness cannot pin an output-token cap on the CLI turn.
        "max_output_tokens": None,
        "internal_retries": "codex-cli internal stream reconnects, not controllable",
        "underlying_provider_request_count": "UNKNOWN",
        "fresh_prepared_run_per_sample": True,
    },
    ProviderId.ANTHROPIC_CLAUDE_OPUS_5: {
        "path": "AnthropicReasoningProvider (no Codex, no Seatbelt)",
        "configured_model": ANTHROPIC_MODEL,
        "effort": ANTHROPIC_EFFORT,
        "timeout_seconds": ANTHROPIC_TIMEOUT_SECONDS,
        "max_output_tokens": ANTHROPIC_MAX_OUTPUT_TOKENS,
        "transport_retries": DEFAULT_TRANSPORT_RETRIES,
        "internal_retries": f"anthropic SDK max_retries={DEFAULT_TRANSPORT_RETRIES}",
        "underlying_provider_request_count": "UNKNOWN",
    },
}

COMPARISON_LIMITS: dict[str, object] = {
    "identical_domain_input": True,
    "identical_provider_controls": False,
    "shared": [
        "ORBIT_INSTRUCTIONS",
        "reasoning_payload(case.task_input)",
        "OrbitAssessment",
        "validate_assessment(output, case.task_input)",
        "orbit_benchmark.evaluate(case, output)",
    ],
    "asymmetries": [
        "codex: no enforceable max_output_tokens; anthropic: 1024",
        "codex: CLI-internal reconnects; anthropic: transport_retries=1",
        "codex latency is harness wall clock incl. process start; anthropic is API call time",
        "codex reports cached_input_tokens; anthropic reports none (recorded as null)",
        "codex discards domain-invalid output inside the harness; anthropic returns it",
        "anthropic exposes a provider request id; codex does not (thread_id kept separately)",
    ],
    "runner_retries": 0,
    # Neither path exposes how many requests it actually sent: Codex reconnects
    # inside the CLI, the Anthropic SDK retries the transport. Never estimated.
    "underlying_provider_request_count": "UNKNOWN",
    "winner_score": None,
}

PLAN_ORDER = "suite case order > repetition > provider (codex before anthropic)"


# --- Plan --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedSample:
    provider: ProviderId
    case: OrbitSuiteCase
    repetition: int

    @property
    def sample_id(self) -> str:
        # Evaluation metadata only. Never part of the model input.
        return f"{self.provider.value}:{self.case.slug}:r{self.repetition}"

    def describe(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "provider": self.provider.value,
            "case_slug": self.case.slug,
            "repetition": self.repetition,
            "configured_model": CONFIGURED_MODEL[self.provider],
            "prompt_hash": ORBIT_PROMPT_HASH,
            "input_digest": orbit_input_digest(self.case.task_input),
            "controls": PROVIDER_CONTROLS[self.provider],
        }


@dataclass(frozen=True, slots=True)
class ComparisonPlan:
    """Fixed before the first call. Nothing in a campaign re-plans from answers."""

    providers: tuple[ProviderId, ...]
    cases: tuple[OrbitSuiteCase, ...]
    repetitions: int
    samples: tuple[PlannedSample, ...]
    suite: str = "v1"

    def describe(self, *, mode: str) -> dict[str, object]:
        return {
            "mode": mode,
            "suite": self.suite,
            "providers": [p.value for p in self.providers],
            "cases": [c.slug for c in self.cases],
            "repetitions": self.repetitions,
            "planned_samples": len(self.samples),
            "order": PLAN_ORDER,
            "provider_controls": {p.value: PROVIDER_CONTROLS[p] for p in self.providers},
            "comparison_limits": COMPARISON_LIMITS,
            "samples": [sample.describe() for sample in self.samples],
        }


def build_plan(
    providers: Sequence[ProviderId],
    cases: Sequence[OrbitSuiteCase],
    repetitions: int,
    suite: str = "v1",
) -> ComparisonPlan:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    samples = tuple(
        PlannedSample(provider=provider, case=suite_case, repetition=repetition)
        for suite_case in cases
        for repetition in range(1, repetitions + 1)
        for provider in providers
    )
    return ComparisonPlan(
        providers=tuple(providers),
        cases=tuple(cases),
        repetitions=repetitions,
        samples=samples,
        suite=suite,
    )


# --- Requests: the one place model input is built ----------------------------


def bound_domain_validator(task_input: OrbitTaskInput) -> Callable[[OrbitAssessment], None]:
    def validate(output: OrbitAssessment) -> None:
        try:
            validate_assessment(output, task_input)
        except OrbitValidationError as error:
            raise DomainValidationError(error.reason_code) from None

    return validate


def codex_request(suite_case: OrbitSuiteCase) -> EvaluationRequest[OrbitAssessment]:
    return EvaluationRequest(
        instructions=ORBIT_INSTRUCTIONS,
        data=reasoning_payload(suite_case.task_input),
        output_model=OrbitAssessment,
        domain_validator=bound_domain_validator(suite_case.task_input),
        deadline_seconds=CODEX_DEADLINE_SECONDS,
        cleanup_reserve_seconds=CODEX_CLEANUP_RESERVE_SECONDS,
    )


def anthropic_request(suite_case: OrbitSuiteCase) -> ReasoningRequest[OrbitAssessment]:
    return ReasoningRequest(
        instructions=ORBIT_INSTRUCTIONS,
        data=reasoning_payload(suite_case.task_input),
        output_model=OrbitAssessment,
        max_output_tokens=ANTHROPIC_MAX_OUTPUT_TOKENS,
        timeout_seconds=ANTHROPIC_TIMEOUT_SECONDS,
    )


# --- Normalised result -------------------------------------------------------


class SampleStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"  # Codex harness rejection
    PROVIDER_FAILURE = "PROVIDER_FAILURE"  # Anthropic typed failure
    NOT_RUN = "NOT_RUN"  # campaign halted before this sample


class FailureKind(StrEnum):
    # The provider answered, but the answer broke the output contract (schema or
    # domain). This is a model-quality fact, not infrastructure.
    OUTPUT_CONTRACT = "OUTPUT_CONTRACT"
    # No judgeable answer: transport, deadline, process, preflight, refusal.
    TECHNICAL = "TECHNICAL"


CODEX_CONTRACT_FAILURES = frozenset(
    {
        EvaluationFailure.OUTPUT_NOT_JSON,
        EvaluationFailure.OUTPUT_SCHEMA_MISMATCH,
        EvaluationFailure.OUTPUT_DOMAIN_INVALID,
    }
)
# Every INVALID_MODEL_OUTPUT means the call returned no valid structured answer
# (OUTPUT_SCHEMA_MISMATCH, OUTPUT_MISSING, or any future code of that
# category). Treating one of them as technical would drop it from the quality
# denominator and flatter the Anthropic path.
ANTHROPIC_CONTRACT_CATEGORY = ReasoningErrorCategory.INVALID_MODEL_OUTPUT
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
SCHEMA_INVALID = "SCHEMA_INVALID"


@dataclass(frozen=True, slots=True)
class SampleResult:
    sample_id: str
    provider: str
    configured_model: str
    case_slug: str
    repetition: int
    status: SampleStatus

    classification: str | None = None
    strength: str | None = None
    reason_codes: tuple[str, ...] | None = None
    data_gaps: tuple[str, ...] | None = None
    cited_observation_ids: tuple[str, ...] | None = None
    summary: str | None = None

    # None: not judgeable (technical failure or not run).
    domain_valid: bool | None = None
    domain_reason: str | None = None

    # None: no output to judge.
    classification_match: bool | None = None
    required_reason_codes_present: bool | None = None
    data_gaps_match: bool | None = None
    required_citations_present: bool | None = None
    benchmark_pass: bool | None = None
    benchmark_verdict: str | None = None

    latency_ms: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    observed_tool_activity: tuple[tuple[str, int], ...] | None = None

    provider_request_id: str | None = None
    thread_id: str | None = None
    reported_model: str | None = None

    # Set on the execution path at the moment evaluate()/generate_structured()
    # is entered, never inferred from the outcome.
    provider_invocation_started: bool = False

    failure_kind: FailureKind | None = None
    failure: str | None = None
    failure_detail: str | None = None
    process_safe_lines: tuple[str, ...] = ()
    stream_safe_lines: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _base(sample: PlannedSample) -> dict[str, Any]:
    return {
        "sample_id": sample.sample_id,
        "provider": sample.provider.value,
        "configured_model": CONFIGURED_MODEL[sample.provider],
        "case_slug": sample.case.slug,
        "repetition": sample.repetition,
    }


def _judged(sample: PlannedSample, output: OrbitAssessment) -> dict[str, Any]:
    """Contract and benchmark fields for an output that exists."""
    result = evaluate(sample.case, output)
    return {
        "classification": output.classification.value,
        "strength": output.strength.value,
        "reason_codes": tuple(code.value for code in output.reason_codes),
        "data_gaps": tuple(code.value for code in output.data_gaps),
        "cited_observation_ids": tuple(str(i) for i in output.cited_observation_ids),
        "summary": output.summary,
        "domain_valid": result.domain_valid,
        "domain_reason": result.domain_reason,
        "classification_match": result.classification_match,
        "required_reason_codes_present": result.required_reason_codes_present,
        "data_gaps_match": result.data_gaps_match,
        "required_citations_present": result.required_citations_present,
        "benchmark_pass": result.benchmark_pass,
        "benchmark_verdict": result.verdict.value,
        "failure_kind": None if result.domain_valid else FailureKind.OUTPUT_CONTRACT,
    }


def _contract_violation(reason: str) -> dict[str, Any]:
    """An answer that broke the contract but whose content is not available."""
    return {
        "domain_valid": False,
        "domain_reason": reason,
        "benchmark_pass": False,
        "benchmark_verdict": "DOMAIN_INVALID",
        "failure_kind": FailureKind.OUTPUT_CONTRACT,
    }


def normalize_codex(
    sample: PlannedSample, outcome: EvaluationOutcome[OrbitAssessment]
) -> SampleResult:
    if isinstance(outcome, EvaluationCompleted):
        return SampleResult(
            **_base(sample),
            status=SampleStatus.COMPLETED,
            **_judged(sample, outcome.output),
            latency_ms=outcome.wall_clock_ms,
            input_tokens=outcome.usage.input_tokens,
            cached_input_tokens=outcome.usage.cached_input_tokens,
            output_tokens=outcome.usage.output_tokens,
            observed_tool_activity=tuple(
                (a.item_type, a.count) for a in outcome.observed_tool_activity
            ),
            provider_request_id=None,
            thread_id=outcome.thread_id,
            reported_model=outcome.configuration.reported_model,
        )
    # A harness rejection. No assessment is invented and no benchmark miss is
    # recorded; only the harness's own already-redacted fields are kept.
    contract: dict[str, Any] = {"failure_kind": FailureKind.TECHNICAL}
    if outcome.reason in CODEX_CONTRACT_FAILURES:
        contract = _contract_violation(
            outcome.detail_code
            if outcome.reason == EvaluationFailure.OUTPUT_DOMAIN_INVALID
            else SCHEMA_INVALID
        )
    stream = outcome.stream_diagnostic
    return SampleResult(
        **_base(sample),
        status=SampleStatus.REJECTED,
        **contract,
        latency_ms=outcome.wall_clock_ms,
        provider_request_id=None,
        thread_id=stream.thread_id if stream is not None else None,
        failure=outcome.reason.value,
        failure_detail=outcome.detail_code,
        process_safe_lines=outcome.diagnostic.safe_lines if outcome.diagnostic else (),
        stream_safe_lines=stream.safe_lines if stream is not None else (),
    )


def normalize_anthropic_result(
    sample: PlannedSample, result: ReasoningResult[OrbitAssessment]
) -> SampleResult:
    # Schema was enforced by the adapter; the ORBIT domain check was not, and
    # `_judged` runs it. A domain-invalid answer stays COMPLETED + DOMAIN_INVALID.
    return SampleResult(
        **_base(sample),
        status=SampleStatus.COMPLETED,
        **_judged(sample, result.output),
        latency_ms=result.usage.latency_ms,
        input_tokens=result.usage.input_tokens,
        # ReasoningUsage has no cached-input field. Unknown, not zero.
        cached_input_tokens=None,
        output_tokens=result.usage.output_tokens,
        observed_tool_activity=None,
        provider_request_id=result.usage.provider_request_id,
        reported_model=result.model.model,
    )


def anthropic_contract_reason(reason_code: str) -> str:
    if reason_code == "OUTPUT_SCHEMA_MISMATCH":
        return SCHEMA_INVALID
    # The adapter's own typed code, kept as is; anything not code-shaped is
    # replaced rather than copied, so no vendor text can ride along.
    return reason_code if SAFE_CODE.match(reason_code) else "UNRECOGNIZED_OUTPUT_FAILURE"


def normalize_anthropic_failure(sample: PlannedSample, failure: ReasoningFailure) -> SampleResult:
    contract: dict[str, Any] = {"failure_kind": FailureKind.TECHNICAL}
    if failure.category is ANTHROPIC_CONTRACT_CATEGORY:
        contract = _contract_violation(anthropic_contract_reason(failure.reason_code))
    return SampleResult(
        **_base(sample),
        status=SampleStatus.PROVIDER_FAILURE,
        **contract,
        failure=failure.category.value,
        failure_detail=failure.reason_code,
    )


def unclassified_failure(sample: PlannedSample, error: BaseException) -> SampleResult:
    """An exception no adapter typed. Only its class name is kept, never its text."""
    status = (
        SampleStatus.REJECTED
        if sample.provider is ProviderId.CODEX_GPT_5_5
        else SampleStatus.PROVIDER_FAILURE
    )
    return SampleResult(
        **_base(sample),
        status=status,
        failure_kind=FailureKind.TECHNICAL,
        failure="UNCLASSIFIED_EXCEPTION",
        failure_detail=type(error).__name__,
    )


def invocations_started(results: Sequence[SampleResult]) -> int:
    """Samples whose executor entered `evaluate` / `generate_structured`.

    A runner metric only. It is not a count of provider requests: Codex may
    reconnect inside the CLI and the Anthropic SDK may retry the transport, and
    neither is observable here.
    """
    return sum(1 for r in results if r.provider_invocation_started)


def not_run(sample: PlannedSample, reason: str) -> SampleResult:
    return SampleResult(**_base(sample), status=SampleStatus.NOT_RUN, failure=reason)


# --- Executors ---------------------------------------------------------------


@dataclass
class InvocationMark:
    """Flipped by an executor immediately before it enters the provider call."""

    started: bool = False


class SampleExecutor(Protocol):
    async def __call__(self, sample: PlannedSample, mark: InvocationMark) -> SampleResult: ...


PrepareRun = Callable[..., AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class CodexExecutor:
    """One fresh PreparedRealRun and exactly one evaluate per sample."""

    launcher: CodexLauncher
    source_codex_home: Path
    prepare: PrepareRun = prepare_real_run

    async def __call__(self, sample: PlannedSample, mark: InvocationMark) -> SampleResult:
        request = codex_request(sample.case)
        async with self.prepare(
            launcher=self.launcher, source_codex_home=self.source_codex_home
        ) as prepared:
            if prepared.runner is None:
                return SampleResult(
                    **_base(sample),
                    status=SampleStatus.REJECTED,
                    failure_kind=FailureKind.TECHNICAL,
                    failure=EvaluationFailure.PREFLIGHT_FAILED.value,
                    failure_detail="RELEASE_NOT_GRANTED",
                )
            runner = prepared.require_runner()
            mark.started = True
            outcome = await runner.evaluate(request)
        return normalize_codex(sample, outcome)


@dataclass(frozen=True)
class AnthropicExecutor:
    provider: AnthropicReasoningProvider

    async def __call__(self, sample: PlannedSample, mark: InvocationMark) -> SampleResult:
        request = anthropic_request(sample.case)
        mark.started = True
        try:
            result = await self.provider.generate_structured(request)
        except ReasoningFailure as failure:
            return normalize_anthropic_failure(sample, failure)
        return normalize_anthropic_result(sample, result)


def anthropic_provider_from(environ: Mapping[str, str]) -> AnthropicReasoningProvider:
    return AnthropicReasoningProvider(
        api_key=SecretStr(environ[ANTHROPIC_KEY_VARIABLE]),
        model=ANTHROPIC_MODEL,
        effort=ANTHROPIC_EFFORT,
    )


# --- Live gates --------------------------------------------------------------


@dataclass(frozen=True)
class Dependencies:
    """Everything with a side effect, injectable so tests stay offline."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    launcher_finder: Callable[[], CodexLauncher | None] = default_launcher
    source_codex_home: Path = field(default_factory=lambda: Path.home() / ".codex")
    prepare: PrepareRun = prepare_real_run
    anthropic_factory: Callable[[Mapping[str, str]], AnthropicReasoningProvider] = (
        anthropic_provider_from
    )


async def _codex_release_granted(deps: Dependencies, launcher: CodexLauncher) -> bool:
    """Measure the environment once without a model turn."""
    async with deps.prepare(launcher=launcher, source_codex_home=deps.source_codex_home) as run:
        return run.runner is not None


async def build_executors(
    plan: ComparisonPlan, deps: Dependencies
) -> tuple[dict[ProviderId, SampleExecutor], tuple[str, ...]]:
    """Every provider in the plan is checked before any sample starts.

    Returns executors only when *all* are ready; otherwise the blocking reasons
    and no executor at all, so a half-ready `both` starts nothing.
    """
    blocking: list[str] = []
    executors: dict[ProviderId, SampleExecutor] = {}
    if ProviderId.ANTHROPIC_CLAUDE_OPUS_5 in plan.providers:
        key = deps.environ.get(ANTHROPIC_KEY_VARIABLE, "")
        if not key.strip():
            blocking.append("ANTHROPIC_API_KEY_MISSING")
    launcher: CodexLauncher | None = None
    if ProviderId.CODEX_GPT_5_5 in plan.providers:
        launcher = deps.launcher_finder()
        if launcher is None:
            blocking.append("CODEX_LAUNCHER_MISSING")
    if blocking:
        return {}, tuple(blocking)
    if launcher is not None:
        if not await _codex_release_granted(deps, launcher):
            return {}, ("CODEX_PREFLIGHT_BLOCKED",)
        executors[ProviderId.CODEX_GPT_5_5] = CodexExecutor(
            launcher=launcher, source_codex_home=deps.source_codex_home, prepare=deps.prepare
        )
    if ProviderId.ANTHROPIC_CLAUDE_OPUS_5 in plan.providers:
        executors[ProviderId.ANTHROPIC_CLAUDE_OPUS_5] = AnthropicExecutor(
            deps.anthropic_factory(deps.environ)
        )
    return executors, ()


# A Codex sample that leaves the environment unverified stops the campaign:
# later samples would no longer run under the measured conditions.
HALTING_CODEX_FAILURES = frozenset(
    {EvaluationFailure.CLEANUP_INCOMPLETE.value, EvaluationFailure.PREFLIGHT_FAILED.value}
)


async def run_plan(
    plan: ComparisonPlan, executors: Mapping[ProviderId, SampleExecutor]
) -> tuple[SampleResult, ...]:
    """Each sample once, in plan order. No retry, no re-planning."""
    results: list[SampleResult] = []
    halted: str | None = None
    for sample in plan.samples:
        if halted is not None:
            results.append(not_run(sample, halted))
            continue
        mark = InvocationMark()
        try:
            result = await executors[sample.provider](sample, mark)
        except Exception as error:
            # Recorded by class name only, never re-raised with its text.
            result = unclassified_failure(sample, error)
        results.append(replace(result, provider_invocation_started=mark.started))
        if sample.provider is ProviderId.CODEX_GPT_5_5 and result.failure in HALTING_CODEX_FAILURES:
            halted = f"HALTED_AFTER:{sample.sample_id}"
    return tuple(results)


async def execute_plan(
    plan: ComparisonPlan, deps: Dependencies
) -> tuple[tuple[SampleResult, ...], tuple[str, ...]]:
    executors, blocking = await build_executors(plan, deps)
    if blocking:
        return (), blocking
    return await run_plan(plan, executors), ()


# --- Aggregation -------------------------------------------------------------


def _rate(count: int, denominator: int) -> float | None:
    return round(count / denominator, 4) if denominator else None


def aggregate(plan: ComparisonPlan, results: Sequence[SampleResult]) -> dict[str, object]:
    """Counts and rates per provider and per case. No total score, no winner.

    Denominators are explicit:
    - `judged` = samples with a contract verdict (completed, or rejected for an
      output-contract reason). Technical failures and not-run samples are not in
      it, so an outage never reads as a domain miss.
    - quality criteria are rated over domain-valid samples, where both provider
      paths actually have an output to inspect.
    """
    providers: dict[str, object] = {}
    for provider in plan.providers:
        rows = [r for r in results if r.provider == provider.value]
        judged = [r for r in rows if r.domain_valid is not None]
        valid = [r for r in judged if r.domain_valid]

        def count(rows_: Sequence[SampleResult], name: str) -> int:
            return sum(1 for r in rows_ if getattr(r, name) is True)

        planned = sum(1 for s in plan.samples if s.provider is provider)
        metrics: dict[str, object] = {
            "planned_samples": planned,
            "completed_samples": sum(1 for r in rows if r.status is SampleStatus.COMPLETED),
            "technical_failures": sum(1 for r in rows if r.failure_kind is FailureKind.TECHNICAL),
            "output_contract_failures": sum(
                1 for r in rows if r.failure_kind is FailureKind.OUTPUT_CONTRACT
            ),
            "not_run": sum(1 for r in rows if r.status is SampleStatus.NOT_RUN),
            "judged_samples": len(judged),
            "domain_valid_count": len(valid),
            "domain_valid_rate": _rate(len(valid), len(judged)),
            "benchmark_pass_count": count(judged, "benchmark_pass"),
            "benchmark_pass_rate": _rate(count(judged, "benchmark_pass"), len(judged)),
        }
        for label, name in (
            ("classification_match", "classification_match"),
            ("reason_code_fidelity", "required_reason_codes_present"),
            ("data_gap_match", "data_gaps_match"),
            ("citation_match", "required_citations_present"),
        ):
            metrics[f"{label}_count"] = count(valid, name)
            metrics[f"{label}_rate"] = _rate(count(valid, name), len(valid))
        metrics["strength_distribution"] = dict(
            sorted(Counter(r.strength for r in valid if r.strength is not None).items())
        )
        metrics["denominators"] = {
            "domain_valid_rate": "judged_samples",
            "benchmark_pass_rate": "judged_samples",
            "criteria_rates": "domain_valid_count",
        }
        providers[provider.value] = metrics

    cases: dict[str, object] = {}
    for suite_case in plan.cases:
        per_provider: dict[str, object] = {}
        for provider in plan.providers:
            rows = [
                r
                for r in results
                if r.case_slug == suite_case.slug and r.provider == provider.value
            ]
            per_provider[provider.value] = {
                "runs": len(rows),
                "completed": sum(1 for r in rows if r.status is SampleStatus.COMPLETED),
                "benchmark_passes": sum(1 for r in rows if r.benchmark_pass is True),
                "strengths": dict(
                    sorted(Counter(r.strength for r in rows if r.strength is not None).items())
                ),
            }
        cases[suite_case.slug] = per_provider
    return {"providers": providers, "cases": cases, "winner": None}


# --- CLI ---------------------------------------------------------------------


class UsageError(Exception):
    pass


def _repetitions(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


# v1 stays the default so an unchanged command line plans exactly what the v1
# campaign ran. v2 and the union are explicit choices only.
SUITES: dict[str, tuple[OrbitSuiteCase, ...]] = {
    "v1": SUITE,
    "v2": SUITE_V2,
    "all": (*SUITE, *SUITE_V2),
}


def select_cases(
    requested: Sequence[str] | None, suite: Sequence[OrbitSuiteCase] = SUITE
) -> tuple[OrbitSuiteCase, ...]:
    if not requested or "all" in requested:
        if requested and len(set(requested)) > 1:
            raise UsageError("'all' cannot be combined with individual cases")
        return tuple(suite)
    known = {c.slug: c for c in suite}
    unknown = [slug for slug in requested if slug not in known]
    if unknown:
        raise UsageError(f"unknown case: {', '.join(unknown)}")
    wanted = set(requested)
    # Suite order, whatever order they were given in.
    return tuple(c for c in suite if c.slug in wanted)


def check_output_path(raw: str) -> Path:
    """Results go only where explicitly asked, never into the repository."""
    path = Path(raw)
    if not path.is_absolute():
        raise UsageError("--output must be an absolute path")
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise UsageError("--output must be outside the repository")
    if resolved.exists():
        raise UsageError("--output already exists; refusing to overwrite")
    if not resolved.parent.is_dir():
        raise UsageError("--output directory does not exist")
    return resolved


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.evaluation.orbit_compare_runner",
        description="Plan (default) or explicitly execute the ORBIT provider comparison.",
    )
    parser.add_argument("--suite", choices=sorted(SUITES), default="v1")
    parser.add_argument("--provider", choices=sorted(PROVIDER_CHOICES), default="both")
    parser.add_argument("--repetitions", type=_repetitions, default=1)
    parser.add_argument("--case", action="append", dest="cases", metavar="SLUG|all")
    parser.add_argument("--execute", action="store_true", default=False)
    parser.add_argument("--output", default=None)
    return parser.parse_args(list(argv))


def _write_json(stream: TextIO, document: object) -> None:
    json.dump(document, stream, indent=2, default=_json_default)
    stream.write("\n")


def _json_default(value: object) -> object:
    if isinstance(value, UUID | Path):
        return str(value)
    raise TypeError(type(value).__name__)


def main(
    argv: Sequence[str] | None = None,
    *,
    deps: Dependencies | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        cases = select_cases(args.cases, SUITES[args.suite])
        output = check_output_path(args.output) if args.output is not None else None
        if output is not None and not args.execute:
            raise UsageError("--output is only written with --execute")
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    plan = build_plan(PROVIDER_CHOICES[args.provider], cases, args.repetitions, args.suite)

    if not args.execute:
        # No executor exists in a dry run, so no provider path can be entered.
        _write_json(
            out,
            {
                **plan.describe(mode="DRY_RUN"),
                "provider_invocations_started": 0,
                "real_provider_requests_occurred": "NO",
            },
        )
        return 0

    results, blocking = asyncio.run(
        execute_plan(plan, deps if deps is not None else Dependencies())
    )
    if blocking:
        _write_json(
            out,
            {
                "mode": "EXECUTE_REFUSED",
                "provider_invocations_started": 0,
                "real_provider_requests_occurred": "NO",
                "blocking": list(blocking),
                "planned_samples": len(plan.samples),
            },
        )
        return 3
    report = {
        "mode": "EXECUTED",
        "plan": plan.describe(mode="EXECUTED"),
        "provider_invocations_started": invocations_started(results),
        "underlying_provider_request_count": "UNKNOWN",
        "results": [r.to_dict() for r in results],
        "aggregate": aggregate(plan, results),
    }
    if output is not None:
        with output.open("x", encoding="utf-8") as handle:
            os.chmod(output, 0o600)
            _write_json(handle, report)
        _write_json(
            out,
            {
                "mode": "EXECUTED",
                "output": str(output),
                "samples": len(results),
                "provider_invocations_started": invocations_started(results),
                "underlying_provider_request_count": "UNKNOWN",
            },
        )
    else:
        _write_json(out, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
