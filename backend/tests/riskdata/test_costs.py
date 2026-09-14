"""The PAPER cost basis: configured, bounded, and never mistaken for a measurement."""

from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.config import Settings
from src.core.models import TradingMode
from src.orchestration.costs import (
    PAPER_COST_V1,
    PaperCostAssumptions,
    PaperCostPolicy,
    PaperCostRefusal,
    PaperCostUnavailable,
    paper_cost_assumptions,
)

URL = "postgresql+asyncpg://test_user@localhost/test_database"


def read(fee="30", slippage="25", mode=TradingMode.PAPER, policy=PAPER_COST_V1):
    return paper_cost_assumptions(
        fee_bps=None if fee is None else Decimal(fee),
        slippage_bps=None if slippage is None else Decimal(slippage),
        trading_mode=mode,
        policy=policy,
    )


def settings(monkeypatch, **values) -> Settings:
    monkeypatch.setenv("DATABASE_URL", URL)
    for name in ("PAPER_FEE_BPS", "PAPER_SLIPPAGE_BPS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name.upper(), value)
    return Settings(_env_file=None)


# ---------------------------------------------------------------- no basis


def test_nothing_is_configured_by_default(monkeypatch):
    """A simulation is not told what trading costs until somebody says so."""
    configured = settings(monkeypatch)
    assert configured.paper_fee_bps is None
    assert configured.paper_slippage_bps is None


def test_an_unconfigured_basis_is_not_a_free_trade():
    """Absence never becomes zero.

    Zero is a meaningful configured value — a venue may genuinely charge
    nothing — which is exactly why the absence of a value, and not a zero, is
    what signals a missing basis.
    """
    reading = read(fee=None, slippage=None)
    assert isinstance(reading, PaperCostUnavailable)
    assert reading.reason is PaperCostRefusal.PAPER_COST_BASIS_NOT_CONFIGURED


@pytest.mark.parametrize(
    ("fee", "slippage", "expected"),
    [
        (None, "25", PaperCostRefusal.PAPER_COST_FEE_NOT_CONFIGURED),
        ("30", None, PaperCostRefusal.PAPER_COST_SLIPPAGE_NOT_CONFIGURED),
    ],
)
def test_half_a_basis_is_no_basis(fee, slippage, expected):
    assert read(fee=fee, slippage=slippage).reason is expected


def test_zero_is_a_configured_value_rather_than_an_absence():
    reading = read(fee="0", slippage="0")
    assert isinstance(reading, PaperCostAssumptions)
    assert reading.fee_bps == 0


def test_only_paper_has_cost_assumptions():
    """OBSERVE watches and does not act; live execution does not exist here."""
    for mode in (TradingMode.OBSERVE, TradingMode.LIVE_AUTONOMOUS):
        assert read(mode=mode).reason is PaperCostRefusal.PAPER_COST_MODE_NOT_SUPPORTED


# ------------------------------------------------------------------- bounds


@pytest.mark.parametrize(("fee", "slippage"), [("501", "25"), ("30", "501"), ("9999", "9999")])
def test_a_misplaced_decimal_is_refused_rather_than_simulated(fee, slippage):
    """The bound is a typo guard, not a view on acceptable trading costs."""
    assert read(fee=fee, slippage=slippage).reason is PaperCostRefusal.PAPER_COST_OUT_OF_BOUNDS


def test_the_bounds_travel_with_the_assumption():
    """Bound by content, not by a version label that could be reused."""
    reading = read()
    assert reading.policy.version == "paper-cost-v1"
    assert reading.policy.max_fee_bps == PAPER_COST_V1.max_fee_bps
    wider = replace(PAPER_COST_V1, max_fee_bps=Decimal(900))
    assert read(fee="800", policy=wider).policy.max_fee_bps == Decimal(900)


def test_a_policy_can_never_be_configured_for_live_trading():
    with pytest.raises(ValueError, match="Live execution is unavailable"):
        replace(PAPER_COST_V1, supported_modes=frozenset({TradingMode.LIVE_AUTONOMOUS}))


@pytest.mark.parametrize(
    "change",
    [
        {"max_fee_bps": Decimal(0)},
        {"max_fee_bps": Decimal(-1)},
        {"max_fee_bps": Decimal(10000)},
        {"max_slippage_bps": Decimal(0)},
        {"supported_modes": frozenset()},
    ],
)
def test_an_incoherent_policy_refuses_to_exist(change):
    with pytest.raises(ValueError):
        replace(PAPER_COST_V1, **change)


def test_the_policy_ships_no_cost(monkeypatch):
    """Bounds are code; amounts are configuration. A default would be a number
    nobody chose, quietly deciding what a simulated trade costs."""
    from dataclasses import fields

    names = {item.name for item in fields(PaperCostPolicy)}
    assert names == {"version", "supported_modes", "max_fee_bps", "max_slippage_bps"}
    assert settings(monkeypatch).paper_fee_bps is None


# ------------------------------------------------------------------ meaning


def test_the_assumption_says_it_is_an_assumption():
    reading = read()
    assert reading.basis == "OPERATOR_CONFIGURED_ASSUMPTION"
    assert reading.mode is TradingMode.PAPER


def test_the_fee_names_the_side_it_applies_to():
    """Two readings of "fee" differ by a factor of two; the contract picks one.

    One side of one trade, on the executed notional. A later exit charges it
    again, and a caller that applied it twice to one fill would double-count.
    """
    assert read().fee_meaning == "PROPORTIONAL_FEE_ON_ONE_SIDE_EXECUTED_NOTIONAL"


def test_the_slippage_moves_a_price_rather_than_adding_a_charge():
    assert read().slippage_meaning == "ASSUMED_ADVERSE_MOVE_FROM_REFERENCE_TO_FILL"


def test_the_basis_names_what_it_does_not_cover():
    """So nobody reads two numbers as a complete cost model."""
    excluded = set(read().excludes)
    assert {"GAS", "EXIT_SIDE_FEE", "PRICE_IMPACT_BEYOND_ASSUMED_MOVE"} <= excluded


def test_no_observed_figure_can_reach_this_contract():
    """Four observed figures in this system must never be read in here.

    ANCHOR's execution deviation, a provider's price impact, a tested capacity
    and a realised fill cost are all measurements of something that happened.
    None of them is a stated assumption, and the contract has no field any of
    them could arrive in.
    """
    fields = set(PaperCostAssumptions.model_fields)
    for forbidden in (
        "execution_deviation_bps",
        "provider_price_impact_bps",
        "largest_tested_acceptable_notional_usd",
        "maximum_safe_size_usd",
        "realized_slippage_bps",
        "observed_at",
        "quoted_at",
    ):
        assert forbidden not in fields


def test_nothing_writes_these_assumptions_back_into_anchor_evidence():
    """Evidence records what was observed. An assumption stored as one is
    indelible and wrong, so no ANCHOR contract may name this module."""
    import ast
    from pathlib import Path

    for path in sorted(Path("src/agents/anchor").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "costs" not in node.module.split(".")


# ------------------------------------------------------------ configuration


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("paper_fee_bps", "-1"),
        ("paper_fee_bps", "10001"),
        ("paper_fee_bps", "abc"),
        ("paper_fee_bps", "NaN"),
        ("paper_slippage_bps", "Infinity"),
        ("paper_slippage_bps", "1.0000000000000000001"),
    ],
)
def test_an_unusable_configured_value_fails_at_boot(monkeypatch, name, value):
    with pytest.raises(ValidationError):
        settings(monkeypatch, **{name: value})


def test_a_float_is_refused_outright(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, paper_fee_bps=30.0)


def test_configuring_costs_enables_nothing(monkeypatch):
    """A cost basis is one fact among several, and never a permission."""
    configured = settings(monkeypatch, paper_fee_bps="30", paper_slippage_bps="25")
    assert configured.paper_fee_bps == Decimal("30")
    assert configured.trading_mode is TradingMode.OBSERVE
    assert configured.commander_intake_enabled is False
    assert configured.paper_requested_notional_usd is None
