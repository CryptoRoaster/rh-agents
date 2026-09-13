"""What ANCHOR structurally cannot reach, and cannot become.

This is the specialist closest to execution, so the boundary matters more here
than anywhere before it. It sits one step from a signer in the eventual flow and
handles quotes that providers ship with the material to build a transaction —
which makes "it does not use that" insufficient. It must not have it.
"""

import ast
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from src.agents.anchor import AnchorContextPort, AnchorWorkerHandler
from src.agents.anchor.context import AnchorContextReader
from src.agents.anchor.models import (
    AnchorTaskInput,
    ExecutionAssessment,
    QuotedPoint,
)
from src.core.config import Settings
from src.core.models import AgentRole
from src.orchestration.worker.capabilities import CAPABILITY_TYPES, AnchorCapabilities

DATABASE = "postgresql+asyncpg://user@localhost:5432/rh_agents"
PACKAGE = Path("src/agents/anchor")

EXECUTION_AUTHORITY = (
    "wallet",
    "signer",
    "private_key",
    "executor",
    "broadcast",
    "sendtransaction",
    "sendrawtransaction",
    "calldata",
    "ledger",
    "sentinel",
    "riskbinding",
    "risk_binding",
)


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


def package_imports() -> set[str]:
    """Every module the package imports, from the syntax tree rather than the text.

    The docstrings here name the things the package must not reach, precisely
    because it must not reach them, so a substring search over the source would
    fail on the prose that states the guarantee.
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
    """Deterministic by construction, not by intention."""
    modules = package_imports()
    assert modules
    assert not any(forbidden in module.lower() for module in modules), modules


@pytest.mark.parametrize(
    "forbidden",
    ["ReasoningProvider", "ReasoningRequest", "generate_structured", "max_output_tokens", "PROMPT"],
)
def test_the_package_names_no_model_machinery(forbidden):
    assert forbidden not in package_identifiers()


def test_there_is_no_prompt_file_at_all():
    assert not (PACKAGE / "prompt.py").exists()


def test_the_handler_needs_no_provider_to_be_constructed():
    handler = AnchorWorkerHandler()
    assert handler.role == AgentRole.ANCHOR
    assert {item.name for item in fields(AnchorWorkerHandler)} == {"policy", "quote_provider"}


def test_the_assessment_carries_no_model_provenance():
    for forbidden in ("prompt_version", "prompt_hash", "reasoning_provider", "reasoning_model"):
        assert forbidden not in ExecutionAssessment.model_fields


# ------------------------------------------------------- the capability


def test_the_capability_is_exactly_lease_context_and_submit():
    assert {item.name for item in fields(AnchorCapabilities)} == {"lease", "context", "submit"}
    assert CAPABILITY_TYPES[AgentRole.ANCHOR] is AnchorCapabilities


def test_the_read_port_a_worker_receives_has_exactly_one_method():
    methods = {
        name
        for name in dir(AnchorContextPort)
        if not name.startswith("_") and callable(getattr(AnchorContextPort, name, None))
    }
    assert methods == {"execution_context"}


def test_the_worker_never_receives_a_quote_provider():
    """Quotes are obtained by infrastructure; the worker gets the answers."""
    surface = {name for name in dir(AnchorCapabilities) if not name.startswith("_")}
    for forbidden in ("quotes", "provider", "http", "rpc", "client", "kyberswap"):
        assert forbidden not in surface


def test_the_package_cannot_reach_a_provider_a_database_or_a_chain():
    reachable = {item.lower() for item in package_imports() | package_identifiers()}
    for forbidden in (
        *EXECUTION_AUTHORITY,
        "httpx",
        "sqlalchemy",
        "asyncsession",
        "kyberswap",
        "web3",
        "eth_call",
        "eth_sendrawtransaction",
    ):
        assert not any(forbidden in item for item in reachable), f"ANCHOR reaches {forbidden}"


def test_the_package_depends_only_on_quote_contracts():
    """It names the port and the types, never an implementation of either."""
    modules = package_imports()
    assert "src.markets.quotes" in modules
    assert not any("kyberswap" in module for module in modules)
    assert not any("geckoterminal" in module for module in modules)


# ------------------------------------------------------------ no authority


@pytest.mark.parametrize("schema", [AnchorTaskInput, ExecutionAssessment, QuotedPoint])
@pytest.mark.parametrize(
    "forbidden",
    [
        "position_size",
        "notional_usd",
        "quantity",
        "portfolio_fraction",
        "risk_outcome",
        "approved",
        "authorization",
        "cash",
        "exposure",
        "daily_loss",
        "session",
        "client",
        "rpc",
        "submit",
        "calldata",
        "transaction",
    ],
)
def test_no_anchor_schema_has_a_field_for_authority_or_a_capability(schema, forbidden):
    assert forbidden not in schema.model_fields


def test_capacity_is_named_so_it_cannot_be_read_as_permission():
    """The field says what the market bears, never what anyone may trade."""
    assert "market_capacity_notional" in ExecutionAssessment.model_fields
    assert "position_size_limit_usd" not in ExecutionAssessment.model_fields
    assert "max_additional_notional_usd" not in ExecutionAssessment.model_fields


def test_anchor_cannot_force_a_trade_case_status():
    reachable = package_imports() | package_identifiers()
    for forbidden in ("TradeCaseStatus", "transition_task", "evaluate_trade_case", "record_risk"):
        assert forbidden not in reachable


def test_anchor_submits_one_evidence_type_and_no_other():
    from src.orchestration.worker.policy import authorized_evidence_type
    from src.orchestration.workflow.models import EvidenceType

    assert authorized_evidence_type(AgentRole.ANCHOR) == EvidenceType.LIQUIDITY_EXECUTION
    reachable = package_imports() | package_identifiers()
    for other in ("SentimentPayload", "OnchainPayload", "TriggerPayload", "TradeSetupPayload"):
        assert other not in reachable or other in {"TriggerPayload", "TradeSetupPayload"}


def test_the_reader_reads_the_two_upstream_payloads_and_writes_nothing():
    """It must recognise a setup and a trigger to bind to them; it writes neither."""
    from dataclasses import fields as dc_fields

    names = {item.name for item in dc_fields(AnchorContextReader)}
    assert "cases" in names
    for forbidden in ("recorder", "writer", "session", "submit"):
        assert forbidden not in names


# --------------------------------------------------------------- settings


def test_no_anchor_worker_and_no_quote_provider_by_default():
    configured = settings()
    assert configured.anchor_worker_enabled is False
    assert configured.execution_quote_provider == "disabled"
    assert configured.worker_runtime_enabled is False


def test_no_fixture_quote_source_is_reachable_from_configuration():
    """A synthetic market must never be able to authorise a real assessment."""
    rendered = repr(Settings.model_fields).lower()
    for forbidden in ("fixture", "fake", "stub"):
        assert (
            forbidden not in repr(Settings.model_fields["execution_quote_provider"]).lower()
            or forbidden not in rendered.split("execution_quote_provider")[-1][:200]
        )


def test_nothing_wires_an_anchor_worker_or_a_quote_call_at_startup():
    for symbol in (
        "AnchorWorkerHandler",
        "AnchorContextReader",
        "KyberSwapQuoteSource",
        "anchor_worker_enabled",
        "execution_quote_provider",
    ):
        assert tracked(symbol, "backend/src/api/", "backend/src/runtime/") == "", (
            f"{symbol} is reachable from a startup path"
        )


def test_there_is_no_public_way_to_assert_liquidity():
    api = "\n".join(path.read_text() for path in sorted(Path("src/api").glob("*.py")))
    lowered = api.lower()
    for forbidden in ("post(", "put(", "patch(", "delete("):
        assert forbidden not in lowered
    for forbidden in ("anchor", "quote", "liquidity"):
        assert forbidden not in lowered


def test_the_fixture_quote_source_lives_only_in_a_file_named_fake():
    hits = subprocess.run(
        ["git", "grep", "-l", "FixtureQuoteSource", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert hits in ([], ["backend/src/markets/fake_quotes.py"])


def test_the_other_specialists_are_untouched():
    for role in (
        AgentRole.ORBIT,
        AgentRole.ATLAS,
        AgentRole.SIGNAL,
        AgentRole.VECTOR,
        AgentRole.PULSE,
    ):
        surface = {item.name for item in fields(CAPABILITY_TYPES[role])}
        assert surface == {"lease", "context", "submit"}
