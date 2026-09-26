"""`--scout-once` beside `--preflight` and `--once`, and what preflight says about it."""

import json
from datetime import UTC, datetime

import pytest

from src.runner.composition import RunnerPorts
from src.runner.main import main
from src.runner.preflight import CheckStatus, Preflight
from tests.runner.conftest import runner_settings, stack_for
from tests.runner.test_preflight import migrate_marker
from tests.scout.conftest import EchoOrbit

NOW = datetime(2026, 9, 26, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "argv",
    [["--scout-once", "--once"], ["--scout-once", "--preflight"]],
    ids=["with_once", "with_preflight"],
)
def test_scout_once_is_its_own_mode(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["runner", *argv])
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_disabled_scout_is_refused_before_anything_is_built(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["runner", "--scout-once"])
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://scout@localhost:1/none")
    monkeypatch.setenv("EARLY_SCOUT_ENABLED", "false")
    assert main() == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "detail": None,
        "kind": "run_configuration_refused",
        "reason": "EARLY_SCOUT_NOT_ENABLED",
    }


def test_scout_settings_are_bounded_and_off_by_default():
    settings = runner_settings()
    assert settings.early_scout_enabled is False
    assert settings.early_scout_max_orbit_reviews_per_run == 8
    assert settings.early_scout_max_history_checks_per_run == 1
    # A due review of an older watch needs a fresh reading: refreshes match reviews.
    assert settings.early_scout_max_refresh_markets_per_run == 8
    assert settings.early_scout_max_discovery_pools == 10
    assert settings.early_scout_max_new_watches_per_run == 1
    # Each watch is owed six reviews: the review budget covers the steady state.
    assert (
        settings.early_scout_max_orbit_reviews_per_run
        >= 6 * settings.early_scout_max_new_watches_per_run
    )
    assert settings.early_scout_max_bootstrap_streams <= 100


async def scout_checks(sessions, settings, ports):
    await migrate_marker(sessions)
    stack = stack_for(sessions, settings, NOW, ports=ports)
    reading = await Preflight(settings, sessions).run(stack)
    return {item.name: item for item in reading.checks if item.name.startswith("EARLY_SCOUT")}


async def test_preflight_names_nothing_about_a_disabled_scout(risk_db):
    _, sessions = risk_db
    checks = await scout_checks(sessions, runner_settings(), RunnerPorts())
    assert checks == {}


async def test_preflight_reports_a_configured_scout(risk_db):
    _, sessions = risk_db
    settings = runner_settings(
        early_scout_enabled=True, market_provider="geckoterminal", market_chains="robinhood"
    )
    checks = await scout_checks(sessions, settings, RunnerPorts(reasoning=EchoOrbit()))
    assert {name: item.status for name, item in checks.items()} == {
        "EARLY_SCOUT_MARKET_PROVIDER": CheckStatus.SATISFIED.value,
        "EARLY_SCOUT_REASONING": CheckStatus.SATISFIED.value,
        "EARLY_SCOUT_BUDGETS": CheckStatus.SATISFIED.value,
    }


async def test_preflight_blocks_a_scout_without_a_model_or_a_real_provider(risk_db):
    _, sessions = risk_db
    settings = runner_settings(early_scout_enabled=True)
    checks = await scout_checks(sessions, settings, RunnerPorts())
    assert checks["EARLY_SCOUT_MARKET_PROVIDER"].status == CheckStatus.BLOCKED.value
    assert checks["EARLY_SCOUT_MARKET_PROVIDER"].reason == "EARLY_SCOUT_PROVIDER_NOT_GECKOTERMINAL"
    assert checks["EARLY_SCOUT_REASONING"].status == CheckStatus.BLOCKED.value
    assert checks["EARLY_SCOUT_REASONING"].reason == "REASONING_PROVIDER_NOT_CONFIGURED"
