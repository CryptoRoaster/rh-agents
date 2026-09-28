"""PAPER_EXIT_V1 as arithmetic: one trigger or none, in a fixed order."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.orchestration.exitpolicy.policy import (
    ExitInputs,
    ExitTrigger,
    PaperExitPolicy,
    evaluate,
)

T0 = datetime(2026, 9, 28, 12, tzinfo=UTC)
# Test fixture values only; production values are an explicit deployment decision.
POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=6 * 3600)


def inputs(mark: str | None = "1.00", held=timedelta(hours=1), liquidity: str | None = "250000"):
    return ExitInputs(
        entry_price_usd=Decimal("1.00"),
        entered_at=T0,
        mark_price_usd=None if mark is None else Decimal(mark),
        mark_observed_at=None if mark is None else T0 + held,
        liquidity_usd=None if liquidity is None else Decimal(liquidity),
        min_liquidity_usd=Decimal("100000"),
        now=T0 + held,
    )


@pytest.mark.parametrize(
    ("case", "expected", "reason"),
    [
        (inputs(mark="0.80"), ExitTrigger.STOP_LOSS, "MARK_AT_OR_BELOW_STOP"),
        (inputs(mark="1.50"), ExitTrigger.TAKE_PROFIT, "MARK_AT_OR_ABOVE_TARGET"),
        (inputs(held=timedelta(hours=6)), ExitTrigger.TIME_EXIT, "MAX_HOLDING_TIME_REACHED"),
        (
            inputs(liquidity="99999"),
            ExitTrigger.SENTINEL_INVALIDATION,
            "LIQUIDITY_BELOW_SENTINEL_MINIMUM",
        ),
        (inputs(mark="1.10"), None, "HOLD"),
    ],
    ids=["stop", "target", "time", "sentinel", "hold"],
)
def test_each_trigger_and_hold(case, expected, reason):
    verdict = evaluate(POLICY, case)
    assert (verdict.trigger, verdict.reason) == (expected, reason)
    assert verdict.policy_version == "PAPER_EXIT_V1"


def test_protective_triggers_come_first():
    # Below the stop, below SENTINEL's liquidity and past the time: recorded as a stop.
    both = inputs(mark="0.50", liquidity="10", held=timedelta(hours=7))
    assert evaluate(POLICY, both).trigger is ExitTrigger.STOP_LOSS
    # Invalidation outranks a take-profit and a time exit.
    invalid = inputs(mark="2.00", liquidity="10", held=timedelta(hours=7))
    assert evaluate(POLICY, invalid).trigger is ExitTrigger.SENTINEL_INVALIDATION


def test_unknown_data_triggers_nothing_it_cannot_prove():
    unknown = evaluate(POLICY, inputs(mark=None, liquidity=None))
    assert (unknown.trigger, unknown.reason, unknown.return_bps) == (
        None,
        "HOLD_MARK_UNKNOWN",
        None,
    )
    # A time exit needs no price.
    late = evaluate(POLICY, inputs(mark=None, liquidity=None, held=timedelta(hours=6)))
    assert late.trigger is ExitTrigger.TIME_EXIT


def test_exact_boundaries_trigger():
    assert evaluate(POLICY, inputs(mark="0.8000")).return_bps == Decimal("-2000.00")
    assert evaluate(POLICY, inputs(mark="0.8000")).trigger is ExitTrigger.STOP_LOSS
    assert evaluate(POLICY, inputs(mark="1.5000")).trigger is ExitTrigger.TAKE_PROFIT


def test_the_invalidation_can_be_switched_off():
    off = PaperExitPolicy(**{**POLICY.model_dump(), "invalidate_below_min_liquidity": False})
    assert evaluate(off, inputs(liquidity="10")).trigger is None


def test_the_policy_has_no_invented_defaults():
    with pytest.raises(ValidationError):
        PaperExitPolicy()  # type: ignore[call-arg]


def test_settings_refuse_an_enabled_policy_without_its_numbers(monkeypatch):
    from src.core.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg:///rh_agents_test?host=/tmp")
    for name in (
        "PAPER_AUTO_EXIT_ENABLED",
        "PAPER_EXIT_STOP_LOSS_BPS",
        "PAPER_EXIT_TAKE_PROFIT_BPS",
        "PAPER_EXIT_MAX_HOLDING_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert Settings(_env_file=None).paper_auto_exit_enabled is False
    monkeypatch.setenv("PAPER_AUTO_EXIT_ENABLED", "true")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("PAPER_EXIT_STOP_LOSS_BPS", "2000")
    monkeypatch.setenv("PAPER_EXIT_TAKE_PROFIT_BPS", "5000")
    monkeypatch.setenv("PAPER_EXIT_MAX_HOLDING_MINUTES", "360")
    assert Settings(_env_file=None).paper_auto_exit_enabled is True
