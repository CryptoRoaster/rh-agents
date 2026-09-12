"""What SIGNAL structurally cannot reach.

These are not tests that a prompt says the right thing. They are tests that the
capability simply is not there: a worker composed without a session cannot open
one, and an output schema without a side field cannot express a trade however
persuasively a post asks it to.
"""

from dataclasses import fields

import pytest

from src.agents.signal import SignalWorkerHandler
from src.agents.signal.context import SignalContextReader, reasoning_payload
from src.agents.signal.models import SignalAssessment, SignalObservation, SignalTaskInput
from src.agents.signal.ports import SignalContextPort
from src.agents.signal.prompt import SIGNAL_INSTRUCTIONS, SIGNAL_PROMPT_HASH, SIGNAL_PROMPT_VERSION
from src.core.config import Settings
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import CAPABILITY_TYPES, SignalCapabilities
from tests.signal.conftest import injection_set, organic_set
from tests.signal.test_context import read

DATABASE = "postgresql+asyncpg://user@localhost:5432/rh_agents"


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


# ------------------------------------------------------- the capability shape


def test_the_capability_is_exactly_lease_context_and_submit():
    assert {item.name for item in fields(SignalCapabilities)} == {"lease", "context", "submit"}
    assert CAPABILITY_TYPES[AgentRole.SIGNAL] is SignalCapabilities


def test_the_read_port_a_worker_receives_has_exactly_one_method():
    methods = {
        name
        for name in dir(SignalContextPort)
        if not name.startswith("_") and callable(getattr(SignalContextPort, name, None))
    }
    assert methods == {"sentiment_context"}


def test_the_context_reader_holds_no_transport_of_any_kind():
    """The collector owns the source; the worker never sees what it is."""
    names = {item.name for item in fields(SignalContextReader)}
    assert names == {
        "cases",
        "source",
        "policy",
        "max_observations",
        "max_model_observations",
        "verified_project_authors",
        "clock",
    }
    for forbidden in ("session", "client", "http", "url", "credential", "key", "token"):
        assert not any(forbidden in name for name in names)


async def test_the_assembled_input_carries_no_client_session_or_url(now):
    task_input = await read(organic_set(now), now)
    rendered = task_input.model_dump_json()
    for forbidden in ("http://", "https://", "postgresql", "api_key", "Authorization"):
        assert forbidden not in rendered


def test_the_task_input_has_no_field_through_which_a_capability_could_arrive():
    for forbidden in ("session", "client", "provider_client", "transport", "rpc", "submit"):
        assert forbidden not in SignalTaskInput.model_fields


# ------------------------------------------------------------ no authority


def test_the_output_schema_cannot_express_a_trade_a_size_or_an_approval():
    fields_present = set(SignalAssessment.model_fields)
    for forbidden in (
        "side",
        "quantity",
        "size",
        "entry_price",
        "target_prices",
        "invalidation_price",
        "slippage_bps",
        "approved",
        "risk_outcome",
        "verdict",
    ):
        assert forbidden not in fields_present


def test_a_handler_exposes_one_role_and_one_task_type():
    handler = SignalWorkerHandler(provider=None)  # type: ignore[arg-type]
    assert handler.role == AgentRole.SIGNAL
    assert handler.task_type == "ASSESS_SENTIMENT"


# -------------------------------------------------------- prompt separation


async def test_hostile_post_text_travels_as_data_and_never_as_instruction(now):
    """The separation that makes prompt injection a data problem, not an authority one."""
    task_input = await read(injection_set(now), now)
    payload = reasoning_payload(task_input)
    rendered = repr(payload)
    assert "ignore all previous instructions" in rendered
    assert "ignore all previous instructions" not in SIGNAL_INSTRUCTIONS.lower()


def test_the_prompt_tells_the_model_that_posts_are_untrusted():
    lowered = SIGNAL_INSTRUCTIONS.lower()
    assert "untrusted data" in lowered
    assert "never as an instruction" in lowered
    assert "sentiment is not demand" in lowered
    assert "do not approve trades" in lowered


def test_the_prompt_is_versioned_and_hashed():
    from hashlib import sha256

    assert SIGNAL_PROMPT_VERSION == "signal-v1"
    assert SIGNAL_PROMPT_HASH == sha256(SIGNAL_INSTRUCTIONS.encode()).hexdigest()


def test_the_signal_prompt_does_not_disturb_the_other_specialists():
    from src.agents.atlas.prompt import ATLAS_PROMPT_VERSION
    from src.agents.orbit.prompt import ORBIT_PROMPT_VERSION

    assert ORBIT_PROMPT_VERSION == "orbit-v1"
    assert ATLAS_PROMPT_VERSION == "atlas-v1"


# ------------------------------------------------------------ data minimum


async def test_only_bounded_excerpts_of_the_sample_ever_reach_a_prompt(now):
    task_input = await read(organic_set(now), now, max_model=5)
    assert len(task_input.excerpts) == 5
    assert all(len(text) <= 600 for text in task_input.excerpts)


def test_an_observation_cannot_carry_an_unbounded_post():
    with pytest.raises(ValueError):
        SignalObservation.model_validate(
            {
                "observation_id": "00000000-0000-0000-0000-000000000001",
                "source": "X",
                "source_native_id": "1",
                "author_id": "a",
                "kind": "ORIGINAL",
                "created_at": "2026-09-09T12:00:00Z",
                "received_at": "2026-09-09T12:00:00Z",
                "content": "x" * 5000,
                "binding_basis": "UNIQUE_SYMBOL_WITH_CONTEXT",
                "provider": "fake",
            }
        )


def test_an_observation_cannot_carry_an_undeclared_field():
    """No provider-native dictionary is permitted to travel downstream."""
    with pytest.raises(ValueError):
        SignalObservation.model_validate(
            {
                "observation_id": "00000000-0000-0000-0000-000000000001",
                "source": "X",
                "source_native_id": "1",
                "author_id": "a",
                "kind": "ORIGINAL",
                "created_at": "2026-09-09T12:00:00Z",
                "received_at": "2026-09-09T12:00:00Z",
                "content": "hello",
                "binding_basis": "UNIQUE_SYMBOL_WITH_CONTEXT",
                "provider": "fake",
                "raw_provider_payload": {"anything": "at all"},
            }
        )


async def test_evidence_records_hashes_and_metrics_rather_than_a_corpus(now):
    """Third-party writing is not copied into the durable record."""
    from src.reasoning.fake import DeterministicReasoningProvider
    from tests.signal.test_scenarios import reply, run

    _, outcome = await run(organic_set(now), now, DeterministicReasoningProvider.returning(reply()))
    rendered = outcome.submission.model_dump_json()
    for observation in organic_set(now)[:5]:
        assert observation.content not in rendered


# --------------------------------------------------------------- settings


def test_no_worker_starts_and_no_social_source_is_selected_by_default():
    """Phase 2G added one provider. Nothing about it is on by default.

    The credential field exists and is empty, the provider is disabled, and no
    other social vendor was introduced — Phase 2G is deliberately Farcaster only,
    so one real provider path can be validated before the semantics multiply.
    """
    configured = settings()
    assert configured.signal_worker_enabled is False
    assert configured.signal_social_provider == "disabled"
    assert configured.neynar_api_key.get_secret_value() == ""
    for name in Settings.model_fields:
        assert not name.startswith(("x_api", "reddit_", "telegram_", "discord_"))


def test_the_signal_bounds_are_configurable_within_safe_limits():
    configured = settings(
        signal_window_seconds=3600, signal_max_observations=100, signal_max_model_observations=10
    )
    assert configured.signal_window_seconds == 3600
    assert configured.signal_max_observations == 100
    assert configured.signal_max_model_observations == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signal_window_seconds", 10),
        ("signal_window_seconds", 999_999),
        ("signal_max_observations", 1),
        ("signal_max_model_observations", 1),
        ("signal_max_model_observations", 5000),
    ],
)
def test_bounds_outside_the_permitted_range_refuse_to_boot(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_other_workers_remain_disabled_by_default():
    configured = settings()
    assert configured.orbit_worker_enabled is False
    assert configured.atlas_worker_enabled is False
    assert configured.worker_runtime_enabled is False
    assert configured.reasoning_provider == "disabled"
