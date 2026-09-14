"""What the control plane structurally cannot reach, and cannot become.

A coordinator is the component most likely to acquire authority by accident. It
touches every stage, so every capability looks locally reasonable — a task
setter to unstick a stalled case, a status write to record what is obviously
true, a size to get the demo moving. Each would be defensible in isolation and
together they would be a second system with none of the first one's constraints.

So the tests below are about absence, and they check the absence structurally
rather than by reading the code's intentions.
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest

from src.core.config import Settings
from src.core.models import AgentRole
from src.orchestration.commander.models import (
    RiskState,
    SystemControls,
)
from src.orchestration.worker.capabilities import CAPABILITY_TYPES, CommanderCapabilities
from src.orchestration.worker.policy import authorized_evidence_type
from src.risk.authorization import RiskAuthorization

DATABASE = "postgresql+asyncpg://user@localhost:5432/rh_agents"
PACKAGE = Path("src/orchestration/commander")


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


def package_imports() -> set[str]:
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


# --------------------------------------------------------------- no model


@pytest.mark.parametrize("forbidden", ["reasoning", "anthropic", "openai", "llm", "prompt"])
def test_the_package_imports_no_reasoning_dependency(forbidden):
    """§7. Deterministic infrastructure, verified by import graph."""
    modules = package_imports()
    assert modules
    assert not any(forbidden in module.lower() for module in modules), modules


@pytest.mark.parametrize(
    "forbidden",
    ["ReasoningProvider", "ReasoningRequest", "generate_structured", "PROMPT", "prompt_version"],
)
def test_the_package_names_no_model_machinery(forbidden):
    assert forbidden not in package_identifiers()


def test_there_is_no_prompt_file_at_all():
    assert not (PACKAGE / "prompt.py").exists()


def test_no_decision_reads_prose(package=PACKAGE):
    """§98. Nothing branches on a sentence.

    Findings arrive as enums and typed codes; the one free-text field on a
    decision is a fixed statement looked up from the reason, never parsed.
    """
    from src.orchestration.commander.decision import STATEMENTS
    from src.orchestration.commander.models import CommanderReason

    assert set(STATEMENTS) == set(CommanderReason)
    source = (package / "decision.py").read_text()
    for forbidden in (".lower()", ".startswith(", " in statement", "summary", "rationale"):
        assert forbidden not in source


# --------------------------------------------------------- no capability


def test_the_capability_is_exactly_lease_and_context():
    """No submission port, and no task administration."""
    assert {item.name for item in fields(CommanderCapabilities)} == {"lease", "context"}
    assert CAPABILITY_TYPES[AgentRole.COMMANDER] is CommanderCapabilities


def test_scenario_x_commander_can_never_submit_evidence(worker_db=None):
    """§92. No evidence type is authorized, so the runtime refuses any submission.

    A control plane has no factual domain to contribute. Creating an evidence
    type for orchestration logging would blur two concepts that are worth
    keeping apart: evidence means evidence, and an audit trail means an audit
    trail.
    """
    assert authorized_evidence_type(AgentRole.COMMANDER) is None
    surface = {name for name in dir(CommanderCapabilities) if not name.startswith("_")}
    assert "submit" not in surface


def test_the_read_port_offers_one_question_and_no_administration():
    """§66. No task creation, no retry, no delete, no evaluator trigger."""
    from src.orchestration.worker.capabilities import CommanderContextPort

    methods = {
        name
        for name in dir(CommanderContextPort)
        if not name.startswith("_") and callable(getattr(CommanderContextPort, name, None))
    }
    assert methods == {"commander_context"}
    for forbidden in (
        "create_required_tasks",
        "create_task",
        "set_task_status",
        "retry_task",
        "delete_task",
        "evaluate_trade_case",
        "transition_task",
    ):
        assert forbidden not in dir(CommanderContextPort)


def test_the_package_cannot_reach_a_provider_a_chain_or_a_signer():
    reachable = {item.lower() for item in package_identifiers()}
    for forbidden in (
        "wallet",
        "signer",
        "private_key",
        "broadcast",
        "sendtransaction",
        "calldata",
        "executor",
        "paperexecutor",
        "ledger",
        "rpc",
        "httpx",
        "geckoterminal",
        "kyberswap",
        "neynar",
        "blockscout",
        "moralis",
        "etherscan",
    ):
        assert forbidden not in reachable, f"COMMANDER names {forbidden}"

    modules = {item.lower() for item in package_imports()}
    for forbidden in (
        "httpx",
        "src.markets.kyberswap",
        "src.agents",
        "src.execution",
        "src.ledger",
    ):
        assert not any(module.startswith(forbidden) for module in modules), forbidden


def test_the_package_never_mutates_workflow_state_directly():
    """§6. No status write, no forced transition, no risk binding construction."""
    reachable = package_identifiers()
    for forbidden in (
        "force_status",
        "set_state",
        "set_status",
        "transition_case",
        "record_risk_decision",
        "record_evidence",
        "RiskBinding",
        "RiskDecision",
        "TradeCaseRow",
    ):
        if forbidden == "TradeCaseRow":
            # Read in one advisory duplicate check, never written.
            source = "".join(path.read_text() for path in sorted(PACKAGE.glob("*.py")))
            assert "session.add(" not in source
            assert "TradeCaseRow(" not in source
            continue
        assert forbidden not in reachable, f"COMMANDER names {forbidden}"


def test_the_only_authoritative_write_is_opening_a_case():
    """One mutation, through the existing service, with a derived identity."""
    reachable = package_identifiers()
    assert "open_trade_case" in reachable
    source = "".join(path.read_text() for path in sorted(PACKAGE.glob("*.py")))
    assert source.count("self.cases.") == source.count("self.cases.open_trade_case") + source.count(
        "self.cases.get_trade_case"
    ) + source.count("self.cases.evidence") + source.count("self.cases.tasks")


# ---------------------------------------------------- no risk authority


def test_the_control_plane_cannot_construct_a_risk_evaluation():
    """§27, §28. Nothing here builds a RiskInput, a context or a limit."""
    reachable = package_identifiers()
    for forbidden in ("RiskContext", "RiskLimits", "evaluate", "RiskInput", "classify_decision"):
        assert forbidden not in reachable


def test_risk_state_carries_the_verdict_and_never_the_numbers():
    """§32. An authorization, and whether it still applies. No amounts."""
    assert set(RiskState.model_fields) == {
        "binding_id",
        "risk_decision_id",
        "authorization",
        "risk_input_digest",
        "matches_current_inputs",
        "expires_at",
    }


def test_a_rejection_is_a_verdict_the_control_plane_reads_and_never_revisits():
    """§36, §79. No retry-until-approved path exists to be tested for."""
    from src.orchestration.commander.decision import STATEMENTS
    from src.orchestration.commander.models import CommanderReason

    assert RiskAuthorization.REJECTED.value == "REJECTED"
    assert "does not reconsider" in STATEMENTS[CommanderReason.RISK_REJECTED]
    source = (PACKAGE / "decision.py").read_text()
    for forbidden in ("retry", "reconsider(", "override", "escalate"):
        assert forbidden not in source


# ----------------------------------------------------- paper mode only


def test_scenario_w_there_is_no_live_execution_capability_to_enable():
    """§40, §91. Not disabled — absent. No flag could switch one on."""
    reachable = package_identifiers()
    for forbidden in ("LIVE_AUTONOMOUS", "enable_live_execution", "signing", "sign", "send"):
        assert forbidden not in reachable
    assert SystemControls.model_fields["trading_mode"].annotation is not None
    controls = SystemControls(kill_switch=False, account_paused=False, trading_mode="PAPER")
    assert controls.trading_mode == "PAPER"


def test_no_commander_worker_or_intake_by_default():
    """§63. Configuration presence never starts anything."""
    configured = settings()
    assert configured.commander_worker_enabled is False
    assert configured.commander_intake_enabled is False
    assert configured.commander_kill_switch is False
    assert configured.worker_runtime_enabled is False


def test_nothing_wires_commander_at_startup():
    import subprocess

    for symbol in (
        "CommanderIntakeService",
        "CommanderContextReader",
        "commander_worker_enabled",
        "commander_intake_enabled",
    ):
        found = subprocess.run(
            ["git", "grep", "-il", symbol, "--", "backend/src/api/", "backend/src/runtime/"],
            capture_output=True,
            text=True,
            cwd="..",
        ).stdout.strip()
        assert found == "", f"{symbol} is reachable from a startup path"


def test_there_is_no_public_route_that_forces_anything():
    """§61. Every API route is a read; no bypass was added."""
    api = "\n".join(path.read_text() for path in sorted(Path("src/api").glob("*.py")))
    lowered = api.lower()
    for forbidden in ("post(", "put(", "patch(", "delete("):
        assert forbidden not in lowered
    for forbidden in ("commander", "force", "open-all", "execute"):
        assert forbidden not in lowered


def test_the_other_roles_keep_their_own_capabilities():
    for role in (
        AgentRole.ORBIT,
        AgentRole.ATLAS,
        AgentRole.SIGNAL,
        AgentRole.VECTOR,
        AgentRole.PULSE,
        AgentRole.ANCHOR,
        AgentRole.FUSE,
    ):
        assert {item.name for item in fields(CAPABILITY_TYPES[role])} == {
            "lease",
            "context",
            "submit",
        }


# ------------------------------------------------ the remaining read paths


async def test_a_missing_case_is_a_typed_refusal_not_a_crash(worker_db, now):
    """A control plane asked about a case that does not exist says so safely."""
    from uuid import uuid4

    from src.orchestration.commander.context import CommanderContextUnavailable
    from tests.commander.conftest import build_stack

    _, sessions = worker_db
    _, reader = build_stack(sessions, now)
    with pytest.raises(CommanderContextUnavailable) as caught:
        await reader.commander_context(uuid4(), uuid4())
    assert caught.value.reason_code == "TRADE_CASE_NOT_FOUND"


async def test_a_case_without_a_risk_binding_reports_none(worker_db, now, trace):
    """Absence of an authorization is absence, never a permissive default."""
    from tests.commander.conftest import build_stack, open_case

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-norisk")
    from uuid import uuid4

    context = await reader.commander_context(trade_case.id, uuid4())
    assert context.risk is None


def test_the_pause_reader_reads_and_never_writes():
    """§39. A control plane may observe a stop; it may never lift one."""
    from dataclasses import fields

    from src.orchestration.commander.context import AccountPauseReader

    assert {item.name for item in fields(AccountPauseReader)} == {"sessions"}
    surface = {name for name in dir(AccountPauseReader) if not name.startswith("_")}
    assert surface == {"system_paused"}
