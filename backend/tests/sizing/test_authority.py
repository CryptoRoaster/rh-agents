"""What a sizing module must never be able to reach.

Two numbers in this system are maxima, both in dollars, both already validated,
and both sitting exactly where a desired size would go: ANCHOR's tested capacity
and SENTINEL's remaining headroom. Using either would turn a configured request
into a sizing strategy, and an unusually bad one — a strategy that always asks
for the largest amount anybody would allow.

These tests assert the separation structurally, where a future edit has to pass
them, rather than only behaviourally where a later refactor could drift.
"""

import ast
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from tests.sizing.conftest import (
    RecordedMarkets,
    build_reader,
    open_case,
    record_anchor,
    record_setup,
    record_trigger,
    recorded_snapshot,
)

PACKAGE = Path("src/orchestration/sizing")


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


@pytest.mark.parametrize(
    "forbidden",
    [
        "largest_tested_acceptable_notional_usd",
        "first_tested_rejected_notional_usd",
        "maximum_safe_size_usd",
        "market_capacity_notional",
        "capacity_semantics",
        "ExecutionAssessmentDetail",
        "LiquidityExecutionPayload",
    ],
)
def test_anchor_capacity_is_unreachable_from_sizing(forbidden):
    """`AT_LEAST 50,000` is a fact about liquidity, not an instruction."""
    assert forbidden not in package_identifiers()


@pytest.mark.parametrize(
    "forbidden",
    [
        "max_additional_notional_usd",
        "position_size_limit_usd",
        "max_slippage_bps",
        "RiskDecision",
        "RiskBinding",
        "RiskLimits",
        "RiskContext",
        "RiskAuthorization",
        "TradeCaseRiskBindingRow",
        "classify_decision",
        "evaluate",
    ],
)
def test_sentinel_limits_are_unreachable_from_sizing(forbidden):
    """A ceiling is a limit. A module that could read it is one edit from it."""
    assert forbidden not in package_identifiers()


@pytest.mark.parametrize(
    "forbidden",
    ["TradeIntent", "OrderIntent", "ExecutionResult", "PaperExecutor", "PaperTradingService"],
)
def test_sizing_builds_nothing_that_could_be_executed(forbidden):
    assert forbidden not in package_identifiers()


def test_sizing_itself_still_produces_no_trade_intent():
    """Sizing computes a size. Something else turns one into a request.

    Phase 2M-C settled the durable identity that was missing and built the
    intent in one narrow server-side service. This package is deliberately not
    that service, and the separation is asserted rather than assumed.
    """
    found = subprocess.run(
        ["git", "grep", "-l", "--untracked", "TradeIntent(", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert "backend/src/orchestration/sizing/models.py" not in found
    assert "backend/src/orchestration/sizing/context.py" not in found
    assert "backend/src/orchestration/riskrequest/service.py" in found


@pytest.mark.parametrize(
    "forbidden",
    [
        "AsyncSession",
        "async_sessionmaker",
        "select",
        "text",
        "Base",
        "ExecutionQuoteSource",
        "quote_exact_input",
        "AnthropicProvider",
        "httpx",
    ],
)
def test_sizing_holds_no_generic_database_or_provider_capability(forbidden):
    """Two narrow read ports, and no way to widen them from inside.

    A component that decides how much money a request asks for must not also
    hold a session, a quote client or a transport — those are the difference
    between deciding an amount and acting on one.
    """
    assert forbidden not in package_identifiers()


def test_the_read_ports_expose_exactly_three_methods():
    """The surface, asserted as a surface rather than described in prose."""
    from src.orchestration.sizing.context import SizingCaseSource, SizingMarketInput

    def methods(protocol: type) -> set[str]:
        return {name for name in vars(protocol) if not name.startswith("_")}

    assert methods(SizingCaseSource) == {"get_trade_case", "evidence"}
    assert methods(SizingMarketInput) == {"latest"}


def test_the_control_plane_is_not_wired_to_sizing():
    """No launcher, no capability, no decision path. Deliberately inert.

    COMMANDER reports the sizing gap exactly as it did before; a control plane
    that could read a size would be one step from asking for it.
    """
    imported: set[str] = set()
    identifiers: set[str] = set()
    for path in sorted(Path("src/orchestration/commander").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                identifiers.add(node.id)
    assert not any("sizing" in module for module in imported)
    for forbidden in ("PaperSizingReader", "assess_paper_sizing", "SizingAssessment"):
        assert forbidden not in identifiers

    from src.orchestration.commander.models import CommanderDecision

    assert not any("size" in name or "sizing" in name for name in CommanderDecision.model_fields)


def test_the_configured_amount_starts_no_worker_and_reaches_no_launcher():
    """Configuring a size still enables nothing, and this names who may read it.

    Two places, and the second arrived with Phase 2N-A. The bounded run reads
    the configured amount to hand it to the risk request — which has always
    needed it, and until then only a test could supply one. That is the field's
    purpose, not a widening of it: the amount is read when somebody explicitly
    invokes a run, and reading it still starts no worker and no launcher.

    The assertion below is what keeps that true. The web process must not be
    able to reach the runner at all, so a configured amount cannot begin
    anything by being present.
    """
    found = subprocess.run(
        ["git", "grep", "-l", "--untracked", "paper_requested_notional_usd", "--", "backend/src/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert sorted(found) == [
        "backend/src/core/config.py",
        "backend/src/runner/composition.py",
    ]

    # And the only reader of it is unreachable from the process a deployment
    # actually starts on its own.
    reachable = subprocess.run(
        ["git", "grep", "-l", "--untracked", "src.runner", "--", "backend/src/api/"],
        capture_output=True,
        text=True,
        cwd="..",
    ).stdout.split()
    assert reachable == []


async def test_a_live_anchor_capacity_does_not_move_the_requested_size(worker_db, now, trace):
    """The behavioural half, over real evidence rather than a static check.

    The case carries an ANCHOR finding reporting fifty thousand dollars of
    tested capacity. The requested size stays exactly what was configured.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(recorded_snapshot(now)))
    trade_case = await open_case(reader.cases, now, trace)
    setup = await record_setup(reader.cases, trade_case, now)
    before = await reader.sizing(trade_case.id)

    trigger = await record_trigger(reader.cases, trade_case, now, setup)
    await record_anchor(reader.cases, trade_case, now, setup, trigger)
    after = await reader.sizing(trade_case.id)

    assert before.kind == after.kind == "sizing_assessment"
    assert after.requested_notional_usd == Decimal("500")
    assert after.quantity == before.quantity
    assert after.input_digest == before.input_digest


def test_the_assessment_carries_no_ceiling_of_any_kind():
    """Nothing on the contract a reader could mistake for permission."""
    from src.orchestration.sizing.models import SizingAssessment

    for forbidden in ("max", "limit", "capacity", "allowed", "approved", "authorization"):
        assert not any(forbidden in name for name in SizingAssessment.model_fields)
