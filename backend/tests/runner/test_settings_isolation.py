"""The runner's test configuration is the one the tests state, and no other.

`Settings` reads `../.env` by design: a deployment is configured that way. A test
is not. A developer's own `.env` — a PAPER setup with a different role set or
smaller budgets — must not be able to change what a runner test asserts, so the
factory these tests use is checked here against parent `.env` files written for
the purpose. Every value in them is synthetic.
"""

from pathlib import Path

import pytest

from tests.runner.conftest import RUNNER_BASELINE, runner_settings

# Two deliberately opposite developer configurations. Each would visibly change
# a runner test: which roles run, how many candidates and cases a pass takes,
# how old a market may be. None of these values is a credential.
ENV_A = {
    "TRADING_MODE": "OBSERVE",
    "PAPER_RUNNER_ENABLED": "false",
    "PAPER_RUNNER_MAX_CANDIDATES": "1",
    "PAPER_RUNNER_MAX_CASES": "1",
    "PULSE_WORKER_ENABLED": "true",
    "FUSE_WORKER_ENABLED": "true",
    "MARKET_MAX_AGE_SECONDS": "3600",
}
ENV_B = {
    "TRADING_MODE": "PAPER",
    "PAPER_RUNNER_ENABLED": "true",
    "PAPER_RUNNER_MAX_CANDIDATES": "50",
    "PAPER_RUNNER_MAX_CASES": "20",
    "PULSE_WORKER_ENABLED": "false",
    "FUSE_WORKER_ENABLED": "false",
    "PAPER_RUNNER_MAX_STEPS": "500",
    "MARKET_MAX_AGE_SECONDS": "5",
}


def under_developer_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout: str, values: dict[str, str]
) -> None:
    """Run from a `backend/` whose parent holds the given `.env`.

    That is where `Settings` looks, so this is the developer checkout in
    miniature. The same names are cleared from the process environment, so what
    is measured is the file and nothing ambient.
    """
    root = tmp_path / checkout
    (root / "backend").mkdir(parents=True)
    (root / ".env").write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    for name in {*ENV_A, *ENV_B}:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root / "backend")


def test_a_parent_env_file_contributes_nothing(tmp_path, monkeypatch):
    under_developer_env(tmp_path, monkeypatch, "a", ENV_A)

    settings = runner_settings()

    assert settings.model_fields_set == set(RUNNER_BASELINE)
    assert settings.pulse_worker_enabled is False
    assert settings.fuse_worker_enabled is False
    assert settings.paper_runner_max_candidates == 5
    assert settings.paper_runner_max_cases == 3


def test_opposite_developer_env_files_yield_the_same_settings(tmp_path, monkeypatch):
    under_developer_env(tmp_path, monkeypatch, "a", ENV_A)
    first = runner_settings(pulse_worker_enabled=True)
    under_developer_env(tmp_path, monkeypatch, "b", ENV_B)
    second = runner_settings(pulse_worker_enabled=True)

    assert first.model_dump() == second.model_dump()
    assert first.model_fields_set == {*RUNNER_BASELINE, "pulse_worker_enabled"}
