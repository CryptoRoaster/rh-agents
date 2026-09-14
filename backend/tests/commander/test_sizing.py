"""The number nobody produces, and the numbers that must not be mistaken for it.

To ask SENTINEL about a TradeCase, something has to build a `TradeIntent`, and a
`TradeIntent` carries a quantity. Nothing in `src/` builds one — verified by
import graph below — and nothing produces a requested trade size:

* VECTOR's proposal schema forbids size, notional and portfolio fraction, with
  `extra="forbid"`, so proposing one is a parse error rather than a field
  somebody downstream might read.
* ANCHOR reports the largest size the market was *tested* at, which is a fact
  about liquidity and not a recommendation.
* SENTINEL reports a ceiling, which is a limit and not an instruction.

Two of those are tempting precisely because they are the right shape: a number,
in dollars, already validated, sitting right where a size would go. Using either
would turn a coordinator into a sizing strategy — and an unusually bad one,
since both numbers are maxima.

So the control plane stops and says so. These tests assert that it stops, and
that it stops for the stated reason rather than by accident.
"""

import ast
from pathlib import Path

import pytest

from src.orchestration.commander.decision import decide
from src.orchestration.commander.models import (
    CommanderDecision,
    CommanderDisposition,
    CommanderReason,
)
from src.orchestration.commander.policy import (
    COMMANDER_CONTROL_V1,
    CommanderControlPolicy,
)

PACKAGE = Path("src/orchestration/commander")


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


# ---------------------------------------------- E: the honest stop


async def test_scenario_e_a_fully_evidenced_case_stops_at_the_sizing_gap(worker_db, now, trace):
    """§73. Every prerequisite met, and the control plane still cannot proceed.

    This is the whole finding of the phase, as a test. The case is complete, the
    workflow is satisfied, and asking SENTINEL is the next step — and there is
    no requested size to ask about.
    """
    from tests.commander.conftest import (
        build_stack,
        inject_pre_trigger_evidence,
        inject_trigger,
        open_case,
    )
    from tests.commander.test_execution import inject_anchor_evidence

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "cmd-sizing")
    setup = await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    await inject_trigger(runtime.cases, trade_case, now, setup)
    await inject_anchor_evidence(runtime.cases, trade_case, now, setup)

    from uuid import uuid4

    context = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(context, now, COMMANDER_CONTROL_V1)

    assert context.status.value == "READY_FOR_RISK"
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING
    assert decision.is_progression is False
    assert "requested trade size" in decision.statement


# ------------------------------- N, O: the two numbers that look like sizes


def test_scenario_n_anchor_capacity_is_never_read_as_a_desired_size():
    """§31, §82. `AT_LEAST 50,000` does not mean trade fifty thousand.

    The control plane cannot make this mistake because it never sees the figure:
    the context carries evidence identity and acceptance, never payload content.
    """
    from src.orchestration.commander.models import CommanderContext, EvidenceState

    for forbidden in (
        "largest_tested_acceptable_notional_usd",
        "first_tested_rejected_notional_usd",
        "market_capacity_notional",
        "capacity",
        "notional",
        "quantity",
        "size",
    ):
        assert forbidden not in EvidenceState.model_fields
        assert forbidden not in CommanderContext.model_fields
    assert "largest_tested_acceptable_notional_usd" not in package_identifiers()


def test_scenario_o_a_sentinel_ceiling_is_never_read_as_a_desired_size():
    """§32, §83. `max_additional_notional_usd = 10,000` is a limit, not an order.

    The risk state carries the authorization and the evidence set it was granted
    against — never the numbers. A control plane that could read the cap is one
    edit from trading it.
    """
    from src.orchestration.commander.models import RiskState

    for forbidden in (
        "max_additional_notional_usd",
        "position_size_limit_usd",
        "max_slippage_bps",
        "notional",
        "size",
    ):
        assert forbidden not in RiskState.model_fields
    identifiers = package_identifiers()
    assert "max_additional_notional_usd" not in identifiers
    assert "position_size_limit_usd" not in identifiers


def test_the_package_never_constructs_a_trade_intent():
    """The one object that would carry a size, and nothing here builds one."""
    identifiers = package_identifiers()
    for forbidden in ("TradeIntent", "OrderIntent", "PaperExecutor", "PaperTradingService"):
        assert forbidden not in identifiers


def test_only_the_risk_request_service_produces_a_requested_trade_size():
    """The gap this file recorded is closed, and the assertion is inverted.

    It said a `TradeIntent` constructor appearing anywhere would be the signal
    that the control plane's honest stop could finally become a risk request.
    That happened in Phase 2M-C, so the test now pins *where* — exactly one
    server-side service may build the object that carries a size into a risk
    evaluation, and nothing in the control plane may.
    """
    import subprocess

    found = subprocess.run(
        ["git", "grep", "-l", "--untracked", "TradeIntent(", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert sorted(found) == [
        "backend/src/core/models.py",
        "backend/src/orchestration/riskrequest/service.py",
    ]


def test_no_sizing_number_appears_in_the_policy():
    """§50. The intake policy bounds traffic and freshness, never amounts."""
    from dataclasses import fields

    names = {item.name for item in fields(CommanderControlPolicy)}
    assert names == {
        "version",
        "enabled_chains",
        "max_candidate_age",
        "max_cases_per_cycle",
        "case_lifetime",
        "allow_fixtures",
    }
    for forbidden in ("notional", "size", "quantity", "usd", "fraction", "kelly"):
        assert not any(forbidden in name for name in names)


def test_a_decision_carries_no_amount_at_all():
    for forbidden in ("notional", "size", "quantity", "amount", "price", "slippage"):
        assert forbidden not in CommanderDecision.model_fields


@pytest.mark.parametrize(
    "forbidden",
    ["Decimal", "position_size", "portfolio_fraction", "kelly", "sizing", "allocate"],
)
def test_the_package_contains_no_arithmetic_for_amounts(forbidden):
    """No Decimal anywhere: a control plane with no amounts needs no arithmetic."""
    assert forbidden not in package_identifiers()
