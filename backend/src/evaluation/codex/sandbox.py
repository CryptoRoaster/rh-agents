"""An outer read boundary for the Codex process itself, on macOS.

Codex's `--sandbox read-only` does not provide one.
`SandboxPolicy::has_full_disk_read_access` returns `true` for every variant,
`seatbelt.rs` turns that into a blanket `(allow file-read*)`, and the profile it
builds wraps the *commands the agent runs* rather than the agent. The Codex
process therefore reads with the caller's own rights: the repository, a `.env`,
an SSH key, anything.

So the harness puts its own Seatbelt profile in front of Codex, denying by
default and allowing only what has been shown to be needed. The order is

    harness -> launch_gate -> /usr/bin/sandbox-exec -f <profile> -- codex ...

which keeps everything the ownership work established: the gate is still the
process group leader, still holds the group before anything runs, and
`sandbox-exec` applies the profile and `exec`s in the same process, so the pid,
the group and every inherited descriptor -- including the pinned catalog --
carry through.

Two things this module insists on. Paths are **resolved** before they reach the
profile, because Seatbelt matches the real filesystem and `/tmp` would never
match `/private/tmp`. And there is no fallback: on macOS the outer sandbox is
required, and if it cannot be established the attempt is refused rather than
run without it.
"""

import contextlib
import hashlib
import os
import platform
import socket
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex.auth_home import AUTH_FILE, INSTALLATION_ID_FILE

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PROFILE_DIR = Path(__file__).parent / "profiles"
PLATFORM_DEFAULTS = PROFILE_DIR / "platform-defaults.sbpl"
READ_ISOLATION = PROFILE_DIR / "read-isolation.sbpl"

PROBE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SandboxRoots:
    """The paths the harness section of the profile names.

    Three readable roots and two writable files. Not `HOME`, not `/Volumes`,
    not the repository: each of those is a class of file the attempt has no
    business seeing. The platform section adds system and loader paths on top
    -- those are runtime, not user data, and they are listed in the profile
    itself.

    The two writable paths are `literal`, never `subpath`. The directory that
    holds them stays unwritable, which is what keeps the file set closed and
    `AUTH_HOME_ISOLATION` meaningful.
    """

    codex_vendor: Path
    workspace: Path
    codex_home: Path

    @property
    def auth_file(self) -> Path:
        return self.codex_home / AUTH_FILE

    @property
    def installation_id_file(self) -> Path:
        return self.codex_home / INSTALLATION_ID_FILE

    def parameters(self) -> dict[str, str]:
        # Resolved from the home rather than from the files, which need not
        # exist yet when the profile is written.
        home = self.codex_home.resolve()
        return {
            "CODEX_VENDOR": str(self.codex_vendor.resolve()),
            "WORKSPACE": str(self.workspace.resolve()),
            "CODEX_HOME": str(home),
            "AUTH_FILE": str(home / AUTH_FILE),
            "INSTALLATION_ID_FILE": str(home / INSTALLATION_ID_FILE),
        }


def macos() -> bool:
    return platform.system() == "Darwin"


def available() -> bool:
    """Whether the outer sandbox can be established at all."""
    return (
        macos()
        and SANDBOX_EXEC.is_file()
        and os.access(SANDBOX_EXEC, os.X_OK)
        and PLATFORM_DEFAULTS.is_file()
        and READ_ISOLATION.is_file()
    )


def compose_profile() -> str:
    """Join the vendor platform section with the harness allowlist.

    `(import ...)` is not used: sandbox-exec resolves it against its own search
    path rather than the profile's directory, so the text is assembled here.
    """
    platform_section = PLATFORM_DEFAULTS.read_text(encoding="utf-8")
    harness_section = READ_ISOLATION.read_text(encoding="utf-8")
    return "\n".join(
        [
            "(version 1)",
            "; Deny by default. Every allowance below is explicit.",
            "(deny default)",
            "",
            "; ---- platform section: Codex's own minimum for a sandboxed process ----",
            platform_section,
            "",
            harness_section,
        ]
    )


@dataclass(frozen=True)
class WrittenProfile:
    """A profile on disk together with the digest of what was written.

    The digest is the point. Between a preflight that measures a profile and an
    exec that loads one, the bytes could differ -- a different temporary file, a
    re-composition from source files that changed in between, an edit in place.
    Carrying the digest lets the exec path establish that it is loading the
    policy that was measured, rather than a policy assembled from the same two
    file names.
    """

    path: Path
    digest: str

    def still_matches(self) -> bool:
        """Re-read the file and compare. False means it changed or vanished."""
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest() == self.digest
        except OSError:
            return False


def write_profile(directory: Path) -> Path:
    """Materialise the composed profile so sandbox-exec can load it."""
    return write_bound_profile(directory).path


def write_bound_profile(directory: Path) -> WrittenProfile:
    """Materialise the composed profile and keep the digest of its bytes."""
    payload = compose_profile().encode("utf-8")
    handle, name = tempfile.mkstemp(dir=directory, prefix="read-isolation-", suffix=".sb")
    try:
        written = 0
        while written < len(payload):
            written += os.write(handle, payload[written:])
        os.fsync(handle)
    finally:
        os.close(handle)
    return WrittenProfile(path=Path(name), digest=hashlib.sha256(payload).hexdigest())


def wrap(command: list[str], profile: Path, roots: SandboxRoots) -> list[str]:
    """Put `sandbox-exec` in front of a command, with the roots as parameters."""
    wrapped = [str(SANDBOX_EXEC), "-f", str(profile.resolve())]
    for key, value in sorted(roots.parameters().items()):
        wrapped += ["-D", f"{key}={value}"]
    wrapped.append("--")
    return wrapped + command


@dataclass(frozen=True)
class ProbeResult:
    """What the profile actually permits, measured rather than assumed."""

    allowed_readable: bool
    forbidden_readable: bool
    sentinel_readable: bool
    auth_readable: bool
    auth_rewritable: bool
    installation_id_readable: bool
    installation_id_rewritable: bool
    other_file_creatable: bool
    egress_reachable: bool

    @property
    def read_boundary_holds(self) -> bool:
        return self.allowed_readable and not self.forbidden_readable and not self.sentinel_readable

    @property
    def write_scope_holds(self) -> bool:
        """Exactly the two expected files writable, and nothing beside them.

        `installation_id` is checked the way Codex opens it -- read-write on an
        existing file -- rather than as a plain overwrite, because that is the
        operation the app-server startup performs and the one the policy had to
        be measured against.
        """
        return (
            self.auth_readable
            and self.auth_rewritable
            and self.installation_id_readable
            and self.installation_id_rewritable
            and not self.other_file_creatable
        )


DENIED_EVERYTHING = ProbeResult(
    allowed_readable=False,
    forbidden_readable=True,
    sentinel_readable=True,
    auth_readable=False,
    auth_rewritable=False,
    installation_id_readable=False,
    installation_id_rewritable=False,
    other_file_creatable=True,
    egress_reachable=False,
)


class LoopbackTarget:
    """A local listener, so egress is measured without contacting anyone.

    Reaching out to a real endpoint would test the network rather than the
    profile, and would mean traffic leaving the machine for a test. Loopback
    answers the only question that matters here: does the profile permit an
    outbound TCP connection at all.
    """

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = int(self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            with contextlib.suppress(OSError):
                connection.sendall(b"HELLO\n")
                connection.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._socket.close()
        self._thread.join(timeout=2)


def probe_boundaries(roots: SandboxRoots, profile: Path, outside: Path) -> ProbeResult:
    """Measure the read boundary, the write scope and egress under the profile.

    Sentinels stand in for the repository, HOME and credential classes. Reading
    an actual `.env` to see whether it is blocked would be the wrong experiment
    twice over: it proves one path rather than a class, and it puts a real
    secret in the way of a test. Both files used here are placeholders, never a
    real credential and never the machine's own installation identifier.

    `installation_id` is exercised the way Codex opens it: `exec 3<> file`, a
    read-write open on an existing file, then a mode repair. A plain overwrite
    would pass under a policy that the app-server startup still fails on, which
    is exactly the mistake that produced the second probe's failure.
    """
    workspace = roots.workspace.resolve()
    outside = outside.resolve()
    allowed = workspace / "sandbox-probe-allowed.txt"
    forbidden = outside / "sandbox-probe-forbidden.txt"
    sentinel = outside / "sandbox-probe-sentinel.txt"
    allowed.write_text("allowed\n", encoding="utf-8")
    forbidden.write_text("forbidden\n", encoding="utf-8")
    sentinel.write_text("sentinel\n", encoding="utf-8")

    parameters = roots.parameters()
    auth = Path(parameters["AUTH_FILE"])
    marker = Path(parameters["INSTALLATION_ID_FILE"])
    beside = auth.parent / "sandbox-probe-should-not-exist.txt"
    if not auth.exists():
        auth.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
        os.chmod(auth, 0o600)
    if not marker.exists():
        marker.write_text("00000000-0000-4000-8000-000000000000", encoding="utf-8")
        os.chmod(marker, 0o644)

    target = LoopbackTarget()
    try:
        script = "\n".join(
            [
                *(
                    f'if cat "{path}" >/dev/null 2>&1; then echo "{name} OK";'
                    f' else echo "{name} DENIED"; fi'
                    for name, path in (
                        ("ALLOWED", allowed),
                        ("FORBIDDEN", forbidden),
                        ("SENTINEL", sentinel),
                        ("AUTHREAD", auth),
                        ("INSTALLREAD", marker),
                    )
                ),
                f'if cp "{auth}" "{auth}" 2>/dev/null && : > /dev/null; then :; fi',
                f'if (cat "{auth}" > "{auth}.probe" 2>/dev/null); then echo "SIDECAR OK";'
                f' else echo "SIDECAR DENIED"; fi',
                f"if printf '%s' \"$(cat '{auth}')\" > \"{auth}\" 2>/dev/null;"
                f' then echo "AUTHWRITE OK"; else echo "AUTHWRITE DENIED"; fi',
                # Opened read-write and then mode-repaired, which is what
                # `resolve_installation_id` does. Each check runs in a subshell:
                # a failed `exec` redirection ends a non-interactive shell
                # outright, and a denial must read as a denial rather than as
                # silence.
                f'if ( exec 3<> "{marker}"; exec 3>&- ) 2>/dev/null &&'
                f' /bin/chmod 644 "{marker}" 2>/dev/null;'
                f' then echo "INSTALLRDWR OK"; else echo "INSTALLRDWR DENIED"; fi',
                f'if : > "{beside}" 2>/dev/null; then echo "BESIDE OK";'
                f' else echo "BESIDE DENIED"; fi',
                f'if : > "{marker}.probe" 2>/dev/null; then echo "MARKERSIDECAR OK";'
                f' else echo "MARKERSIDECAR DENIED"; fi',
                f"if /usr/bin/nc -w 3 127.0.0.1 {target.port} < /dev/null > /dev/null 2>&1;"
                f' then echo "EGRESS OK"; else echo "EGRESS DENIED"; fi',
            ]
        )
        command = wrap(["/bin/sh", "-c", script], profile, roots)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return DENIED_EVERYTHING
    finally:
        target.close()
        beside.unlink(missing_ok=True)
        Path(f"{auth}.probe").unlink(missing_ok=True)
        Path(f"{marker}.probe").unlink(missing_ok=True)

    output = completed.stdout
    return ProbeResult(
        allowed_readable="ALLOWED OK" in output,
        forbidden_readable="FORBIDDEN OK" in output,
        sentinel_readable="SENTINEL OK" in output,
        auth_readable="AUTHREAD OK" in output,
        auth_rewritable="AUTHWRITE OK" in output,
        installation_id_readable="INSTALLREAD OK" in output,
        installation_id_rewritable="INSTALLRDWR OK" in output,
        other_file_creatable=any(
            marker in output for marker in ("BESIDE OK", "SIDECAR OK", "MARKERSIDECAR OK")
        ),
        egress_reachable="EGRESS OK" in output,
    )


def codex_vendor_root(executable: Path) -> Path:
    """The directory tree the platform binary needs, from the binary's path.

    The binary lives in `<vendor>/bin/codex` and reaches sibling resources, so
    the vendor root is what has to be readable -- not the whole install prefix.
    """
    return executable.resolve().parent.parent


__all__ = [
    "PROFILE_DIR",
    "SANDBOX_EXEC",
    "ProbeResult",
    "WrittenProfile",
    "SandboxRoots",
    "available",
    "codex_vendor_root",
    "compose_profile",
    "macos",
    "DENIED_EVERYTHING",
    "LoopbackTarget",
    "probe_boundaries",
    "wrap",
    "write_bound_profile",
    "write_profile",
]
