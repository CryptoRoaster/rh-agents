"""The Codex adapter, offline: a fake `prepare_real_run`, never a real CLI."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from src.codex_reasoning.provider import (
    CLEANUP_RESERVE_SECONDS,
    FAILURE_CATEGORIES,
    CodexReasoningProvider,
    codex_provider_from,
)
from src.core.config import Settings
from src.evaluation.codex.models import (
    AttemptConfiguration,
    CleanupReport,
    CodexLauncher,
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationRejected,
    EvaluationRequest,
    EvaluationUsage,
    LauncherKind,
    ObservedToolActivity,
)
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure, ReasoningRequest

LAUNCHER = CodexLauncher(kind=LauncherKind.FAKE_EXECUTABLE, executable=Path("/nonexistent/codex"))
HOME = Path("/nonexistent/.codex")


class Answer(BaseModel):
    verdict: str


def completed(**overrides: Any) -> EvaluationCompleted[Answer]:
    fields: dict[str, Any] = {
        "output": Answer(verdict="ok"),
        "configuration": AttemptConfiguration(
            configured_model="gpt-5.5", configured_effort="high", reported_model="gpt-5.5"
        ),
        "usage": EvaluationUsage(input_tokens=6000, cached_input_tokens=4000, output_tokens=280),
        "observed_tool_activity": (),
        "thread_id": "thread-local-0001",
        "wall_clock_ms": 9000,
        "cleanup": CleanupReport(),
    }
    fields.update(overrides)
    return EvaluationCompleted(**fields)


def rejected(reason: EvaluationFailure) -> EvaluationRejected:
    return EvaluationRejected(
        reason=reason, detail_code="DETAIL", wall_clock_ms=100, cleanup=CleanupReport()
    )


@dataclass
class Runner:
    respond: Callable[[EvaluationRequest[Any]], Any]
    requests: list[EvaluationRequest[Any]] = field(default_factory=list)

    async def evaluate(self, request: EvaluationRequest[Any]) -> Any:
        self.requests.append(request)
        return self.respond(request)


@dataclass
class Prepared:
    runner: Runner | None


@dataclass
class FakePrepare:
    """Stands in for `prepare_real_run` and records how it was entered."""

    respond: Callable[[EvaluationRequest[Any]], Any] = lambda _: completed()
    grant: bool = True
    fail: bool = False
    entries: list[dict[str, Any]] = field(default_factory=list)
    runners: list[Runner] = field(default_factory=list)
    exits: int = 0

    @asynccontextmanager
    async def __call__(self, **kwargs: Any) -> AsyncIterator[Prepared]:
        self.entries.append(kwargs)
        if self.fail:
            raise PermissionError("/Users/someone/.codex/auth.json")
        runner = Runner(self.respond) if self.grant else None
        if runner is not None:
            self.runners.append(runner)
        try:
            yield Prepared(runner)
        finally:
            self.exits += 1


class Clock:
    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


def request(timeout: float = 180) -> ReasoningRequest[Answer]:
    return ReasoningRequest(
        instructions="Assess.",
        data={"price": Decimal("0.000123"), "nested": [{"b": 1, "a": Decimal("2.50")}]},
        output_model=Answer,
        max_output_tokens=1024,
        timeout_seconds=timeout,
    )


def provider(prepare: FakePrepare, clock: Clock | None = None) -> CodexReasoningProvider:
    return CodexReasoningProvider(
        launcher=LAUNCHER,
        source_codex_home=HOME,
        effort="high",
        prepare=prepare,
        monotonic=clock or Clock(0.0),
    )


async def test_a_completed_turn_becomes_a_reasoning_result() -> None:
    prepare = FakePrepare()
    result = await provider(prepare, Clock(0.0, 2.0, 11.5)).generate_structured(request())

    assert result.output == Answer(verdict="ok")
    assert result.model.provider == "codex"
    assert result.model.model == "gpt-5.5"
    assert result.model.effort == "high"
    assert result.usage.input_tokens == 6000
    assert result.usage.output_tokens == 280
    assert result.usage.latency_ms == 11500
    # A thread id is local; it is never published as a provider request id.
    assert result.usage.provider_request_id is None
    assert prepare.entries == [{"launcher": LAUNCHER, "source_codex_home": HOME, "effort": "high"}]
    assert prepare.exits == 1


async def test_the_turn_gets_the_same_channels_and_what_is_left_of_the_budget() -> None:
    prepare = FakePrepare()
    await provider(prepare, Clock(0.0, 4.0, 20.0)).generate_structured(request(timeout=120))

    sent = prepare.runners[0].requests[0]
    assert sent.instructions == "Assess."
    # Money never passes through a float on its way to the CLI.
    assert sent.data == {"nested": [{"a": "2.5", "b": 1}], "price": "0.000123"}
    assert sent.output_model is Answer
    assert sent.deadline_seconds == 116.0
    assert sent.cleanup_reserve_seconds == CLEANUP_RESERVE_SECONDS
    # The caller validates its own domain; the harness accepts any valid schema.
    sent.domain_validator(Answer(verdict="anything"))


async def test_every_call_prepares_a_fresh_environment() -> None:
    prepare = FakePrepare()
    codex = provider(prepare)
    await codex.generate_structured(request())
    await codex.generate_structured(request())

    assert len(prepare.entries) == 2
    assert [len(runner.requests) for runner in prepare.runners] == [1, 1]
    assert prepare.exits == 2


async def test_a_refused_release_makes_no_turn() -> None:
    prepare = FakePrepare(grant=False)
    with pytest.raises(ReasoningFailure) as raised:
        await provider(prepare).generate_structured(request())

    assert raised.value.category is ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED
    assert raised.value.reason_code == "CODEX_RELEASE_NOT_GRANTED"
    assert prepare.runners == []
    assert prepare.exits == 1


async def test_a_preflight_that_ate_the_budget_starts_no_turn() -> None:
    prepare = FakePrepare()
    with pytest.raises(ReasoningFailure) as raised:
        await provider(prepare, Clock(0.0, 50.0)).generate_structured(request(timeout=60))

    assert raised.value.category is ReasoningErrorCategory.PROVIDER_TIMEOUT
    assert raised.value.reason_code == "CODEX_PREFLIGHT_TOO_SLOW"
    assert prepare.runners[0].requests == []


async def test_an_environment_failure_leaks_no_path() -> None:
    with pytest.raises(ReasoningFailure) as raised:
        await provider(FakePrepare(fail=True)).generate_structured(request())

    assert raised.value.category is ReasoningErrorCategory.PROVIDER_UNAVAILABLE
    assert raised.value.reason_code == "CODEX_ENVIRONMENT_FAILED"
    assert "auth.json" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_every_harness_failure_has_a_category() -> None:
    assert set(FAILURE_CATEGORIES) == set(EvaluationFailure)


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (EvaluationFailure.DEADLINE_EXCEEDED, ReasoningErrorCategory.PROVIDER_TIMEOUT),
        (EvaluationFailure.OUTPUT_SCHEMA_MISMATCH, ReasoningErrorCategory.INVALID_MODEL_OUTPUT),
        (EvaluationFailure.TURN_FAILED, ReasoningErrorCategory.PROVIDER_UNAVAILABLE),
        (EvaluationFailure.PREFLIGHT_FAILED, ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED),
        (EvaluationFailure.SCHEMA_UNSUPPORTED, ReasoningErrorCategory.PROVIDER_REJECTED_REQUEST),
    ],
)
async def test_a_rejection_becomes_a_typed_failure(
    reason: EvaluationFailure, category: ReasoningErrorCategory
) -> None:
    prepare = FakePrepare(respond=lambda _: rejected(reason))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(prepare).generate_structured(request())

    assert raised.value.category is category
    assert raised.value.reason_code == f"CODEX_{reason.value}"


async def test_observed_tool_activity_refuses_even_a_valid_answer() -> None:
    activity = (ObservedToolActivity(item_type="command_execution", count=1),)
    prepare = FakePrepare(respond=lambda _: completed(observed_tool_activity=activity))
    with pytest.raises(ReasoningFailure) as raised:
        await provider(prepare).generate_structured(request())

    assert raised.value.category is ReasoningErrorCategory.INVALID_MODEL_OUTPUT
    assert raised.value.reason_code == "CODEX_TOOL_ACTIVITY_OBSERVED"


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg:///rh_agents_test?host=/tmp")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_no_cli_means_no_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = settings(
        monkeypatch, REASONING_PROVIDER="codex", CODEX_EXECUTABLE=str(tmp_path / "missing")
    )
    assert codex_provider_from(configured) is None


def test_the_configured_cli_and_home_are_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The shape an npm install has: bin/codex.js beside a vendored binary.
    package = tmp_path / "codex"
    (package / "bin").mkdir(parents=True)
    entry = package / "bin" / "codex.js"
    entry.write_text("")
    binary = package / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    binary.chmod(0o755)
    configured = settings(
        monkeypatch,
        REASONING_PROVIDER="codex",
        CODEX_EXECUTABLE=str(entry),
        CODEX_HOME=str(tmp_path / "home"),
        REASONING_EFFORT="medium",
    )

    codex = codex_provider_from(configured)

    assert codex is not None
    assert codex.launcher.kind is LauncherKind.PLATFORM_BINARY
    assert codex.launcher.executable == binary
    assert codex.source_codex_home == tmp_path / "home"
    assert codex.effort == "medium"
