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
    catalog_runtime = tmp_path / "catalog-runtime"
    for directory in (workspace, home, outside, catalog_runtime):
        directory.mkdir()
    return (
        sandbox.SandboxRoots(
            codex_vendor=tmp_path / "vendor",
            workspace=workspace,
            codex_home=home,
            # A placeholder path. `probe_boundaries` seeds it in the shape the
            # real runtime catalog has, and the checks against it are
            # destructive by design, so the real one is never pointed at here.
            catalog_file=catalog_runtime / "models.json",
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
    for parameter in (
        "CODEX_VENDOR",
        "WORKSPACE",
        "CODEX_HOME",
        "INSTALLATION_ID_FILE",
        "CATALOG_FILE",
    ):
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


def test_the_resolver_is_reachable_through_the_profile(tmp_path: Path) -> None:
    """Reaching an address and being able to find one are two permissions.

    `getaddrinfo` does not send the query itself -- it hands the name to
    mDNSResponder over a Unix domain socket, which `(remote tcp)` does not
    cover and `(remote udp)` does not either. Every connection by address
    worked while every connection by name failed, which is how the sixth real
    probe died after `turn.started`.

    Measured by connecting to the socket, not by resolving a name: a live
    lookup would send a DNS query off the machine for a test, and `localhost`
    -- the only name resolvable without doing so -- resolves even under the
    profile that broke that probe.
    """
    result = measure(tmp_path)
    assert result.name_resolution_available is True
    assert result.egress_reachable is True


def test_the_resolver_grant_is_one_socket_and_no_wider_network() -> None:
    """The narrowest rule that was measured to work, and nothing beside it.

    `(allow network-outbound)` unrestricted also fixes it, and that would open
    every outbound connection of every kind. Codex's own network policy lists
    `SystemConfiguration.DNSConfiguration`, `networkd`, `ocspd`,
    `SecurityServer` and the `net.routetable` sysctls; each was granted alone
    against a resolver probe and not one made a remote name resolvable.
    """
    rules = [
        line for line in sandbox.compose_profile().splitlines() if not line.lstrip().startswith(";")
    ]
    text = " ".join("\n".join(rules).split())
    assert '(allow network-outbound (literal "/private/var/run/mDNSResponder"))' in text

    forms = [f"(allow {form}".strip() for form in text.split("(allow ")[1:]]
    outbound = sorted(form for form in forms if "network-outbound" in form)
    # The complete set, listed rather than counted: tcp to anywhere, the
    # platform section's syslog socket, and this one resolver socket. A bare
    # `(allow network-outbound)` or a `(remote unix-socket)` wildcard would
    # also make the probe pass, and neither is here.
    assert outbound == [
        '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
        '(allow network-outbound (literal "/private/var/run/syslog"))',
        "(allow network-outbound (remote tcp))",
    ], outbound
    assert "(remote unix-socket)" not in text
    assert "(remote udp)" not in text


def test_the_native_trust_store_is_reachable_through_the_profile(tmp_path: Path) -> None:
    """Reaching a server and being able to verify it are two permissions.

    The eighth real probe resolved the name, opened the connection, started a
    turn and then spent its whole budget on `invalid peer certificate:
    UnknownIssuer`. `rustls-native-certs` reads the macOS store through
    Security.framework's `TrustSettings`, and under the profile every domain
    came back empty.

    Asked the way that library asks. A readable keychain file proves nothing
    here: the file was already readable when the probe failed.
    """
    assert measure(tmp_path).tls_trust_available is True


def test_the_trust_grant_is_two_literals_and_no_user_keychain() -> None:
    """The public anchors this machine already trusts, and nothing of the user's.

    Measured one dimension at a time: with file reads held open only
    `com.apple.SecurityServer` worked, and with mach held open only
    `/System/Library/Keychains` did. Neither is sufficient alone, so both are
    granted -- narrowed to the two files that were measured to be needed.
    `X509Anchors` sits beside them and is not one of them.
    """
    rules = [
        line for line in sandbox.compose_profile().splitlines() if not line.lstrip().startswith(";")
    ]
    text = " ".join("\n".join(rules).split())

    assert '(literal "/System/Library/Keychains/SystemRootCertificates.keychain")' in text
    assert '(literal "/System/Library/Keychains/SystemTrustSettings.plist")' in text
    assert '(allow mach-lookup (global-name "com.apple.SecurityServer"))' in text

    # The negatives are asserted against the harness section alone. The
    # platform section is Codex's own vendored minimum for a sandboxed process
    # -- it already grants `/private/var/db`, among others -- and is not this
    # profile's to police. What this test owns is what the harness adds on top.
    harness = " ".join(
        line
        for line in sandbox.READ_ISOLATION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(";")
    )
    # No keychain reaches the harness section as a directory, and no user
    # keychain reaches it at all. A subpath here would hand over every file
    # that is or ever will be placed in it.
    assert '(subpath "/System/Library/Keychains")' not in harness
    assert "/Library/Keychains" not in harness.replace("/System/Library/Keychains/", "")
    assert "X509Anchors" not in harness
    assert "Users/" not in harness
    assert "/private/var/db" not in harness
    # The only home the harness names is the isolated one it created itself.
    assert "HOME" not in harness.replace('(param "CODEX_HOME")', "")


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
    for parameter in (
        "CODEX_VENDOR",
        "WORKSPACE",
        "CODEX_HOME",
        "INSTALLATION_ID_FILE",
        "CATALOG_FILE",
    ):
        assert any(item.startswith(f"{parameter}=") for item in wrapped)


def test_the_runtime_catalog_is_readable_over_and_over(tmp_path: Path) -> None:
    """Three opens of the same path, all returning the same bytes.

    This is the property the descriptor transport could not offer and the
    reason the catalog moved to a named file at all. Codex 0.153.4 loads
    `model_catalog_json` in the initial `ConfigBuilder::build()` and again at
    `thread/start`; a `/dev/fd/N` stream answers the first load and hands the
    second an empty string, which is how the third real probe died.

    One successful read would therefore prove nothing. The probe reads three
    times and `catalog_replayable` only holds if every read agreed.
    """
    result = measure(tmp_path)
    assert result.catalog_readable is True
    assert result.catalog_replayable is True


def test_the_runtime_catalog_cannot_be_changed_from_inside(tmp_path: Path) -> None:
    """Read, and nothing else. Write, truncate, unlink and sidecar all refused.

    The named file gave up the immutability an unlinked descriptor had, and
    this is where that is paid back. A catalog the sandboxed process could
    rewrite would be a catalog the judgement no longer describes -- and the
    second config load at `thread/start` would read whatever replaced it.
    """
    result = measure(tmp_path)
    assert result.catalog_writable is False
    assert result.catalog_truncatable is False
    assert result.catalog_deletable is False
    assert result.catalog_sidecar_creatable is False
    assert result.catalog_scope_holds is True


def test_the_catalog_grant_is_one_literal_read_and_no_write() -> None:
    """`(subpath catalog-runtime)` would open the directory it is kept in."""
    rules = [
        line for line in sandbox.compose_profile().splitlines() if not line.lstrip().startswith(";")
    ]
    text = " ".join("\n".join(rules).split())
    forms = [f"(allow {form}" for form in text.split("(allow ")[1:]]
    catalog_forms = [form for form in forms if 'param "CATALOG_FILE"' in form]
    assert len(catalog_forms) == 1, catalog_forms
    assert '(literal (param "CATALOG_FILE"))' in catalog_forms[0]
    assert "(subpath" not in catalog_forms[0]
    assert "file-write" not in catalog_forms[0]


def test_the_vendor_root_is_the_tree_the_binary_needs() -> None:
    executable = Path("/opt/x/vendor/aarch64-apple-darwin/bin/codex")
    assert sandbox.codex_vendor_root(executable).name == "aarch64-apple-darwin"
