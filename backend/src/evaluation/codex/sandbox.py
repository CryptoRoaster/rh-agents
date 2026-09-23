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

import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PROFILE_DIR = Path(__file__).parent / "profiles"
PLATFORM_DEFAULTS = PROFILE_DIR / "platform-defaults.sbpl"
READ_ISOLATION = PROFILE_DIR / "read-isolation.sbpl"

PROBE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SandboxRoots:
    """The only paths the Codex process may read.

    Deliberately three, and deliberately narrow. Not `HOME`, not `/Volumes`,
    not the repository: each of those is a class of file the attempt has no
    business seeing.
    """

    codex_vendor: Path
    workspace: Path
    codex_home: Path

    def parameters(self) -> dict[str, str]:
        return {
            "CODEX_VENDOR": str(self.codex_vendor.resolve()),
            "WORKSPACE": str(self.workspace.resolve()),
            "CODEX_HOME": str(self.codex_home.resolve()),
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
    """What a read attempt under the profile actually achieved."""

    allowed_readable: bool
    forbidden_readable: bool
    sentinel_readable: bool

    @property
    def boundary_holds(self) -> bool:
        return self.allowed_readable and not self.forbidden_readable and not self.sentinel_readable


def probe_read_boundary(roots: SandboxRoots, profile: Path, outside: Path) -> ProbeResult:
    """Check the boundary with sentinel files rather than with real secrets.

    Reading an actual `.env` to see whether it is blocked would be the wrong
    experiment twice over: it proves one path rather than a class, and it puts a
    real secret in the way of a test. Sentinels stand in for the classes.
    """
    allowed = roots.workspace.resolve() / "sandbox-probe-allowed.txt"
    forbidden = outside.resolve() / "sandbox-probe-forbidden.txt"
    sentinel = outside.resolve() / "sandbox-probe-sentinel.txt"
    allowed.write_text("allowed\n", encoding="utf-8")
    forbidden.write_text("forbidden\n", encoding="utf-8")
    sentinel.write_text("sentinel\n", encoding="utf-8")

    script = "; ".join(
        f'if cat "{path}" >/dev/null 2>&1; then echo "{name} OK"; else echo "{name} DENIED"; fi'
        for name, path in (
            ("ALLOWED", allowed),
            ("FORBIDDEN", forbidden),
            ("SENTINEL", sentinel),
        )
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
        return ProbeResult(False, True, True)

    output = completed.stdout
    return ProbeResult(
        allowed_readable="ALLOWED OK" in output,
        forbidden_readable="FORBIDDEN OK" in output,
        sentinel_readable="SENTINEL OK" in output,
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
    "probe_read_boundary",
    "wrap",
    "write_profile",
]
