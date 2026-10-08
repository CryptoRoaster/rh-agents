"""PRE_VECTOR_EARLY_ENTRY_V1 as policy: what it is, and what it leaves untouched."""

from datetime import timedelta
from decimal import Decimal

from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, EARLY_ANCHOR_EXECUTION_V1
from src.agents.vector.models import SetupKind
from src.agents.vector.policy import VECTOR_SETUP_V1
from src.core.config import Settings
from src.core.models import AgentRole, RiskLimits
from src.orchestration.strategy.early import (
    EARLY_ENTRY_V1,
    EARLY_SETUP_KIND,
    PRE_VECTOR_EARLY_ENTRY_V1,
    EarlyBook,
    cap_refusal,
    capacity_refusal,
    early_risk_limits,
    limits_for,
)
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.policy import (
    CURRENT_WORKFLOW,
    TRADE_CASE_EARLY_V1,
    TRADE_CASE_V2,
    policy_for,
)


def test_the_strategy_constants_are_the_ones_decided():
    policy = EARLY_ENTRY_V1
    assert policy.version == PRE_VECTOR_EARLY_ENTRY_V1
    assert policy.max_age == timedelta(hours=6)
    assert policy.allowed_history == {"MARKET_HISTORY_TOO_SHORT", "MARKET_HISTORY_EMPTY"}
    assert policy.notional_usd == Decimal(10)
    assert policy.max_open_positions == 5
    assert policy.max_exposure_usd == Decimal(50)
    assert policy.daily_loss_cap_usd == Decimal(30)
    assert policy.max_entries_per_run == 1
    assert policy.reentry is False
    assert policy.min_liquidity_usd == Decimal(10_000)
    assert policy.lifetime == timedelta(minutes=10)


def test_the_normal_workflow_is_still_the_current_one_and_unchanged():
    assert CURRENT_WORKFLOW is TRADE_CASE_V2
    assert policy_for("trade-case-v2") is TRADE_CASE_V2
    roles = {item.role: item for item in TRADE_CASE_V2.requirements}
    # VECTOR still produces the setup, required and safety-critical.
    assert roles[AgentRole.VECTOR].evidence_type is EvidenceType.TRADE_SETUP
    assert roles[AgentRole.VECTOR].required and roles[AgentRole.VECTOR].safety_critical
    # SIGNAL stays optional, FUSE stays optional and outside the risk digest.
    assert roles[AgentRole.SIGNAL].required is False
    assert roles[AgentRole.FUSE].required is False
    assert AgentRole.EARLY not in roles
    assert all(task.role is not AgentRole.EARLY for task in TRADE_CASE_V2.tasks)


def test_the_early_workflow_requires_orbit_atlas_early_pulse_and_anchor():
    assert policy_for("trade-case-early-v1") is TRADE_CASE_EARLY_V1
    required = {item.role for item in TRADE_CASE_EARLY_V1.requirements if item.required}
    assert required == {
        AgentRole.ORBIT,
        AgentRole.ATLAS,
        AgentRole.EARLY,
        AgentRole.PULSE,
        AgentRole.ANCHOR,
    }
    roles = {item.role for item in TRADE_CASE_EARLY_V1.requirements}
    assert AgentRole.SIGNAL not in roles  # NOT_USED_IN_V1
    assert AgentRole.VECTOR not in roles
    setup = TRADE_CASE_EARLY_V1.requirement(EvidenceType.TRADE_SETUP)
    assert setup.role is AgentRole.EARLY and setup.safety_critical
    # The same trigger watch and the same refreshable sources as V2.
    pulse = next(item for item in TRADE_CASE_EARLY_V1.tasks if item.role is AgentRole.PULSE)
    assert pulse == next(item for item in TRADE_CASE_V2.tasks if item.role is AgentRole.PULSE)
    assert TRADE_CASE_EARLY_V1.refreshable_sources == TRADE_CASE_V2.refreshable_sources


def test_vector_is_untouched_and_can_never_propose_the_early_kind():
    assert VECTOR_SETUP_V1.history_bars == 48
    assert VECTOR_SETUP_V1.min_closed_bars == 24
    assert VECTOR_SETUP_V1.supported_kinds == {SetupKind.BREAKOUT_LONG, SetupKind.PULLBACK_LONG}
    assert EARLY_SETUP_KIND not in {kind.value for kind in SetupKind}


def test_the_anchor_ladders():
    assert ANCHOR_EXECUTION_V1.ladder_notional == tuple(
        Decimal(item) for item in (100, 500, 2500, 10000, 50000)
    )
    assert ANCHOR_EXECUTION_V1.rung_rounding == "DOWN"
    assert EARLY_ANCHOR_EXECUTION_V1.ladder_notional == tuple(
        Decimal(item) for item in (10, 25, 50, 100, 250)
    )
    # Every integrity bound is the normal one.
    for name in (
        "max_quote_age",
        "max_reference_age",
        "max_ladder_skew",
        "max_execution_deviation_bps",
        "max_provider_price_impact_bps",
        "max_route_hops",
        "max_usd_valuation_skew_bps",
    ):
        assert getattr(EARLY_ANCHOR_EXECUTION_V1, name) == getattr(ANCHOR_EXECUTION_V1, name)
    assert EARLY_ANCHOR_EXECUTION_V1.max_quote_age == timedelta(seconds=30)
    assert EARLY_ANCHOR_EXECUTION_V1.max_quote_requests == 8


def test_only_minimum_liquidity_differs_in_the_early_risk_limits():
    normal = RiskLimits()
    early = early_risk_limits(normal)
    assert normal.min_liquidity_usd == Decimal(100_000)
    assert early.min_liquidity_usd == Decimal(10_000)
    assert early.model_dump(exclude={"min_liquidity_usd"}) == normal.model_dump(
        exclude={"min_liquidity_usd"}
    )
    # The position limit is the account's, not the early notional.
    assert early.max_position_size_usd == Decimal(2500)
    assert limits_for(None, normal) is normal
    assert limits_for("SOMETHING_ELSE", normal) is normal
    assert limits_for(PRE_VECTOR_EARLY_ENTRY_V1, normal) == early


def test_the_strategy_caps():
    open_book = EarlyBook(open_positions=4, exposure_usd=Decimal(40), realized_loss_today_usd=0)
    assert cap_refusal(open_book) is None
    assert cap_refusal(EarlyBook(5, Decimal(40), Decimal(0))) == "EARLY_MAX_OPEN_POSITIONS_REACHED"
    assert cap_refusal(EarlyBook(4, Decimal("40.01"), Decimal(0))) == "EARLY_MAX_EXPOSURE_REACHED"
    assert cap_refusal(EarlyBook(0, Decimal(0), Decimal(30))) == "EARLY_DAILY_LOSS_CAP_REACHED"
    assert cap_refusal(EarlyBook(0, Decimal(0), Decimal("29.99"))) is None


def test_the_fixed_notional_is_never_downsized():
    assert capacity_refusal(Decimal(10)) is None
    assert capacity_refusal(Decimal(250)) is None
    assert capacity_refusal(Decimal("9.999999")) == "EARLY_EXECUTABLE_CAPACITY_BELOW_NOTIONAL"
    assert capacity_refusal(None) == "EARLY_EXECUTABLE_CAPACITY_UNKNOWN"


def test_the_consent_is_off_by_default_and_needs_its_inputs():
    import pytest

    url = "postgresql+asyncpg://user@localhost/test_database"
    assert Settings.model_fields["pre_vector_early_entry_enabled"].default is False
    assert Settings(_env_file=None, database_url=url).pre_vector_early_entry_enabled is False
    with pytest.raises(ValueError, match="bounded paper run"):
        Settings(_env_file=None, database_url=url, pre_vector_early_entry_enabled=True)
    paper = {"trading_mode": "PAPER", "paper_runner_enabled": True}
    with pytest.raises(ValueError, match="EARLY_SCOUT_ENABLED"):
        Settings(_env_file=None, database_url=url, pre_vector_early_entry_enabled=True, **paper)
