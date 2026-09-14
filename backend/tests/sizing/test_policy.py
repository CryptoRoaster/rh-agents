"""The policy, and the one number an operator actually sets.

A configured amount is the only input to this phase a person chooses. It is
therefore the only place where a wrong value would be silent rather than loud,
so it is validated at boot and refused there.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.core.config import Settings
from src.core.models import Side, TradingMode
from src.orchestration.sizing.policy import PAPER_SIZING_V1, PaperSizingPolicy

URL = "postgresql+asyncpg://test_user@localhost/test_database"


def settings(monkeypatch, value: str | None) -> Settings:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.delenv("PAPER_REQUESTED_NOTIONAL_USD", raising=False)
    if value is not None:
        monkeypatch.setenv("PAPER_REQUESTED_NOTIONAL_USD", value)
    return Settings(_env_file=None)


# ------------------------------------------------------------- configuration


def test_no_amount_is_configured_by_default(monkeypatch):
    """The gap stands unless somebody closes it deliberately."""
    assert settings(monkeypatch, None).paper_requested_notional_usd is None


def test_a_configured_amount_is_read_exactly(monkeypatch):
    """Decimal strings convert exactly; nothing passes through a float."""
    configured = settings(monkeypatch, "250.25").paper_requested_notional_usd
    assert configured == Decimal("250.25")
    assert str(configured) == "250.25"


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "-0.000000000000000001",
        "abc",
        "",
        "NaN",
        "Infinity",
        "-Infinity",
        # Finer than the ledger can store.
        "1.0000000000000000001",
        # Larger than `Numeric(38, 18)` can hold.
        "100000000000000000000000000000000000000",
    ],
)
def test_an_unusable_amount_fails_at_boot(monkeypatch, value):
    """Loud at startup rather than quiet at the moment money is sized."""
    with pytest.raises(ValidationError):
        settings(monkeypatch, value)


def test_a_float_amount_is_refused_outright(monkeypatch):
    """`0.1` is not a tenth in binary, and a sized amount must be exact."""
    monkeypatch.setenv("DATABASE_URL", URL)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, paper_requested_notional_usd=500.0)


def test_configuring_an_amount_enables_nothing_else(monkeypatch):
    """A number is not consent to run anything."""
    configured = settings(monkeypatch, "500")
    assert configured.trading_mode is TradingMode.OBSERVE
    assert configured.commander_intake_enabled is False
    assert configured.commander_worker_enabled is False


# -------------------------------------------------------------------- policy


def test_the_shipped_policy_is_paper_buy_only():
    assert PAPER_SIZING_V1.version == "paper-sizing-v1"
    assert PAPER_SIZING_V1.supported_sides == frozenset({Side.BUY})
    assert PAPER_SIZING_V1.supported_modes == frozenset({TradingMode.PAPER})


def test_price_freshness_matches_the_reference_bound_anchor_already_uses():
    """One fact, one tolerance.

    Both are "the recorded USD price of a token, recent enough to mean
    something", read from the same recorder at the same cadence. Two different
    numbers for that would be two different opinions about when a price stops
    describing a market, and the looser one would win by accident.
    """
    assert PAPER_SIZING_V1.max_price_age == ANCHOR_EXECUTION_V1.max_reference_age


def test_the_quantity_envelope_matches_the_ledger():
    """Twenty digits left of the point, eighteen to the right."""
    assert PAPER_SIZING_V1.max_quantity_total_digits == 38
    assert PAPER_SIZING_V1.max_quantity_decimal_places == 18
    assert PAPER_SIZING_V1.max_integer_digits == 20


@pytest.mark.parametrize(
    ("decimals", "unit"),
    [(18, "1E-18"), (6, "0.000001"), (0, "1"), (24, "1E-18"), (36, "1E-18")],
)
def test_the_supported_unit_is_the_coarser_of_token_and_ledger(decimals, unit):
    assert PAPER_SIZING_V1.supported_unit(decimals) == Decimal(unit)


@pytest.mark.parametrize(
    "change",
    [
        {"max_price_age": timedelta(0)},
        {"max_price_age": timedelta(seconds=-1)},
        {"supported_sides": frozenset()},
        {"supported_modes": frozenset()},
        {"max_quantity_decimal_places": 19},
        {"max_quantity_decimal_places": -1},
        {"max_quantity_total_digits": 39},
        {"max_quantity_total_digits": 0},
    ],
)
def test_an_incoherent_policy_refuses_to_exist(change):
    with pytest.raises(ValueError):
        replace(PAPER_SIZING_V1, **change)


def test_a_policy_can_never_be_configured_for_live_trading():
    """Stated as a property of the type, not as a branch somebody must remember."""
    with pytest.raises(ValueError, match="Live execution is unavailable"):
        replace(PAPER_SIZING_V1, supported_modes=frozenset({TradingMode.LIVE_AUTONOMOUS}))


def test_the_policy_holds_no_amount_at_all():
    """Amounts are configured, never shipped. A default would be a strategy."""
    from dataclasses import fields

    names = {item.name for item in fields(PaperSizingPolicy)}
    for forbidden in ("notional", "usd", "amount", "fraction", "kelly", "equity"):
        assert not any(forbidden in name for name in names)
