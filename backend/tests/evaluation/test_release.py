"""The release gates, and what each of them refuses to call a pass."""

from pathlib import Path

import pytest

from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import IsolatedHome
from src.evaluation.codex.catalogs import GPT_5_5_CATALOG, GPT_5_5_CATALOG_SHA256
from src.evaluation.codex.models import SUPPORTED_CLI_VERSION
from src.evaluation.codex.release import GateState, evaluate_release


def sandbox_roots(tmp_path: Path) -> tuple[sandbox.SandboxRoots, Path]:
    workspace = tmp_path / "workspace"
    home = tmp_path / "codex-home"
    outside = tmp_path / "outside"
    vendor = tmp_path / "vendor"
    for directory in (workspace, home, outside, vendor):
        directory.mkdir(exist_ok=True)
    return (
        sandbox.SandboxRoots(codex_vendor=vendor, workspace=workspace, codex_home=home),
        outside,
    )


def status_for(tmp_path: Path, **overrides: object):  # type: ignore[no-untyped-def]
    roots, outside = sandbox_roots(tmp_path)
    home = IsolatedHome(path=tmp_path / "codex-home", auth_present=True)
    settings: dict[str, object] = {
        "catalog_path": GPT_5_5_CATALOG,
        "expected_digest": GPT_5_5_CATALOG_SHA256,
        "model": "gpt-5.5",
        "snapshot_dir": tmp_path,
        "cli_version": f"codex-cli {SUPPORTED_CLI_VERSION}",
        "supported_version": SUPPORTED_CLI_VERSION,
        "chatgpt_session": True,
        "roots": roots,
        "probe_outside": outside,
        "isolated_home": home,
    }
    settings.update(overrides)
    return evaluate_release(**settings)  # type: ignore[arg-type]


def gate(status: object, name: str) -> object:
    return next(item for item in status.gates if item.name == name)  # type: ignore[attr-defined]


@pytest.mark.skipif(not sandbox.available(), reason="macOS sandbox-exec is not available")
def test_the_catalog_version_session_and_sandbox_gates_pass(tmp_path: Path) -> None:
    status = status_for(tmp_path)
    for name in (
        "MODEL_CATALOG",
        "CATALOG_DIGEST",
        "CODEX_VERSION",
        "CHATGPT_SESSION",
        "TOOL_SURFACE",
        "OUTER_READ_SANDBOX",
    ):
        assert gate(status, name).state is GateState.PASS, name  # type: ignore[attr-defined]


@pytest.mark.skipif(not sandbox.available(), reason="macOS sandbox-exec is not available")
def test_auth_isolation_stays_unverified_until_a_real_turn(tmp_path: Path) -> None:
    """The mechanism exists; whether the copied state authenticates does not follow.

    Calling this PASS because the file is in place would be the same mistake as
    calling a quiet run proof that no tool was offered.
    """
    status = status_for(tmp_path)
    assert gate(status, "AUTH_HOME_ISOLATION").state is GateState.UNVERIFIED  # type: ignore[attr-defined]
    assert status.may_run is False
    assert "REAL RUN REFUSED" in status.render()


def test_a_missing_outer_sandbox_is_a_failure_not_a_warning(tmp_path: Path) -> None:
    status = status_for(tmp_path, roots=None, probe_outside=None)
    assert gate(status, "OUTER_READ_SANDBOX").state is GateState.FAIL  # type: ignore[attr-defined]
    assert status.may_run is False


def test_a_stale_digest_fails_its_own_gate(tmp_path: Path) -> None:
    status = status_for(tmp_path, expected_digest="0" * 64)
    assert gate(status, "CATALOG_DIGEST").state is GateState.FAIL  # type: ignore[attr-defined]
    assert status.may_run is False


def test_a_wrong_build_fails_and_an_unknown_one_is_unverified(tmp_path: Path) -> None:
    assert (
        gate(status_for(tmp_path, cli_version="codex-cli 0.155.1"), "CODEX_VERSION").state
        is GateState.FAIL
    )  # type: ignore[attr-defined]
    assert (
        gate(status_for(tmp_path, cli_version=None), "CODEX_VERSION").state is GateState.UNVERIFIED
    )  # type: ignore[attr-defined]


def test_an_unprobed_session_is_unverified_rather_than_assumed(tmp_path: Path) -> None:
    assert (
        gate(status_for(tmp_path, chatgpt_session=None), "CHATGPT_SESSION").state
        is GateState.UNVERIFIED
    )  # type: ignore[attr-defined]
    assert (
        gate(status_for(tmp_path, chatgpt_session=False), "CHATGPT_SESSION").state is GateState.FAIL
    )  # type: ignore[attr-defined]


def test_a_catalog_for_another_model_fails_the_surface_gate(tmp_path: Path) -> None:
    status = status_for(tmp_path, model="gpt-5.6-sol")
    assert gate(status, "TOOL_SURFACE").state is GateState.FAIL  # type: ignore[attr-defined]
    assert status.may_run is False


def test_a_home_without_login_state_fails(tmp_path: Path) -> None:
    status = status_for(tmp_path, isolated_home=IsolatedHome(path=tmp_path, auth_present=False))
    assert gate(status, "AUTH_HOME_ISOLATION").state is GateState.FAIL  # type: ignore[attr-defined]
