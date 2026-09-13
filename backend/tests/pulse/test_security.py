"""What PULSE structurally cannot reach, and cannot become.

The headline is the absence of a model. Every other specialist has a reasoning
provider because every other specialist interprets something; PULSE compares two
Decimals. A model here would add cost, latency and variance to a question with
one correct answer, and would make it impossible to say afterwards why the
system acted — so the dependency is not merely unused, it is not there.
"""

import ast
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from src.agents.pulse import PulseContextPort, PulseWorkerHandler
from src.agents.pulse.models import PulseTaskInput, TriggerEvaluation, WatchedTrigger
from src.core.config import Settings
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import CAPABILITY_TYPES, PulseCapabilities

DATABASE = "postgresql+asyncpg://user@localhost:5432/rh_agents"

PACKAGE = Path("src/agents/pulse")

EXECUTION_AUTHORITY = (
    "wallet",
    "signer",
    "private_key",
    "executor",
    "broadcast",
    "ledger",
    "sentinel",
    "riskbinding",
    "risk_binding",
)


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


def package_source() -> str:
    return "\n".join(path.read_text() for path in sorted(PACKAGE.glob("*.py")))


def package_imports() -> set[str]:
    """Every module this package imports, from the syntax tree rather than the text.

    The docstrings in this package name the things it must not reach, precisely
    because it must not reach them. A substring search over the source would
    therefore fail on the prose that states the guarantee — so the guarantee is
    checked against the import graph itself.
    """
    modules: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def package_identifiers() -> set[str]:
    """Every name and attribute the package's code actually uses."""
    names: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.rsplit(".", 1)[-1])
    return names


def tracked(symbol: str, *paths: str) -> str:
    return subprocess.run(
        ["git", "grep", "-il", symbol, "--", *paths],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.strip()


# ------------------------------------------------------------- no model


@pytest.mark.parametrize(
    "forbidden", ["reasoning", "anthropic", "openai", "llm", "prompt", "provider"]
)
def test_the_package_imports_no_reasoning_dependency(forbidden):
    """Not unused — absent. A monitor that could call a model eventually would."""
    modules = package_imports()
    assert modules
    assert not any(forbidden in module.lower() for module in modules), modules


@pytest.mark.parametrize(
    "forbidden",
    [
        "ReasoningProvider",
        "ReasoningRequest",
        "generate_structured",
        "max_output_tokens",
        "temperature",
        "PROMPT",
    ],
)
def test_the_package_names_no_model_machinery(forbidden):
    assert forbidden not in package_identifiers()


def test_the_handler_needs_nothing_to_be_constructed():
    """Every other specialist takes a provider. This one takes a policy."""
    handler = PulseWorkerHandler()
    assert handler.role == AgentRole.PULSE
    assert {item.name for item in fields(PulseWorkerHandler)} == {"policy"}


def test_there_is_no_prompt_file_at_all():
    assert not (PACKAGE / "prompt.py").exists()


def test_the_evaluation_carries_no_model_provenance():
    """Nothing produced this answer except arithmetic, and the record shows it."""
    for forbidden in ("prompt_version", "prompt_hash", "reasoning_provider", "reasoning_model"):
        assert forbidden not in TriggerEvaluation.model_fields


# ------------------------------------------------------- the capability shape


def test_the_capability_is_exactly_lease_context_and_submit():
    assert {item.name for item in fields(PulseCapabilities)} == {"lease", "context", "submit"}
    assert CAPABILITY_TYPES[AgentRole.PULSE] is PulseCapabilities


def test_the_read_port_a_worker_receives_has_exactly_one_method():
    methods = {
        name
        for name in dir(PulseContextPort)
        if not name.startswith("_") and callable(getattr(PulseContextPort, name, None))
    }
    assert methods == {"trigger_context"}


def test_the_package_cannot_reach_a_provider_or_a_database():
    reachable = {item.lower() for item in package_imports() | package_identifiers()}
    for forbidden in (*EXECUTION_AUTHORITY, "httpx", "sqlalchemy", "geckoterminal", "asyncsession"):
        assert not any(forbidden in item for item in reachable), f"PULSE reaches {forbidden}"


def test_the_package_cannot_reach_rpc_or_chain_pricing():
    """A market price comes from the market layer, never from a chain read."""
    reachable = {item.lower() for item in package_imports() | package_identifiers()}
    for forbidden in ("rpc", "web3", "eth_call", "block_number", "balanceof", "transfer"):
        assert not any(forbidden in item for item in reachable), f"PULSE reaches {forbidden}"


# ------------------------------------------------------------ no authority


@pytest.mark.parametrize("schema", [PulseTaskInput, WatchedTrigger, TriggerEvaluation])
@pytest.mark.parametrize(
    "forbidden",
    [
        "position_size",
        "notional_usd",
        "quantity",
        "slippage_bps",
        "route",
        "venue_preference",
        "risk_outcome",
        "approved",
        "authorization",
        "session",
        "client",
        "rpc",
        "submit",
        "confidence",
    ],
)
def test_no_pulse_schema_has_a_field_for_execution_or_a_capability(schema, forbidden):
    assert forbidden not in schema.model_fields


def test_pulse_cannot_force_a_trade_case_status():
    reachable = package_imports() | package_identifiers()
    for forbidden in ("TradeCaseStatus", "transition_task", "evaluate_trade_case", "record_risk"):
        assert forbidden not in reachable


def test_pulse_submits_one_evidence_type_and_no_other():
    from src.orchestration.worker.policy import authorized_evidence_type
    from src.orchestration.workflow.models import EvidenceType

    assert authorized_evidence_type(AgentRole.PULSE) == EvidenceType.TRIGGER
    reachable = package_imports() | package_identifiers()
    for other in ("SentimentPayload", "OnchainPayload", "LiquidityExecutionPayload"):
        assert other not in reachable


# --------------------------------------------------------------- settings


def test_no_pulse_worker_starts_by_default():
    configured = settings()
    assert configured.pulse_worker_enabled is False
    assert configured.worker_runtime_enabled is False


def test_nothing_wires_a_pulse_worker_at_startup():
    for symbol in ("PulseWorkerHandler", "PulseContextReader", "pulse_worker_enabled"):
        assert tracked(symbol, "backend/src/api/", "backend/src/runtime/") == "", (
            f"{symbol} is reachable from a startup path"
        )


def test_there_is_no_public_way_to_force_a_trigger():
    """No manual trigger endpoint, and no mutation route of any kind."""
    api = "\n".join(path.read_text() for path in sorted(Path("src/api").glob("*.py")))
    for forbidden in ("post(", "put(", "patch(", "delete("):
        assert forbidden not in api.lower()
    for forbidden in ("trigger", "pulse"):
        assert forbidden not in api.lower()


def test_the_other_workers_remain_disabled_by_default():
    configured = settings()
    assert configured.orbit_worker_enabled is False
    assert configured.atlas_worker_enabled is False
    assert configured.signal_worker_enabled is False
    assert configured.vector_worker_enabled is False


def test_the_earlier_specialists_are_untouched():
    for role in (AgentRole.ORBIT, AgentRole.ATLAS, AgentRole.SIGNAL, AgentRole.VECTOR):
        surface = {item.name for item in fields(CAPABILITY_TYPES[role])}
        assert surface == {"lease", "context", "submit"}
