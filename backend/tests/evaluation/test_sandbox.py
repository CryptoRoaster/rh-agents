"""The outer read boundary, exercised with sentinels rather than real secrets."""

from pathlib import Path

import pytest

from src.evaluation.codex import sandbox

pytestmark = pytest.mark.skipif(
    not sandbox.available(), reason="macOS sandbox-exec is not available"
)


def roots_for(tmp_path: Path) -> tuple[sandbox.SandboxRoots, Path]:
    workspace = tmp_path / "workspace"
    home = tmp_path / "codex-home"
    outside = tmp_path / "outside"
    for directory in (workspace, home, outside):
        directory.mkdir()
    return (
        sandbox.SandboxRoots(
            codex_vendor=tmp_path / "vendor", workspace=workspace, codex_home=home
        ),
        outside,
    )


def test_the_profile_denies_by_default() -> None:
    profile = sandbox.compose_profile()
    assert "(deny default)" in profile
    # The thing Codex's own read-only policy does, and this one must not.
    # Comments are excluded: the header explains that blanket rule by quoting it.
    rules = [line for line in profile.splitlines() if not line.lstrip().startswith(";")]
    assert not any(line.strip() == "(allow file-read*)" for line in rules)
    assert not any(line.strip().startswith('(allow file-read* (regex #"^/"') for line in rules)
    for parameter in ("CODEX_VENDOR", "WORKSPACE", "CODEX_HOME"):
        assert f'(param "{parameter}")' in profile


def measure(tmp_path: Path) -> sandbox.ProbeResult:
    roots, outside = roots_for(tmp_path)
    profile = sandbox.write_profile(tmp_path)
    try:
        return sandbox.probe_boundaries(roots, profile, outside)
    finally:
        profile.unlink(missing_ok=True)


def test_the_boundary_holds_for_all_three_classes(tmp_path: Path) -> None:
    """Allowed reads work; a neighbouring directory and a secret stand-in do not.

    The sentinel represents the repository, HOME and credential classes. Reading
    a real `.env` to check would prove one path instead of a class, and would
    put an actual secret in a test.
    """
    result = measure(tmp_path)
    assert result.allowed_readable is True
    assert result.forbidden_readable is False
    assert result.sentinel_readable is False
    assert result.read_boundary_holds is True


def test_exactly_one_file_is_writable(tmp_path: Path) -> None:
    """The isolated auth file, and nothing beside it.

    Codex persists a refreshed token by truncating and rewriting
    CODEX_HOME/auth.json, so that one file has to be writable. The directory is
    not, which is why a second file cannot appear next to it. The file used here
    is a placeholder; no real credential takes part in a test.
    """
    result = measure(tmp_path)
    assert result.auth_readable is True
    assert result.auth_rewritable is True
    assert result.other_file_creatable is False
    assert result.write_scope_holds is True


def test_outbound_tcp_is_reachable_through_the_profile(tmp_path: Path) -> None:
    """Measured against a loopback listener, not against anyone real.

    The turn has to reach the provider, so egress is open and documented as a
    limit. What the profile enforces is the file read boundary.
    """
    assert measure(tmp_path).egress_reachable is True


def test_the_app_sandbox_extensions_are_not_inherited() -> None:
    """An inherited extension would grant paths outside the three roots."""
    rules = [
        line for line in sandbox.compose_profile().splitlines() if not line.lstrip().startswith(";")
    ]
    assert not any("app-sandbox" in line for line in rules)
    assert not any("/opt/homebrew/lib" in line for line in rules)
    assert not any("/usr/local/lib" in line for line in rules)


def test_paths_reach_the_profile_resolved(tmp_path: Path) -> None:
    """Seatbelt matches the real filesystem: /tmp never matches /private/tmp."""
    roots, _ = roots_for(tmp_path)
    (tmp_path / "vendor").mkdir()
    parameters = roots.parameters()
    for value in parameters.values():
        assert Path(value).is_absolute()
        assert Path(value) == Path(value).resolve()


def test_the_wrapper_keeps_the_command_intact(tmp_path: Path) -> None:
    roots, _ = roots_for(tmp_path)
    (tmp_path / "vendor").mkdir()
    profile = tmp_path / "profile.sb"
    profile.write_text("(version 1)\n", encoding="utf-8")
    wrapped = sandbox.wrap(["/bin/echo", "hello"], profile, roots)
    assert wrapped[0] == str(sandbox.SANDBOX_EXEC)
    assert wrapped[-2:] == ["/bin/echo", "hello"]
    assert "--" in wrapped
    for parameter in ("CODEX_VENDOR", "WORKSPACE", "CODEX_HOME"):
        assert any(item.startswith(f"{parameter}=") for item in wrapped)


def test_the_vendor_root_is_the_tree_the_binary_needs() -> None:
    executable = Path("/opt/x/vendor/aarch64-apple-darwin/bin/codex")
    assert sandbox.codex_vendor_root(executable).name == "aarch64-apple-darwin"
