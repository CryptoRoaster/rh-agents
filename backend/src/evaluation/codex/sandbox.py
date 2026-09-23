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
import os
import platform
import socket
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PROFILE_DIR = Path(__file__).parent / "profiles"
PLATFORM_DEFAULTS = PROFILE_DIR / "platform-defaults.sbpl"
READ_ISOLATION = PROFILE_DIR / "read-isolation.sbpl"

PROBE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SandboxRoots:
    """The paths the harness section of the profile names.

    Three readable roots and one writable file. Not `HOME`, not `/Volumes`, not
    the repository: each of those is a class of file the attempt has no business
    seeing. The platform section adds system and loader paths on top -- those
    are runtime, not user data, and they are listed in the profile itself.
    """

    codex_vendor: Path
    workspace: Path
    codex_home: Path

    @property
    def auth_file(self) -> Path:
        return self.codex_home / "auth.json"

    def parameters(self) -> dict[str, str]:
        return {
            "CODEX_VENDOR": str(self.codex_vendor.resolve()),
            "WORKSPACE": str(self.workspace.resolve()),
            "CODEX_HOME": str(self.codex_home.resolve()),
            # Resolved from the home rather than the file, which may not exist
            # yet when the profile is written.
            "AUTH_FILE": str(self.codex_home.resolve() / "auth.json"),
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


def write_profile(directory: Path) -> Path:
    """Materialise the composed profile so sandbox-exec can load it."""
    handle, name = tempfile.mkstemp(dir=directory, prefix="read-isolation-", suffix=".sb")
    try:
        os.write(handle, compose_profile().encode("utf-8"))
        os.fsync(handle)
    finally:
        os.close(handle)
    return Path(name)


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
    other_file_creatable: bool
    egress_reachable: bool

    @property
    def read_boundary_holds(self) -> bool:
        return self.allowed_readable and not self.forbidden_readable and not self.sentinel_readable

    @property
    def write_scope_holds(self) -> bool:
        """One file writable, and no way to put anything beside it."""
        return self.auth_readable and self.auth_rewritable and not self.other_file_creatable


DENIED_EVERYTHING = ProbeResult(
    allowed_readable=False,
    forbidden_readable=True,
    sentinel_readable=True,
    auth_readable=False,
    auth_rewritable=False,
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
    secret in the way of a test. The auth file used here is a placeholder, never
    a real credential.
    """
    workspace = roots.workspace.resolve()
    outside = outside.resolve()
    allowed = workspace / "sandbox-probe-allowed.txt"
    forbidden = outside / "sandbox-probe-forbidden.txt"
    sentinel = outside / "sandbox-probe-sentinel.txt"
    allowed.write_text("allowed\n", encoding="utf-8")
    forbidden.write_text("forbidden\n", encoding="utf-8")
    sentinel.write_text("sentinel\n", encoding="utf-8")

    auth = Path(roots.parameters()["AUTH_FILE"])
    beside = auth.parent / "sandbox-probe-should-not-exist.txt"
    existed = auth.exists()
    if not existed:
        auth.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
        os.chmod(auth, 0o600)

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
                    )
                ),
                f'if cp "{auth}" "{auth}" 2>/dev/null && : > /dev/null; then :; fi',
                f'if (cat "{auth}" > "{auth}.probe" 2>/dev/null); then echo "SIDECAR OK";'
                f' else echo "SIDECAR DENIED"; fi',
                f"if printf '%s' \"$(cat '{auth}')\" > \"{auth}\" 2>/dev/null;"
                f' then echo "AUTHWRITE OK"; else echo "AUTHWRITE DENIED"; fi',
                f'if : > "{beside}" 2>/dev/null; then echo "BESIDE OK";'
                f' else echo "BESIDE DENIED"; fi',
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

    output = completed.stdout
    return ProbeResult(
        allowed_readable="ALLOWED OK" in output,
        forbidden_readable="FORBIDDEN OK" in output,
        sentinel_readable="SENTINEL OK" in output,
        auth_readable="AUTHREAD OK" in output,
        auth_rewritable="AUTHWRITE OK" in output,
        other_file_creatable="BESIDE OK" in output or "SIDECAR OK" in output,
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
    "SandboxRoots",
    "available",
    "codex_vendor_root",
    "compose_profile",
    "macos",
    "DENIED_EVERYTHING",
    "LoopbackTarget",
    "probe_boundaries",
    "wrap",
    "write_profile",
]
