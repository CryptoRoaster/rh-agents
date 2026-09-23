"""The release gates, and what each of them refuses to call a pass."""

from pathlib import Path

import pytest

from src.evaluation.codex import release as release_module
from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import AUTH_FILE, INSTALLATION_ID_FILE, IsolatedHome
from src.evaluation.codex.catalogs import GPT_5_5_CATALOG, GPT_5_5_CATALOG_SHA256
from src.evaluation.codex.models import SUPPORTED_CLI_VERSION
from src.evaluation.codex.release import REQUIRED_GATES, GateState, evaluate_release


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
    home_path = tmp_path / "codex-home"
    home_path.chmod(0o700)
    auth = home_path / AUTH_FILE
    if not auth.exists():
        # A placeholder. No real credential appears in any test here.
        auth.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
    auth.chmod(0o600)
    marker = home_path / INSTALLATION_ID_FILE
    if not marker.exists():
        marker.write_text("00000000-0000-4000-8000-000000000000", encoding="utf-8")
    marker.chmod(0o644)
    home = IsolatedHome(path=home_path, auth_present=True)
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
def test_auth_isolation_judges_the_isolation_not_the_token(tmp_path: Path) -> None:
    """These were conflated, and the result was a gate nothing could clear.

    `may_run` needs every blocking gate to pass, while the old version of this
    one could only pass after the turn it was blocking. The isolation itself is
    checkable now; server acceptance is a separate, advisory gate.
    """
    status = status_for(tmp_path)
    assert gate(status, "AUTH_HOME_ISOLATION").state is GateState.PASS  # type: ignore[attr-defined]
    assert gate(status, "AUTH_REMOTE_VALIDITY").state is GateState.UNVERIFIED  # type: ignore[attr-defined]
    # Advisory: unknowable before a request, so it never blocks the check that
    # has to happen before one.
    assert status.may_run is True
    assert "REAL RUN ALLOWED" in status.render()


def test_a_home_with_extra_files_fails_isolation(tmp_path: Path) -> None:
    """The set is closed at two. A third entry is still an isolation failure."""
    status = status_for(tmp_path)
    assert gate(status, "AUTH_HOME_ISOLATION").state is GateState.PASS  # type: ignore[attr-defined]
    (tmp_path / "codex-home" / "config.toml").write_text("x", encoding="utf-8")
    later = status_for(tmp_path)
    assert gate(later, "AUTH_HOME_ISOLATION").state is GateState.FAIL  # type: ignore[attr-defined]
    assert later.may_run is False


def test_a_home_without_the_installation_id_fails_isolation(tmp_path: Path) -> None:
    """The shape the harness built before this was found is now refused.

    `codex exec` requires the file, so a home without it is not a home the
    attempt could run in -- which is exactly what the second real probe
    demonstrated at the cost of one authorised turn.
    """
    status_for(tmp_path)
    home = tmp_path / "codex-home"
    (home / INSTALLATION_ID_FILE).unlink()
    problem = release_module._isolation_problem(home)
    assert problem is not None
    assert "expected exactly" in problem


def test_a_loose_mode_fails_isolation(tmp_path: Path) -> None:
    """A world-readable copy of the login state is not isolation."""
    status_for(tmp_path)  # builds the home the way the harness would
    home = tmp_path / "codex-home"
    (home / AUTH_FILE).chmod(0o644)
    assert release_module._isolation_problem(home) == "auth.json is not 0600"
    (home / AUTH_FILE).chmod(0o600)

    # The second file has its own required mode, and it is checked too: 0644 is
    # what `resolve_installation_id` repairs to, so a different mode means the
    # CLI would attempt a chmod the policy need not have allowed.
    (home / INSTALLATION_ID_FILE).chmod(0o600)
    assert release_module._isolation_problem(home) == "installation_id is not 0644"
    (home / INSTALLATION_ID_FILE).chmod(0o644)

    home.chmod(0o755)
    assert release_module._isolation_problem(home) == "home is not 0700"


@pytest.mark.skipif(not sandbox.available(), reason="macOS sandbox-exec is not available")
def test_egress_is_its_own_gate(tmp_path: Path) -> None:
    status = status_for(tmp_path)
    assert gate(status, "NETWORK_EGRESS").state is GateState.PASS  # type: ignore[attr-defined]


def test_the_gate_set_is_the_same_on_every_platform(tmp_path: Path) -> None:
    """A gate that cannot be measured is FAIL, never absent.

    This is not a cosmetic symmetry. `authorize` requires the full required
    set, so a status that silently drops `NETWORK_EGRESS` where no sandbox can
    be measured would be refused for the wrong reason -- and before the shape
    check existed, an incomplete set was simply not noticed. It is also how the
    Linux CI run came to disagree with the macOS one: the gate was missing, not
    failing, and looking it up raised `StopIteration`.
    """
    assert sorted(item.name for item in status_for(tmp_path).gates) == sorted(REQUIRED_GATES)


def test_an_unmeasurable_platform_fails_both_sandbox_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "macos", lambda: False)
    status = status_for(tmp_path)
    assert sorted(item.name for item in status.gates) == sorted(REQUIRED_GATES)
    assert gate(status, "OUTER_READ_SANDBOX").state is GateState.FAIL  # type: ignore[attr-defined]
    assert gate(status, "NETWORK_EGRESS").state is GateState.FAIL  # type: ignore[attr-defined]
    assert status.may_run is False


@pytest.mark.parametrize(
    "reported",
    ["codex-cli 0.153.40", "codex-cli 0.153.4-dev", "x0.153.4x", "codex-cli"],
)
def test_a_near_miss_version_is_not_the_supported_build(tmp_path: Path, reported: str) -> None:
    """`supported in cli_version` would have accepted 0.153.40 for 0.153.4."""
    status = status_for(tmp_path, cli_version=reported)
    assert gate(status, "CODEX_VERSION").state is GateState.FAIL  # type: ignore[attr-defined]
    assert status.may_run is False


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
