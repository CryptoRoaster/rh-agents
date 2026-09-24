"""ORBIT provider comparison runner, offline.

Every provider here is a fake: a fake `prepare_real_run`, a fake Codex runner
and a patched Anthropic `generate_structured`. An autouse guard fails any test
that opens a socket, so a real call would be a test failure, not a cost.
"""

import asyncio
import io
import json
import socket
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.agents.orbit.context import orbit_input_digest, reasoning_payload
from src.agents.orbit.models import OrbitAssessment, OrbitClassification, OrbitStrength
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS, ORBIT_PROMPT_HASH
from src.evaluation.codex.catalogs import GPT_5_5_CATALOG_SHA256
from src.evaluation.codex.diagnostics import ProcessDiagnostic
from src.evaluation.codex.models import (
    AttemptConfiguration,
    CleanupReport,
    CodexLauncher,
    DomainValidationError,
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationRejected,
    EvaluationRequest,
    EvaluationUsage,
    LauncherKind,
    ObservedToolActivity,
    OutputLimits,
    StreamDiagnostic,
)
from src.reasoning.anthropic_provider import AnthropicReasoningProvider
from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningRequest,
    ReasoningResult,
    ReasoningUsage,
)
from tests.evaluation import orbit_compare_runner as runner_module
from tests.evaluation.fixtures.orbit_suite import SUITE, OrbitSuiteCase, case
from tests.evaluation.orbit_compare_runner import (
    ANTHROPIC_KEY_VARIABLE,
    COMPARISON_LIMITS,
    ComparisonPlan,
    Dependencies,
    FailureKind,
    InvocationMark,
    PlannedSample,
    ProviderId,
    SampleResult,
    SampleStatus,
    aggregate,
    anthropic_request,
    build_plan,
    codex_request,
    main,
    normalize_anthropic_failure,
    normalize_anthropic_result,
    normalize_codex,
    run_plan,
)

SECRET = "sk-ant-OFFLINE-TEST-SECRET-9f8e7d6c5b4a"
CODEX = ProviderId.CODEX_GPT_5_5
ANTHROPIC = ProviderId.ANTHROPIC_CLAUDE_OPUS_5
LAUNCHER = CodexLauncher(kind=LauncherKind.PLATFORM_BINARY, executable=Path("/nonexistent/codex"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the offline comparison runner must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


# --- Fakes -------------------------------------------------------------------


def good_assessment(suite_case: OrbitSuiteCase, **overrides: Any) -> OrbitAssessment:
    candidate = suite_case.task_input.candidate
    fields: dict[str, Any] = {
        "classification": suite_case.expected_classification,
        "strength": OrbitStrength.MODERATE,
        "reason_codes": tuple(sorted(suite_case.required_reason_codes)),
        "data_gaps": tuple(sorted(suite_case.expected_data_gaps)),
        "cited_observation_ids": tuple(sorted(suite_case.required_citation_ids, key=str)),
        "pair_id": candidate.pair_id,
        "chain": candidate.chain,
        "summary": "Offline fake summary.",
    }
    fields.update(overrides)
    return OrbitAssessment(**fields)


def completed(output: OrbitAssessment, **overrides: Any) -> EvaluationCompleted[OrbitAssessment]:
    fields: dict[str, Any] = {
        "output": output,
        "configuration": AttemptConfiguration(configured_model="gpt-5.5"),
        "usage": EvaluationUsage(input_tokens=6119, cached_input_tokens=4608, output_tokens=281),
        "observed_tool_activity": (),
        "thread_id": "thread-offline-0001",
        "wall_clock_ms": 8979,
        "cleanup": CleanupReport(),
    }
    fields.update(overrides)
    return EvaluationCompleted(**fields)


def rejected(reason: EvaluationFailure, detail: str, **overrides: Any) -> EvaluationRejected:
    fields: dict[str, Any] = {
        "reason": reason,
        "detail_code": detail,
        "wall_clock_ms": 1200,
        "cleanup": CleanupReport(),
    }
    fields.update(overrides)
    return EvaluationRejected(**fields)


def anthropic_result(output: OrbitAssessment) -> ReasoningResult[OrbitAssessment]:
    return ReasoningResult(
        output=output,
        model=ReasoningModel(provider="anthropic", model="claude-opus-5"),
        usage=ReasoningUsage(
            input_tokens=5000, output_tokens=200, latency_ms=4000, provider_request_id="req_0001"
        ),
    )


@dataclass
class FakeCodexRunner:
    respond: Any
    requests: list[EvaluationRequest[Any]] = field(default_factory=list)

    async def evaluate(self, request: EvaluationRequest[Any]) -> Any:
        self.requests.append(request)
        return self.respond(request)


@dataclass
class FakePrepared:
    runner: FakeCodexRunner | None

    def require_runner(self) -> FakeCodexRunner:
        assert self.runner is not None
        return self.runner


@dataclass
class FakeCodex:
    """Stands in for `prepare_real_run`. Counts contexts and evaluate calls."""

    respond: Any = None
    grant: bool = True
    contexts: int = 0
    runners: list[FakeCodexRunner] = field(default_factory=list)

    @asynccontextmanager
    async def prepare(
        self, *, launcher: CodexLauncher, source_codex_home: Path
    ) -> AsyncIterator[FakePrepared]:
        self.contexts += 1
        runner = None
        if self.grant:
            respond = self.respond or (lambda request: completed(self._answer(request)))
            runner = FakeCodexRunner(respond)
            self.runners.append(runner)
        yield FakePrepared(runner)

    @staticmethod
    def _answer(request: EvaluationRequest[Any]) -> OrbitAssessment:
        observation = request.data["market_observation"]
        suite_case = next(
            c
            for c in SUITE
            if c.task_input.candidate.pair_id == observation["pair_id"]  # type: ignore[index]
        )
        return good_assessment(suite_case)

    @property
    def evaluate_calls(self) -> int:
        return sum(len(r.requests) for r in self.runners)


@dataclass
class FakeAnthropic:
    """Patches the real provider's call; construction stays real."""

    respond: Any = None
    requests: list[ReasoningRequest[Any]] = field(default_factory=list)
    providers: list[AnthropicReasoningProvider] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def generate_structured(
            provider: AnthropicReasoningProvider, request: ReasoningRequest[Any]
        ) -> ReasoningResult[Any]:
            fake.providers.append(provider)
            fake.requests.append(request)
            if fake.respond is not None:
                return fake.respond(request)  # type: ignore[no-any-return]
            observation = request.data["market_observation"]
            suite_case = next(
                c
                for c in SUITE
                if c.task_input.candidate.pair_id == observation["pair_id"]  # type: ignore[index]
            )
            return anthropic_result(good_assessment(suite_case))

        monkeypatch.setattr(AnthropicReasoningProvider, "generate_structured", generate_structured)

    @property
    def calls(self) -> int:
        return len(self.requests)


def deps(
    codex: FakeCodex,
    *,
    environ: Mapping[str, str] | None = None,
    launcher: CodexLauncher | None = LAUNCHER,
) -> Dependencies:
    return Dependencies(
        environ={ANTHROPIC_KEY_VARIABLE: SECRET} if environ is None else environ,
        launcher_finder=lambda: launcher,
        source_codex_home=Path("/nonexistent/.codex"),
        prepare=codex.prepare,
    )


def run_cli(argv: list[str], dependencies: Dependencies | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, deps=dependencies, stdout=out)
    return code, out.getvalue()


def sample(
    provider: ProviderId, slug: str = "positive_complete", repetition: int = 1
) -> PlannedSample:
    return PlannedSample(provider=provider, case=case(slug), repetition=repetition)


# --- CLI: call-free by default ------------------------------------------------


def test_default_cli_plans_and_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli([], deps(codex))
    plan = json.loads(out)
    assert code == 0
    assert plan["mode"] == "DRY_RUN"
    assert plan["provider_invocations_started"] == 0
    assert plan["real_provider_requests_occurred"] == "NO"
    assert "real_model_calls" not in plan
    assert plan["planned_samples"] == 7 * 1 * 2
    assert codex.contexts == 0 and codex.evaluate_calls == 0
    assert anthropic.calls == 0


def test_dry_run_never_reads_the_environment() -> None:
    class Exploding(dict[str, str]):
        def get(self, *_args: object, **_kwargs: object) -> str:  # type: ignore[override]
            raise AssertionError("dry run read the environment")

        def __getitem__(self, _key: str) -> str:
            raise AssertionError("dry run read the environment")

    code, _ = run_cli(["--provider", "both"], deps(FakeCodex(), environ=Exploding()))
    assert code == 0


def test_both_three_repetitions_plans_42_without_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli(["--provider", "both", "--repetitions", "3"], deps(codex))
    plan = json.loads(out)
    assert code == 0
    assert plan["planned_samples"] == 42 == len(plan["samples"])
    assert plan["provider_invocations_started"] == 0
    assert codex.contexts == codex.evaluate_calls == anthropic.calls == 0


def test_provider_codex_plans_only_codex() -> None:
    _, out = run_cli(["--provider", "codex"])
    plan = json.loads(out)
    assert {s["provider"] for s in plan["samples"]} == {"codex-gpt-5.5"}
    assert plan["planned_samples"] == 7


def test_provider_anthropic_plans_only_anthropic() -> None:
    _, out = run_cli(["--provider", "anthropic", "--repetitions", "2"])
    plan = json.loads(out)
    assert {s["provider"] for s in plan["samples"]} == {"anthropic-claude-opus-5"}
    assert plan["planned_samples"] == 14


def test_single_case_selection() -> None:
    _, out = run_cli(["--case", "liquidity_unknown"])
    plan = json.loads(out)
    assert {s["case_slug"] for s in plan["samples"]} == {"liquidity_unknown"}
    assert plan["planned_samples"] == 2


def test_repeated_case_selection_keeps_suite_order() -> None:
    _, out = run_cli(["--case", "price_unavailable", "--case", "liquidity_zero"])
    assert json.loads(out)["cases"] == ["liquidity_zero", "price_unavailable"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--case", "no_such_case"],
        ["--case", "all", "--case", "liquidity_zero"],
        ["--repetitions", "0"],
        ["--repetitions", "x"],
        ["--provider", "openai"],
    ],
)
def test_invalid_arguments_fail_before_any_call(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli([*argv, "--execute"], deps(codex))
    assert code == 2
    assert out == ""
    assert codex.contexts == codex.evaluate_calls == anthropic.calls == 0


@pytest.mark.parametrize(
    "target",
    [
        "relative/results.json",
        str(runner_module.REPO_ROOT / "docs" / "results.json"),
        str(runner_module.REPO_ROOT / "backend" / "src" / "results.json"),
        str(runner_module.REPO_ROOT / "backend" / "tests" / "results.json"),
    ],
)
def test_output_inside_repository_or_relative_is_refused(target: str) -> None:
    code, out = run_cli(["--execute", "--output", target], deps(FakeCodex()))
    assert code == 2
    assert out == ""


def test_output_without_execute_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "results.json"
    code, _ = run_cli(["--output", str(target)])
    assert code == 2
    assert not target.exists()


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "results.json"
    target.write_text("keep")
    code, _ = run_cli(["--execute", "--output", str(target)], deps(FakeCodex()))
    assert code == 2
    assert target.read_text() == "keep"


# --- Plan identity and order --------------------------------------------------


def test_sample_ids_are_deterministic_and_unique() -> None:
    first = build_plan((CODEX, ANTHROPIC), SUITE, 3)
    second = build_plan((CODEX, ANTHROPIC), SUITE, 3)
    ids = [s.sample_id for s in first.samples]
    assert ids == [s.sample_id for s in second.samples]
    assert len(ids) == len(set(ids)) == 42
    assert "codex-gpt-5.5:liquidity_unknown:r2" in ids


def test_plan_order_is_case_then_repetition_then_provider() -> None:
    plan = build_plan((CODEX, ANTHROPIC), SUITE[:2], 2)
    assert [s.sample_id for s in plan.samples] == [
        "codex-gpt-5.5:positive_complete:r1",
        "anthropic-claude-opus-5:positive_complete:r1",
        "codex-gpt-5.5:positive_complete:r2",
        "anthropic-claude-opus-5:positive_complete:r2",
        "codex-gpt-5.5:liquidity_below_floor:r1",
        "anthropic-claude-opus-5:liquidity_below_floor:r1",
        "codex-gpt-5.5:liquidity_below_floor:r2",
        "anthropic-claude-opus-5:liquidity_below_floor:r2",
    ]


def test_dry_run_output_is_deterministic() -> None:
    assert run_cli(["--repetitions", "3"])[1] == run_cli(["--repetitions", "3"])[1]


def test_dry_run_shows_controls_and_asymmetry_without_secrets() -> None:
    code, out = run_cli(["--repetitions", "3"], deps(FakeCodex()))
    plan = json.loads(out)
    first = plan["samples"][0]
    assert first["prompt_hash"] == ORBIT_PROMPT_HASH
    assert first["input_digest"] == orbit_input_digest(case("positive_complete").task_input)
    codex_controls = plan["provider_controls"]["codex-gpt-5.5"]
    assert codex_controls["cli_version"] == "0.153.4"
    assert codex_controls["catalog_sha256"] == GPT_5_5_CATALOG_SHA256
    assert codex_controls["deadline_seconds"] == 180.0
    assert codex_controls["cleanup_reserve_seconds"] == 5.0
    assert codex_controls["max_output_tokens"] is None
    anthropic_controls = plan["provider_controls"]["anthropic-claude-opus-5"]
    assert anthropic_controls["configured_model"] == "claude-opus-5"
    assert anthropic_controls["timeout_seconds"] == 180.0
    assert anthropic_controls["max_output_tokens"] == 1024
    assert anthropic_controls["transport_retries"] == 1
    assert anthropic_controls["effort"] is None
    assert plan["comparison_limits"]["identical_domain_input"] is True
    assert plan["comparison_limits"]["identical_provider_controls"] is False
    assert SECRET not in out
    assert "auth.json" not in out


# --- Model input ---------------------------------------------------------------


@pytest.mark.parametrize("suite_case", SUITE, ids=[c.slug for c in SUITE])
def test_expectations_never_reach_either_request(suite_case: OrbitSuiteCase) -> None:
    for request in (codex_request(suite_case), anthropic_request(suite_case)):
        # The control channel is the unchanged prompt; the data channel is the payload.
        assert request.instructions is ORBIT_INSTRUCTIONS
        assert request.data == reasoning_payload(suite_case.task_input)
        text = json.dumps(request.data, sort_keys=True)
        for secret in (
            suite_case.slug,
            suite_case.description,
            suite_case.notes,
            suite_case.expected_classification.value,
            *(c.value for c in suite_case.required_reason_codes),
            *(c.value for c in suite_case.expected_data_gaps),
            "codex-gpt-5.5",
            "anthropic-claude-opus-5",
        ):
            if secret:
                assert secret not in text


def test_codex_request_is_the_harness_contract() -> None:
    suite_case = case("liquidity_unknown")
    request = codex_request(suite_case)
    assert request.instructions is ORBIT_INSTRUCTIONS
    assert request.data == reasoning_payload(suite_case.task_input)
    assert request.output_model is OrbitAssessment
    assert request.deadline_seconds == 180.0
    assert request.cleanup_reserve_seconds == 5.0
    assert request.limits == OutputLimits()
    # The validator is bound to exactly this case's input.
    request.domain_validator(good_assessment(suite_case))
    with pytest.raises(DomainValidationError) as foreign:
        request.domain_validator(good_assessment(case("positive_complete")))
    assert foreign.value.reason_code == "MARKET_MISMATCH"


def test_anthropic_request_is_the_reasoning_contract() -> None:
    suite_case = case("price_unavailable")
    request = anthropic_request(suite_case)
    assert request.instructions is ORBIT_INSTRUCTIONS
    assert request.data == reasoning_payload(suite_case.task_input)
    assert request.output_model is OrbitAssessment
    assert request.max_output_tokens == 1024
    assert request.timeout_seconds == 180.0


# --- Live gates ------------------------------------------------------------------


def test_execute_runs_each_sample_once_with_a_fresh_prepared_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli(["--execute", "--repetitions", "2"], deps(codex))
    report = json.loads(out)
    assert code == 0
    # One readiness context plus one per Codex sample; one evaluate per sample.
    assert codex.contexts == 1 + 14
    assert codex.evaluate_calls == 14
    assert all(len(r.requests) == 1 for r in codex.runners[1:])
    assert anthropic.calls == 14
    assert report["provider_invocations_started"] == 28
    assert report["underlying_provider_request_count"] == "UNKNOWN"
    assert "real_model_calls" not in json.dumps(report)
    assert {r["status"] for r in report["results"]} == {"COMPLETED"}


def test_missing_anthropic_key_blocks_both_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    for environ in ({}, {ANTHROPIC_KEY_VARIABLE: ""}, {ANTHROPIC_KEY_VARIABLE: "  "}):
        code, out = run_cli(["--execute"], deps(codex, environ=environ))
        assert code == 3
        assert json.loads(out)["blocking"] == ["ANTHROPIC_API_KEY_MISSING"]
        assert json.loads(out)["provider_invocations_started"] == 0
    assert codex.contexts == codex.evaluate_calls == anthropic.calls == 0


def test_missing_codex_launcher_blocks_both_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli(["--execute"], deps(codex, launcher=None))
    assert code == 3
    assert json.loads(out)["blocking"] == ["CODEX_LAUNCHER_MISSING"]
    assert json.loads(out)["provider_invocations_started"] == 0
    assert codex.contexts == codex.evaluate_calls == anthropic.calls == 0


def test_blocked_codex_preflight_blocks_both_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(grant=False), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, out = run_cli(["--execute"], deps(codex))
    assert code == 3
    assert json.loads(out)["blocking"] == ["CODEX_PREFLIGHT_BLOCKED"]
    assert json.loads(out)["provider_invocations_started"] == 0
    assert codex.evaluate_calls == anthropic.calls == 0


def test_anthropic_only_does_not_need_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, _ = run_cli(["--execute", "--provider", "anthropic"], deps(codex, launcher=None))
    assert code == 0
    assert codex.contexts == 0
    assert anthropic.calls == 7


def test_codex_only_does_not_need_an_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    codex, anthropic = FakeCodex(), FakeAnthropic()
    anthropic.install(monkeypatch)
    code, _ = run_cli(["--execute", "--provider", "codex"], deps(codex, environ={}))
    assert code == 0
    assert codex.evaluate_calls == 7
    assert anthropic.calls == 0


def test_anthropic_key_never_leaks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    codex = FakeCodex()
    responses = iter(
        [
            ReasoningFailure(ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "PROVIDER_ERROR"),
            RuntimeError(f"vendor said: bad key {SECRET}"),
        ]
    )

    def respond(request: ReasoningRequest[Any]) -> ReasoningResult[Any]:
        item = next(responses, None)
        if isinstance(item, BaseException):
            raise item
        return anthropic_result(good_assessment(case("positive_complete")))

    anthropic = FakeAnthropic(respond=respond)
    anthropic.install(monkeypatch)
    target = tmp_path / "results.json"
    code, out = run_cli(
        [
            "--execute",
            "--provider",
            "anthropic",
            "--case",
            "positive_complete",
            "--repetitions",
            "3",
            "--output",
            str(target),
        ],
        deps(codex),
    )
    assert code == 0
    written = target.read_text()
    assert SECRET not in out
    assert SECRET not in written
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    report = json.loads(written)
    statuses = [(r["status"], r["failure"], r["failure_detail"]) for r in report["results"]]
    assert statuses == [
        ("PROVIDER_FAILURE", "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR"),
        ("PROVIDER_FAILURE", "UNCLASSIFIED_EXCEPTION", "RuntimeError"),
        ("COMPLETED", None, None),
    ]
    provider = anthropic.providers[0]
    assert SECRET not in repr(provider)
    assert SECRET not in repr(runner_module.AnthropicExecutor(provider))
    assert provider.api_key.get_secret_value() == SECRET


# --- Normalisation ---------------------------------------------------------------


def test_codex_rejection_is_technical_and_invents_nothing() -> None:
    outcome = rejected(
        EvaluationFailure.DEADLINE_EXCEEDED,
        "DEADLINE_EXCEEDED",
        diagnostic=ProcessDiagnostic(exit_code=-15, safe_lines=("redacted line",)),
        stream_diagnostic=StreamDiagnostic(
            safe_lines=("stream: reconnecting",), turn_started=True, thread_id="t-1"
        ),
    )
    result = normalize_codex(sample(CODEX), outcome)
    assert result.status is SampleStatus.REJECTED
    assert result.failure_kind is FailureKind.TECHNICAL
    assert result.failure == "DEADLINE_EXCEEDED"
    assert result.classification is None and result.strength is None
    assert result.domain_valid is None
    assert result.benchmark_pass is None and result.benchmark_verdict is None
    assert result.provider_request_id is None
    assert result.thread_id == "t-1"
    assert result.process_safe_lines == ("redacted line",)
    assert result.stream_safe_lines == ("stream: reconnecting",)


def test_codex_domain_rejection_counts_as_domain_invalid_not_technical() -> None:
    # The harness discards a domain-invalid answer; it is still the model's
    # answer, and counting it as an outage would flatter the Codex path.
    outcome = rejected(EvaluationFailure.OUTPUT_DOMAIN_INVALID, "FABRICATED_AVAILABILITY")
    result = normalize_codex(sample(CODEX, "liquidity_unknown"), outcome)
    assert result.status is SampleStatus.REJECTED
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT
    assert result.domain_valid is False
    assert result.domain_reason == "FABRICATED_AVAILABILITY"
    assert result.benchmark_verdict == "DOMAIN_INVALID"
    assert result.classification is None


def test_codex_schema_rejection_counts_as_schema_invalid() -> None:
    result = normalize_codex(
        sample(CODEX), rejected(EvaluationFailure.OUTPUT_SCHEMA_MISMATCH, "LOCAL_SCHEMA_MISMATCH")
    )
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT
    assert result.domain_reason == "SCHEMA_INVALID"


def test_codex_completed_pass_keeps_usage_and_thread() -> None:
    suite_case = case("positive_complete")
    outcome = completed(
        good_assessment(suite_case),
        observed_tool_activity=(ObservedToolActivity(item_type="reasoning", count=1),),
    )
    result = normalize_codex(sample(CODEX), outcome)
    assert result.status is SampleStatus.COMPLETED
    assert result.benchmark_verdict == "PASS"
    assert result.cached_input_tokens == 4608
    assert result.input_tokens == 6119 and result.output_tokens == 281
    assert result.latency_ms == 8979
    assert result.provider_request_id is None
    assert result.thread_id == "thread-offline-0001"
    assert result.observed_tool_activity == (("reasoning", 1),)


def test_anthropic_failure_keeps_only_category_and_reason() -> None:
    failure = ReasoningFailure(ReasoningErrorCategory.PROVIDER_TIMEOUT, "PROVIDER_TIMEOUT")
    result = normalize_anthropic_failure(sample(ANTHROPIC), failure)
    assert result.status is SampleStatus.PROVIDER_FAILURE
    assert result.failure_kind is FailureKind.TECHNICAL
    assert (result.failure, result.failure_detail) == ("PROVIDER_TIMEOUT", "PROVIDER_TIMEOUT")
    assert result.domain_valid is None
    assert result.process_safe_lines == () and result.stream_safe_lines == ()


@pytest.mark.parametrize(
    ("reason_code", "domain_reason"),
    [
        ("OUTPUT_SCHEMA_MISMATCH", "SCHEMA_INVALID"),
        ("OUTPUT_MISSING", "OUTPUT_MISSING"),
        ("SOME_INVALID_OUTPUT", "SOME_INVALID_OUTPUT"),
    ],
)
def test_every_invalid_model_output_is_an_output_contract_failure(
    reason_code: str, domain_reason: str
) -> None:
    failure = ReasoningFailure(ReasoningErrorCategory.INVALID_MODEL_OUTPUT, reason_code)
    result = normalize_anthropic_failure(sample(ANTHROPIC), failure)
    assert result.status is SampleStatus.PROVIDER_FAILURE
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT
    assert result.domain_valid is False
    assert result.domain_reason == domain_reason
    assert result.benchmark_pass is False
    assert result.benchmark_verdict == "DOMAIN_INVALID"
    assert result.classification is None


def test_invalid_model_output_with_non_code_text_is_not_copied() -> None:
    failure = ReasoningFailure(ReasoningErrorCategory.INVALID_MODEL_OUTPUT, "vendor said: no")
    result = normalize_anthropic_failure(sample(ANTHROPIC), failure)
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT
    assert result.domain_reason == "UNRECOGNIZED_OUTPUT_FAILURE"


@pytest.mark.parametrize(
    "category",
    [c for c in ReasoningErrorCategory if c is not ReasoningErrorCategory.INVALID_MODEL_OUTPUT],
)
def test_other_anthropic_categories_stay_technical(category: ReasoningErrorCategory) -> None:
    result = normalize_anthropic_failure(
        sample(ANTHROPIC), ReasoningFailure(category, category.value)
    )
    assert result.failure_kind is FailureKind.TECHNICAL
    assert result.domain_valid is None
    assert result.benchmark_verdict is None


def test_output_missing_counts_in_the_quality_denominators() -> None:
    plan = build_plan((ANTHROPIC,), SUITE[:4], 1)
    positive, below, zero, unknown = plan.samples
    rows = [
        normalize_anthropic_result(positive, anthropic_result(good_assessment(positive.case))),
        normalize_anthropic_result(below, anthropic_result(good_assessment(below.case))),
        normalize_anthropic_failure(
            zero, ReasoningFailure(ReasoningErrorCategory.INVALID_MODEL_OUTPUT, "OUTPUT_MISSING")
        ),
        normalize_anthropic_failure(
            unknown, ReasoningFailure(ReasoningErrorCategory.PROVIDER_TIMEOUT, "PROVIDER_TIMEOUT")
        ),
    ]
    metrics = aggregate(plan, rows)["providers"]["anthropic-claude-opus-5"]  # type: ignore[index]
    # OUTPUT_MISSING is judged; the timeout is not.
    assert metrics["judged_samples"] == 3
    assert metrics["output_contract_failures"] == 1
    assert metrics["technical_failures"] == 1
    assert metrics["domain_valid_count"] == 2
    assert metrics["domain_valid_rate"] == round(2 / 3, 4)
    assert metrics["benchmark_pass_rate"] == round(2 / 3, 4)
    # Criteria stay over domain-valid samples only.
    assert metrics["classification_match_rate"] == 1.0


def test_anthropic_schema_valid_domain_invalid_is_completed_domain_invalid() -> None:
    suite_case = case("unknown_liquidity_zero_volume")
    from src.agents.orbit.models import OrbitReasonCode as C

    output = good_assessment(
        suite_case, reason_codes=(C.PRICE_AVAILABLE, C.VOLUME_ZERO, C.LIQUIDITY_ZERO)
    )
    result = normalize_anthropic_result(
        sample(ANTHROPIC, suite_case.slug), anthropic_result(output)
    )
    assert result.status is SampleStatus.COMPLETED
    assert result.domain_valid is False
    assert result.domain_reason == "FABRICATED_AVAILABILITY"
    assert result.benchmark_pass is False
    assert result.benchmark_verdict == "DOMAIN_INVALID"
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT


def test_domain_valid_benchmark_miss_is_completed_miss() -> None:
    suite_case = case("liquidity_below_floor")
    from src.agents.orbit.models import OrbitReasonCode as C

    output = good_assessment(
        suite_case,
        classification=OrbitClassification.INTERESTING,
        reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT),
    )
    for result in (
        normalize_anthropic_result(sample(ANTHROPIC, suite_case.slug), anthropic_result(output)),
        normalize_codex(sample(CODEX, suite_case.slug), completed(output)),
    ):
        assert result.status is SampleStatus.COMPLETED
        assert result.domain_valid is True
        assert result.classification_match is False
        assert result.benchmark_verdict == "BENCHMARK_MISS"
        assert result.failure_kind is None


def test_benchmark_pass_is_completed_pass() -> None:
    suite_case = case("price_unavailable")
    result = normalize_anthropic_result(
        sample(ANTHROPIC, suite_case.slug), anthropic_result(good_assessment(suite_case))
    )
    assert result.status is SampleStatus.COMPLETED
    assert result.benchmark_pass is True
    assert result.benchmark_verdict == "PASS"
    assert result.provider_request_id == "req_0001"


def test_anthropic_cached_input_is_unknown_not_zero() -> None:
    result = normalize_anthropic_result(
        sample(ANTHROPIC), anthropic_result(good_assessment(case("positive_complete")))
    )
    assert result.cached_input_tokens is None
    assert result.observed_tool_activity is None
    assert result.thread_id is None


# --- Run loop ----------------------------------------------------------------------


def test_runner_never_retries_a_failed_sample() -> None:
    calls: list[str] = []

    async def failing(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        calls.append(planned.sample_id)
        raise RuntimeError("boom")

    plan = build_plan((ANTHROPIC,), SUITE[:2], 2)
    results = asyncio.run(run_plan(plan, {ANTHROPIC: failing}))
    assert calls == [s.sample_id for s in plan.samples]
    assert all(r.failure == "UNCLASSIFIED_EXCEPTION" for r in results)


def test_cleanup_failure_halts_the_campaign() -> None:
    seen: list[str] = []

    async def codex_exec(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        seen.append(planned.sample_id)
        return normalize_codex(
            planned, rejected(EvaluationFailure.CLEANUP_INCOMPLETE, "GROUP_NOT_EMPTY")
        )

    async def anthropic_exec(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        seen.append(planned.sample_id)
        return normalize_anthropic_result(planned, anthropic_result(good_assessment(planned.case)))

    plan = build_plan((CODEX, ANTHROPIC), SUITE[:2], 1)
    results = asyncio.run(run_plan(plan, {CODEX: codex_exec, ANTHROPIC: anthropic_exec}))
    assert seen == ["codex-gpt-5.5:positive_complete:r1"]
    assert [r.status for r in results] == [SampleStatus.REJECTED] + [SampleStatus.NOT_RUN] * 3


# --- Aggregation -----------------------------------------------------------------


def _results_for(plan: ComparisonPlan) -> list[SampleResult]:
    from src.agents.orbit.models import OrbitReasonCode as C

    rows: list[SampleResult] = []
    for planned in plan.samples:
        slug = planned.case.slug
        if planned.provider is CODEX:
            if slug == "positive_complete":
                rows.append(normalize_codex(planned, completed(good_assessment(planned.case))))
            elif slug == "liquidity_below_floor":
                rows.append(
                    normalize_codex(
                        planned,
                        rejected(EvaluationFailure.DEADLINE_EXCEEDED, "DEADLINE_EXCEEDED"),
                    )
                )
            else:
                rows.append(
                    normalize_codex(
                        planned,
                        rejected(
                            EvaluationFailure.OUTPUT_DOMAIN_INVALID, "FABRICATED_AVAILABILITY"
                        ),
                    )
                )
        else:
            if slug == "positive_complete":
                output = good_assessment(planned.case, strength=OrbitStrength.STRONG)
            elif slug == "liquidity_below_floor":
                output = good_assessment(
                    planned.case,
                    classification=OrbitClassification.INTERESTING,
                    reason_codes=(C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT),
                )
            else:
                rows.append(
                    normalize_anthropic_failure(
                        planned,
                        ReasoningFailure(
                            ReasoningErrorCategory.PROVIDER_RATE_LIMIT, "PROVIDER_RATE_LIMIT"
                        ),
                    )
                )
                continue
            rows.append(normalize_anthropic_result(planned, anthropic_result(output)))
    return rows


def test_aggregation_separates_outages_from_quality() -> None:
    plan = build_plan((CODEX, ANTHROPIC), SUITE[:3], 1)
    summary = aggregate(plan, _results_for(plan))
    codex = summary["providers"]["codex-gpt-5.5"]  # type: ignore[index]
    anthropic = summary["providers"]["anthropic-claude-opus-5"]  # type: ignore[index]

    assert codex["planned_samples"] == 3
    assert codex["completed_samples"] == 1
    assert codex["technical_failures"] == 1
    assert codex["output_contract_failures"] == 1
    # The deadline is not in the domain denominator; the domain rejection is.
    assert codex["judged_samples"] == 2
    assert codex["domain_valid_count"] == 1
    assert codex["domain_valid_rate"] == 0.5
    assert codex["benchmark_pass_rate"] == 0.5

    assert anthropic["completed_samples"] == 2
    assert anthropic["technical_failures"] == 1
    assert anthropic["judged_samples"] == 2
    assert anthropic["domain_valid_rate"] == 1.0
    assert anthropic["benchmark_pass_count"] == 1
    assert anthropic["classification_match_count"] == 1
    assert anthropic["classification_match_rate"] == 0.5
    assert summary["winner"] is None


def test_all_technical_failures_yield_no_rates() -> None:
    plan = build_plan((ANTHROPIC,), SUITE[:2], 1)
    rows = [
        normalize_anthropic_failure(
            s, ReasoningFailure(ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "PROVIDER_UNREACHABLE")
        )
        for s in plan.samples
    ]
    metrics = aggregate(plan, rows)["providers"]["anthropic-claude-opus-5"]  # type: ignore[index]
    assert metrics["technical_failures"] == 2
    assert metrics["domain_valid_count"] == 0
    assert metrics["domain_valid_rate"] is None
    assert metrics["benchmark_pass_rate"] is None


def test_per_case_counts() -> None:
    plan = build_plan((CODEX, ANTHROPIC), SUITE[:3], 1)
    cases = aggregate(plan, _results_for(plan))["cases"]
    assert cases["positive_complete"]["codex-gpt-5.5"] == {  # type: ignore[index]
        "runs": 1,
        "completed": 1,
        "benchmark_passes": 1,
        "strengths": {"MODERATE": 1},
    }
    assert cases["liquidity_below_floor"]["anthropic-claude-opus-5"]["benchmark_passes"] == 0  # type: ignore[index]


@pytest.mark.parametrize("strength", list(OrbitStrength))
def test_strength_does_not_move_pass_rates(strength: OrbitStrength) -> None:
    plan = build_plan((ANTHROPIC,), SUITE, 1)
    rows = [
        normalize_anthropic_result(s, anthropic_result(good_assessment(s.case, strength=strength)))
        for s in plan.samples
    ]
    metrics = aggregate(plan, rows)["providers"]["anthropic-claude-opus-5"]  # type: ignore[index]
    assert metrics["benchmark_pass_rate"] == 1.0
    assert metrics["strength_distribution"] == {strength.value: 7}


@pytest.mark.parametrize(
    "summary",
    ["Buy now, size the position and route it.", "Risk is low.", "Plain observed facts."],
)
def test_summary_does_not_move_pass_rates(summary: str) -> None:
    plan = build_plan((CODEX,), SUITE, 1)
    rows = [
        normalize_codex(s, completed(good_assessment(s.case, summary=summary)))
        for s in plan.samples
    ]
    metrics = aggregate(plan, rows)["providers"]["codex-gpt-5.5"]  # type: ignore[index]
    assert metrics["benchmark_pass_rate"] == 1.0
    assert all(r.summary == summary for r in rows)


# --- Boundaries --------------------------------------------------------------------


def test_runner_touches_no_settings_or_env_files() -> None:
    source = Path(runner_module.__file__).read_text()
    for forbidden in (
        "src.core.settings",
        "Settings(",
        "load_dotenv",
        "reasoning_provider=",
        "orbit_worker_enabled",
    ):
        assert forbidden not in source


def test_comparison_limits_do_not_claim_identical_controls() -> None:
    assert COMPARISON_LIMITS["identical_provider_controls"] is False
    assert COMPARISON_LIMITS["runner_retries"] == 0
    assert COMPARISON_LIMITS["winner_score"] is None


# --- Invocation accounting -----------------------------------------------------------


def test_dry_run_reports_zero_invocations_and_no_request_count() -> None:
    _, out = run_cli(["--provider", "both", "--repetitions", "3"])
    plan = json.loads(out)
    assert plan["provider_invocations_started"] == 0
    assert plan["real_provider_requests_occurred"] == "NO"
    assert plan["comparison_limits"]["underlying_provider_request_count"] == "UNKNOWN"
    for controls in plan["provider_controls"].values():
        assert controls["underlying_provider_request_count"] == "UNKNOWN"


def test_prepared_run_without_runner_is_not_an_invocation() -> None:
    codex = FakeCodex(grant=False)
    executor = runner_module.CodexExecutor(
        launcher=LAUNCHER, source_codex_home=Path("/nonexistent"), prepare=codex.prepare
    )
    plan = build_plan((CODEX,), SUITE[:1], 1)
    (result,) = asyncio.run(run_plan(plan, {CODEX: executor}))
    assert result.failure_detail == "RELEASE_NOT_GRANTED"
    assert result.provider_invocation_started is False
    assert runner_module.invocations_started([result]) == 0


def test_codex_evaluate_counts_once() -> None:
    codex = FakeCodex()
    executor = runner_module.CodexExecutor(
        launcher=LAUNCHER, source_codex_home=Path("/nonexistent"), prepare=codex.prepare
    )
    plan = build_plan((CODEX,), SUITE[:1], 1)
    results = asyncio.run(run_plan(plan, {CODEX: executor}))
    assert [r.provider_invocation_started for r in results] == [True]
    assert runner_module.invocations_started(results) == 1 == codex.evaluate_calls


def test_anthropic_generate_structured_counts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    anthropic = FakeAnthropic()
    anthropic.install(monkeypatch)
    executor = runner_module.AnthropicExecutor(
        runner_module.anthropic_provider_from({ANTHROPIC_KEY_VARIABLE: SECRET})
    )
    plan = build_plan((ANTHROPIC,), SUITE[:1], 1)
    results = asyncio.run(run_plan(plan, {ANTHROPIC: executor}))
    assert runner_module.invocations_started(results) == 1 == anthropic.calls


def test_exception_inside_a_started_call_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(request: ReasoningRequest[Any]) -> ReasoningResult[Any]:
        raise RuntimeError("connection dropped mid-call")

    anthropic = FakeAnthropic(respond=explode)
    anthropic.install(monkeypatch)
    executor = runner_module.AnthropicExecutor(
        runner_module.anthropic_provider_from({ANTHROPIC_KEY_VARIABLE: SECRET})
    )
    codex = FakeCodex(respond=lambda request: (_ for _ in ()).throw(OSError("pipe")))
    codex_executor = runner_module.CodexExecutor(
        launcher=LAUNCHER, source_codex_home=Path("/nonexistent"), prepare=codex.prepare
    )
    plan = build_plan((CODEX, ANTHROPIC), SUITE[:1], 1)
    results = asyncio.run(run_plan(plan, {CODEX: codex_executor, ANTHROPIC: executor}))
    assert [r.failure for r in results] == ["UNCLASSIFIED_EXCEPTION"] * 2
    assert [r.provider_invocation_started for r in results] == [True, True]


def test_exception_before_the_call_is_not_counted() -> None:
    @asynccontextmanager
    async def broken_prepare(**_kwargs: object) -> AsyncIterator[FakePrepared]:
        raise OSError("could not build the environment")
        yield FakePrepared(None)  # pragma: no cover

    executor = runner_module.CodexExecutor(
        launcher=LAUNCHER, source_codex_home=Path("/nonexistent"), prepare=broken_prepare
    )
    plan = build_plan((CODEX,), SUITE[:1], 1)
    (result,) = asyncio.run(run_plan(plan, {CODEX: executor}))
    assert result.failure == "UNCLASSIFIED_EXCEPTION"
    assert result.provider_invocation_started is False


def test_not_run_samples_are_not_counted() -> None:
    async def codex_exec(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        mark.started = True
        return normalize_codex(
            planned, rejected(EvaluationFailure.CLEANUP_INCOMPLETE, "GROUP_NOT_EMPTY")
        )

    async def anthropic_exec(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        raise AssertionError("must not run after a halt")

    plan = build_plan((CODEX, ANTHROPIC), SUITE[:2], 1)
    results = asyncio.run(run_plan(plan, {CODEX: codex_exec, ANTHROPIC: anthropic_exec}))
    assert [r.status for r in results][1:] == [SampleStatus.NOT_RUN] * 3
    assert runner_module.invocations_started(results) == 1


def test_codex_domain_rejection_stays_output_contract_after_invocation() -> None:
    async def codex_exec(planned: PlannedSample, mark: InvocationMark) -> SampleResult:
        mark.started = True
        return normalize_codex(
            planned, rejected(EvaluationFailure.OUTPUT_DOMAIN_INVALID, "CONTRADICTED_VALUE")
        )

    plan = build_plan((CODEX,), SUITE[:1], 1)
    (result,) = asyncio.run(run_plan(plan, {CODEX: codex_exec}))
    assert result.failure_kind is FailureKind.OUTPUT_CONTRACT
    assert result.domain_reason == "CONTRADICTED_VALUE"
    assert result.provider_invocation_started is True
