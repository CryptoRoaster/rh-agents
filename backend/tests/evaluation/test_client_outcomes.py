"""End-to-end outcomes of one attempt, driven by a fake process.

Scope, stated plainly: these tests exercise our argument building, our process
handling and our validation. They do not demonstrate the real CLI's tool
surface, its filesystem isolation, or how subscription usage is metered. A fake
process cannot show any of that, and a green run here is not evidence for it.
"""

import json
import time
from pathlib import Path
from unittest import mock

import pytest
from pydantic import BaseModel, ConfigDict

from src.agents.orbit.models import OrbitAssessment, OrbitClassification
from src.evaluation.codex import catalog as catalog_module
from src.evaluation.codex import client as client_module
from src.evaluation.codex.command import ALLOWED_ENVIRONMENT_KEYS
from src.evaluation.codex.models import (
    EvaluationCompleted,
    EvaluationFailure,
    EvaluationRejected,
    OutputLimits,
)
from tests.evaluation.conftest import (
    LAUNCHER_INJECTED_ENVIRONMENT_KEYS,
    Probe,
    orbit_domain_validator,
)


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
    assert outcome.configuration.configured_model == "gpt-5.4"
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


async def test_the_launcher_is_asked_which_build_it_is(probe: Probe) -> None:
    probe.scenario("success")
    client = probe.client()
    assert isinstance(await client.evaluate(probe.request()), EvaluationCompleted)
    # The version probe is its own process with its own counter.
    assert client.version_starts == 1
    assert client.exec_starts == 1


async def test_a_launcher_reporting_another_build_never_runs_an_attempt(
    probe: Probe,
) -> None:
    probe.scenario("success", version="codex-cli 0.155.1")
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.CLI_VERSION_UNSUPPORTED
    # The measured version is reported, not the one we hoped for.
    assert outcome.detail_code == "0.155.1"
    assert client.version_starts == 1
    assert client.exec_starts == 0


async def test_a_launcher_that_reports_no_version_never_runs_an_attempt(
    probe: Probe,
) -> None:
    probe.scenario("success", version=None)
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.VERSION_CHECK_FAILED
    assert outcome.detail_code == "VERSION_NOT_REPORTED"
    assert client.exec_starts == 0


async def test_a_failing_version_probe_never_runs_an_attempt(probe: Probe) -> None:
    probe.scenario("success", version="__fail__")
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.VERSION_CHECK_FAILED
    assert outcome.detail_code == "EXIT_2"
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


async def test_a_validator_that_overruns_the_deadline_is_never_a_late_success(
    probe: Probe,
) -> None:
    """The regression: synchronous validation cannot be cancelled, only caught.

    `time.sleep` in a validator blocks the event loop outright. Nothing can
    interrupt it, so the only honest handling is to notice the overrun once it
    returns and refuse the result that arrived too late.
    """
    probe.scenario("success")
    bound = orbit_domain_validator(probe.task_input)

    def slow(output: OrbitAssessment) -> None:
        bound(output)
        time.sleep(2.0)

    outcome = await probe.client().evaluate(
        probe.request(deadline_seconds=2.0, cleanup_reserve_seconds=0.5, domain_validator=slow)
    )
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.DEADLINE_EXCEEDED
    assert outcome.detail_code == "VALIDATION_OVERRAN"


async def test_the_same_budget_accepts_a_validator_that_returns_in_time(
    probe: Probe,
) -> None:
    probe.scenario("success")
    outcome = await probe.client().evaluate(
        probe.request(deadline_seconds=2.0, cleanup_reserve_seconds=0.5)
    )
    assert isinstance(outcome, EvaluationCompleted)


async def test_the_version_probe_runs_with_the_same_scrubbed_environment(
    probe: Probe,
) -> None:
    recorded = probe.workspace.parent / "version-env.json"
    probe.scenario("success", version_environment_out=str(recorded))
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)

    seen = json.loads(Path(recorded).read_text(encoding="utf-8"))  # noqa: ASYNC240
    assert set(seen) - LAUNCHER_INJECTED_ENVIRONMENT_KEYS == set(ALLOWED_ENVIRONMENT_KEYS)
    for banned in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        assert banned not in seen


async def test_the_login_probe_reads_the_channel_the_cli_actually_uses(
    probe: Probe,
) -> None:
    """The regression: 0.153.4 answers on stderr, with stdout left empty.

    `run_login_status` reports every outcome with `eprintln!`, so a probe that
    watched stdout alone would see nothing and reject a perfectly valid ChatGPT
    session. The exit code cannot stand in for the marker either -- an API-key
    session also exits 0.
    """
    probe.scenario("success", login="login_status_chatgpt")
    client = probe.client(run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)
    assert client.preflight_starts == 1
    assert client.exec_starts == 1


@pytest.mark.parametrize(
    "login",
    [
        "login_status_api_key",
        "login_status_access_token",
        "login_status_failure",
        # A longer status beginning with the marker: accepted by a substring
        # test, rejected by an exact line match.
        "login_status_chatgpt_prefixed",
        # The marker exists only if the two channels are glued together, which
        # is why they are matched separately.
        "login_status_split_channels",
    ],
)
async def test_no_other_login_mode_counts_as_a_chatgpt_session(probe: Probe, login: str) -> None:
    probe.scenario("success", login=login)
    client = probe.client(run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.PREFLIGHT_FAILED
    assert client.exec_starts == 0


async def test_a_slow_schema_validation_does_not_fund_a_domain_validator(
    probe: Probe,
) -> None:
    """The deadline stops at the first boundary where control comes back."""
    probe.scenario("success")
    started = False

    class SlowAssessment(OrbitAssessment):
        @classmethod
        def model_validate(cls, obj: object, **kwargs: object) -> "SlowAssessment":
            time.sleep(2.0)
            return super().model_validate(obj, **kwargs)  # type: ignore[no-any-return,arg-type]

    def sentinel(_output: OrbitAssessment) -> None:
        nonlocal started
        started = True

    outcome = await probe.client().evaluate(
        probe.request(
            output_model=SlowAssessment,
            domain_validator=sentinel,
            deadline_seconds=2.0,
            cleanup_reserve_seconds=0.5,
        )
    )
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.DEADLINE_EXCEEDED
    assert outcome.detail_code == "SCHEMA_VALIDATION_OVERRAN"
    assert started is False


async def test_the_pinned_catalog_decides_not_the_bundled_dump(probe: Probe) -> None:
    """The regression for the gap between inspected and effective catalog.

    The fake's `debug models --bundled` output says `tool_mode:
    "code_mode_only"`; the pinned catalog says null. A root session resolves
    ModelInfo through the ModelsManager, so judging the bundled dump would have
    judged a catalog the turn never uses. This run must follow the pinned file.
    """
    probe.scenario("success")
    probe.catalog(tool_mode=None)
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)


async def test_a_pinned_catalog_with_code_mode_never_reaches_exec(probe: Probe) -> None:
    """And the other direction, which is the one that matters.

    Bundled could say anything; what decides is the file `codex exec` is pinned
    to. When that file declares a tool mode, no attempt starts.
    """
    probe.scenario("success")
    probe.catalog(tool_mode="code_mode_only")
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.TOOL_SURFACE_UNSUPPORTED
    assert outcome.detail_code == "TOOL_MODE_CODE_MODE_ONLY"
    assert client.exec_starts == 0
    assert client.version_starts == 0


async def test_replacing_the_catalog_after_the_check_changes_nothing(
    probe: Probe,
) -> None:
    """The regression for "same path is not same bytes".

    The guard takes its snapshot, the path is then atomically replaced with a
    catalog declaring `code_mode_only`, and the attempt still runs -- because
    the child inherits the descriptor the guard read, not the directory entry.
    A pathname pin would have handed the replacement to `codex exec`.
    """
    probe.scenario("success")
    probe.catalog(tool_mode=None)
    client = probe.client()

    original_open = catalog_module.open_catalog
    swapped: dict[str, bool] = {}

    def open_then_swap(path: Path) -> catalog_module.OpenCatalog | None:
        opened = original_open(path)
        replacement = path.with_suffix(".swap")
        replacement.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "tool_mode": "code_mode_only",
                            "apply_patch_tool_type": "freeform",
                            "experimental_supported_tools": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        replacement.replace(path)
        swapped["done"] = True
        return opened

    with (
        mock.patch.object(catalog_module, "open_catalog", open_then_swap),
        mock.patch.object(client_module, "open_catalog", open_then_swap),
    ):
        outcome = await client.evaluate(probe.request())

    assert swapped.get("done") is True
    assert "code_mode_only" in probe.model_catalog_path.read_text(encoding="utf-8")
    assert isinstance(outcome, EvaluationCompleted)


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"tool_mode": "code_mode_only"}, "TOOL_MODE_CODE_MODE_ONLY"),
        ({"tool_mode": "direct"}, "TOOL_MODE_DIRECT"),
        ({"experimental_supported_tools": ["clock"]}, "EXPERIMENTAL_TOOL_CLOCK"),
        ({"apply_patch_tool_type": "something_new"}, "APPLY_PATCH_SOMETHING_NEW"),
        ({"slug": "another-model"}, "MODEL_NOT_IN_CATALOG"),
        ({"tool_mode": {"unexpected": "shape"}}, "TOOL_MODE_MALFORMED"),
        ({"apply_patch_tool_type": 7}, "APPLY_PATCH_MALFORMED"),
        ({"experimental_supported_tools": "clock"}, "EXPERIMENTAL_TOOLS_MALFORMED"),
    ],
)
async def test_a_wider_or_malformed_surface_never_runs_an_attempt(
    probe: Probe, overrides: dict[str, object], detail: str
) -> None:
    """Fail-closed: an unwanted surface, an unknown value and a wrong type all refuse.

    A wrong type is not the same fact as `null`. Reading `{"unexpected":
    "shape"}` as "absent" would turn a parsing accident into a permission.
    """
    probe.scenario("success")
    probe.catalog(**overrides)
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.TOOL_SURFACE_UNSUPPORTED
    assert outcome.detail_code == detail
    assert client.exec_starts == 0


async def test_an_unreadable_pinned_catalog_never_runs_an_attempt(probe: Probe) -> None:
    probe.scenario("success")
    probe.model_catalog_path.unlink()
    client = probe.client()
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.detail_code == "CATALOG_UNREADABLE"
    assert client.exec_starts == 0
