"""End-to-end outcomes of one attempt, driven by a fake process.

Scope, stated plainly: these tests exercise our argument building, our process
handling and our validation. They do not demonstrate the real CLI's tool
surface, its filesystem isolation, or how subscription usage is metered. A fake
process cannot show any of that, and a green run here is not evidence for it.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from src.agents.orbit.models import OrbitAssessment, OrbitClassification
from src.evaluation.codex.command import ALLOWED_ENVIRONMENT_KEYS
from src.evaluation.codex.models import (
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationRejected,
    OutputLimits,
)
from tests.evaluation.conftest import LAUNCHER_INJECTED_ENVIRONMENT_KEYS, Probe


class FreeForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bag: dict[str, object]


async def test_a_valid_answer_is_accepted_and_locally_validated(probe: Probe) -> None:
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert isinstance(outcome.output, OrbitAssessment)
    assert outcome.output.classification is OrbitClassification.INTERESTING
    assert outcome.output.pair_id == probe.task_input.candidate.pair_id
    assert outcome.usage.input_tokens == 1200
    assert outcome.thread_id == "11111111-2222-3333-4444-555555555555"
    assert outcome.cleanup.complete is True
    assert outcome.wall_clock_ms >= 0


async def test_configured_values_are_never_passed_off_as_reported_ones(probe: Probe) -> None:
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert outcome.configuration.configured_model == "gpt-5.6-sol"
    assert outcome.configuration.configured_effort == "low"
    # The documented event contract carries neither, so both stay absent.
    assert outcome.configuration.reported_model is None
    assert outcome.configuration.reported_effort is None


async def test_only_the_last_message_before_the_turn_ends_counts(probe: Probe) -> None:
    probe.scenario("two_messages")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)


async def test_missing_usage_stays_none_without_failing_the_attempt(probe: Probe) -> None:
    probe.scenario("no_usage")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert outcome.usage.input_tokens is None
    assert outcome.usage.output_tokens is None


async def test_domain_contradiction_never_yields_a_completed_result(probe: Probe) -> None:
    probe.scenario("success", pair_id="a-different-pair")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.OUTPUT_DOMAIN_INVALID
    assert outcome.detail_code == "MARKET_MISMATCH"


async def test_citing_an_observation_that_was_never_shown_is_refused(probe: Probe) -> None:
    probe.scenario("success", observation_ids=["ffffffff-0000-4000-8000-000000000009"])
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.detail_code == "UNKNOWN_OBSERVATION_REFERENCE"


async def test_schema_conformant_nonsense_is_refused_locally(probe: Probe) -> None:
    probe.scenario("success", assessment_overrides={"classification": "MAYBE"})
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.OUTPUT_SCHEMA_MISMATCH


async def test_a_non_json_answer_is_refused(probe: Probe) -> None:
    probe.scenario("not_json")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.OUTPUT_NOT_JSON


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("turn_failed", EvaluationFailure.TURN_FAILED),
        ("no_final_message", EvaluationFailure.NO_FINAL_MESSAGE),
        ("message_without_turn_end", EvaluationFailure.INCONSISTENT_COMPLETION),
        ("item_before_turn", EvaluationFailure.EVENT_STREAM_INVALID),
        ("corrupt_line", EvaluationFailure.EVENT_STREAM_INVALID),
    ],
)
async def test_broken_event_sequences_are_refused(
    probe: Probe, scenario: str, failure: EvaluationFailure
) -> None:
    probe.scenario(scenario)
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is failure


async def test_a_failing_exit_contradicts_a_completed_turn(probe: Probe) -> None:
    probe.scenario("exit_nonzero")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.INCONSISTENT_COMPLETION
    assert outcome.detail_code == "EXIT_3"


async def test_an_oversized_answer_hits_the_parser_budget(probe: Probe) -> None:
    probe.scenario("huge_message")
    outcome = await probe.client().evaluate(
        probe.request(limits=OutputLimits(max_line_bytes=400_000, max_final_message_bytes=1024))
    )
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.PARSER_BUDGET_EXCEEDED


async def test_observed_tool_activity_is_recorded_when_it_happens(probe: Probe) -> None:
    probe.scenario("tool_activity")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    activity = {item.item_type: item.count for item in outcome.observed_tool_activity}
    assert activity == {"command_execution": 1, "web_search": 1}


async def test_a_quiet_stream_is_not_evidence_that_no_tool_was_offered(probe: Probe) -> None:
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    # Nothing was used. Which tools were *offered* remains unknown here.
    assert outcome.observed_tool_activity == ()


async def test_a_second_attempt_is_refused_rather_than_retried(probe: Probe) -> None:
    probe.scenario("success")
    client = probe.client()
    assert isinstance(await client.evaluate(probe.request()), EvaluationCompleted)
    second = await client.evaluate(probe.request())
    assert isinstance(second, EvaluationRejected)
    assert second.detail_code == "EXEC_BUDGET_EXHAUSTED"
    assert client.exec_starts == 1


async def test_an_unsupported_effort_is_refused_not_remapped(probe: Probe) -> None:
    probe.scenario("success")
    outcome = await probe.client(effort="max").evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.EFFORT_NOT_SUPPORTED
    assert outcome.detail_code == "max"


async def test_an_unsupported_cli_version_never_starts_a_process(probe: Probe) -> None:
    probe.scenario("success")
    client = probe.client(cli_version="0.155.1")
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.CLI_VERSION_UNSUPPORTED
    assert client.exec_starts == 0


async def test_an_inexpressible_output_model_never_starts_a_process(probe: Probe) -> None:
    probe.scenario("success")
    client = probe.client()
    outcome = await client.evaluate(
        probe.request(output_model=FreeForm, domain_validator=lambda _output: None)
    )
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.SCHEMA_UNSUPPORTED
    assert client.exec_starts == 0


async def test_the_login_probe_is_counted_apart_from_the_attempt(probe: Probe) -> None:
    probe.scenario("success", login="login_status_chatgpt")
    client = probe.client(run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert client.preflight_starts == 1
    assert client.exec_starts == 1


@pytest.mark.parametrize("login", ["login_status_api_key", "login_status_failure"])
async def test_without_a_chatgpt_session_no_attempt_is_started(probe: Probe, login: str) -> None:
    probe.scenario("success", login=login)
    client = probe.client(run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.PREFLIGHT_FAILED
    assert client.exec_starts == 0


async def test_the_child_inherits_no_credential_from_the_parent(probe: Probe) -> None:
    recorded = probe.workspace.parent / "child-env.json"
    probe.scenario("record_environment", environment_out=str(recorded))
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)

    seen = json.loads(Path(recorded).read_text(encoding="utf-8"))  # noqa: ASYNC240
    # Everything present is either a key the harness passed or a variable the
    # operating system and the test's own shell shim add. Nothing else gets in.
    assert set(seen) - LAUNCHER_INJECTED_ENVIRONMENT_KEYS == set(ALLOWED_ENVIRONMENT_KEYS)
    for banned in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
        "DATABASE_URL",
        "OPENAI_BASE_URL",
    ):
        assert banned not in seen
    assert seen["CODEX_HOME"] == str(probe.codex_home)
