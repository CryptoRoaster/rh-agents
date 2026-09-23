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
    for parameter in ("CODEX_VENDOR", "WORKSPACE", "CODEX_HOME", "INSTALLATION_ID_FILE"):
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


def test_exactly_two_files_are_writable(tmp_path: Path) -> None:
    """The isolated auth file and installation_id, and nothing beside them.

    Codex persists a refreshed token by truncating and rewriting
    CODEX_HOME/auth.json, and its app-server startup opens
    CODEX_HOME/installation_id read-write. Both are named `literal` in the
    profile; the directory is not writable, which is why no third file can
    appear next to them. Both files used here are placeholders -- no real
    credential and no real installation identifier takes part in a test.
    """
    result = measure(tmp_path)
    assert result.auth_readable is True
    assert result.auth_rewritable is True
    assert result.installation_id_readable is True
    assert result.installation_id_rewritable is True
    assert result.other_file_creatable is False
    assert result.write_scope_holds is True


def test_the_installation_id_is_opened_the_way_codex_opens_it(tmp_path: Path) -> None:
    """Read-write on an existing file, then a mode repair.

    A plain overwrite would pass under a policy the app-server startup still
    fails on: `file-write-data` alone permits truncate-and-write but refuses
    `chmod`, and `resolve_installation_id` does both. Measuring the wrong
    operation is how the second real probe came to fail on a boundary that
    looked green.
    """
    assert measure(tmp_path).installation_id_rewritable is True


def test_the_write_rule_is_two_literals_and_never_the_directory(tmp_path: Path) -> None:
    """`(subpath CODEX_HOME)` would reopen everything the literals keep shut."""
    profile = sandbox.compose_profile()
    rules = [line for line in profile.splitlines() if not line.lstrip().startswith(";")]
    text = "\n".join(rules)
    assert '(literal (param "AUTH_FILE"))' in text
    assert '(literal (param "INSTALLATION_ID_FILE"))' in text

    # Every harness write grant targets a literal. Not one of them is a
    # subpath, which is what keeps the directory closed and the file set known.
    # Rules span two lines, so the forms are rejoined before being inspected.
    forms = [f"(allow {form}" for form in " ".join(text.split()).split("(allow ")[1:]]
    # Only the harness grants, which are the parameterised ones. The platform
    # section's own device writes (/dev/null, /dev/fd, /dev/dtracehelper) are
    # runtime, not user data, and are documented separately.
    granting = [form for form in forms if "file-write" in form and "(param " in form]
    assert len(granting) == 2, granting
    for form in granting:
        assert "(literal (param " in form, form
        assert "(subpath" not in form, form

    # `file-write*` on the marker would additionally permit unlinking it. The
    # granted set was measured and stops short of that.
    assert not any("file-write*" in form for form in granting), granting
    assert any("file-write-mode" in form for form in granting), granting


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
    for parameter in ("CODEX_VENDOR", "WORKSPACE", "CODEX_HOME", "INSTALLATION_ID_FILE"):
        assert any(item.startswith(f"{parameter}=") for item in wrapped)


def test_the_vendor_root_is_the_tree_the_binary_needs() -> None:
    executable = Path("/opt/x/vendor/aarch64-apple-darwin/bin/codex")
    assert sandbox.codex_vendor_root(executable).name == "aarch64-apple-darwin"
