"""What a failed attempt is allowed to say about itself.

The first authorised real run exited 1 after 375 ms with no events, and the
harness could say nothing further: `run_bounded` captured the child's
`stderr_tail`, `_run_attempt` used only `exit_code` and `cleanup`, and the
bytes were dropped. A failure that cannot be diagnosed is a failure that gets
retried blindly, which is the one thing this harness exists to prevent.

Handing the raw tail out instead would be the wrong repair. A provider error, an
auth error or a CLI panic can carry an `Authorization` header, a refresh token
or a cookie, and it can carry the user's absolute paths. So the bytes never
leave this module. What leaves is a `ProcessDiagnostic`: a few short lines that
went through redaction, plus counts.

The design rule here is that redaction fails *closed*. Every step is inside one
try block, and if anything at all goes wrong the result is the single line
`[DIAGNOSTIC_REDACTION_FAILED]`. There is no path on which unredacted bytes
reach a caller, including the path where the redactor itself raises.
"""

import re
from dataclasses import dataclass
from pathlib import Path

MAX_LINES = 8
MAX_LINE_CHARS = 240
MAX_TOTAL_CHARS = 2048

REDACTED_LINE = "[REDACTED_SENSITIVE_LINE]"
REDACTION_FAILED = "[DIAGNOSTIC_REDACTION_FAILED]"
MASKED_VALUE = "[REDACTED_VALUE]"

# A line mentioning any of these is dropped whole rather than edited. Editing
# assumes the shape of the value, and a shape assumption is exactly what fails
# on the error message nobody anticipated.
SENSITIVE_TERMS = (
    "authorization",
    "bearer",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "cookie",
    "set-cookie",
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Defensive masking for anything token-shaped that survived the line check: a
# JWT, or any long unbroken run of credential-ish characters. Deliberately
# blunt -- a masked build identifier costs nothing, a leaked token costs a lot.
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?\b")
LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")


@dataclass(frozen=True)
class ProcessDiagnostic:
    """Why a child failed, in a form that is safe to report.

    Deliberately has no raw field. `stderr_captured_bytes` is how much was read
    off the pipe, which is worth knowing when `safe_lines` is short because
    everything in it was redacted rather than because nothing was written.
    """

    exit_code: int
    stderr_present: bool = False
    stderr_captured_bytes: int = 0
    stderr_was_truncated: bool = False
    safe_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathAliases:
    """Bound paths, replaced by their role before anything is reported.

    A diagnostic is for the harness's behaviour, not for publishing where this
    machine keeps the user's home. Longest first on substitution, so
    `<CODEX_HOME>` inside a temporary root does not get half-rewritten by the
    shorter root that contains it.
    """

    entries: tuple[tuple[str, str], ...] = ()

    @classmethod
    def build(cls, **paths: Path | str | None) -> "PathAliases":
        collected: list[tuple[str, str]] = []
        for role, value in paths.items():
            if value is None:
                continue
            text = str(value)
            if len(text) < 2:
                continue
            collected.append((text, f"<{role.upper()}>"))
            resolved = str(Path(text).resolve()) if text.startswith("/") else text
            if resolved != text:
                collected.append((resolved, f"<{role.upper()}>"))
        collected.sort(key=lambda item: len(item[0]), reverse=True)
        return cls(entries=tuple(collected))

    def apply(self, line: str) -> str:
        for needle, alias in self.entries:
            line = line.replace(needle, alias)
        return line


EMPTY_ALIASES = PathAliases()


def _sensitive(line: str) -> bool:
    lowered = line.lower()
    return any(term in lowered for term in SENSITIVE_TERMS)


def _mask(line: str) -> str:
    line = JWT.sub(MASKED_VALUE, line)
    return LONG_OPAQUE.sub(MASKED_VALUE, line)


def redact(stderr_tail: bytes, aliases: PathAliases = EMPTY_ALIASES) -> tuple[str, ...]:
    """Turn the captured tail into at most `MAX_LINES` safe lines.

    Fails closed as a whole: any exception anywhere in here -- decoding,
    substitution, a regex pathology -- produces the single failure marker
    rather than partial output or the input.
    """
    try:
        # errors="replace" rather than a raise: invalid UTF-8 is a diagnostic
        # in itself and must not become a second failure.
        text = stderr_tail.decode("utf-8", errors="replace")
        safe: list[str] = []
        budget = MAX_TOTAL_CHARS
        for raw in text.splitlines():
            if len(safe) >= MAX_LINES or budget <= 0:
                break
            line = ANSI.sub("", raw).strip()
            if not line:
                continue
            if _sensitive(line):
                # A run of credential-shaped lines collapses into one marker.
                # Keeping each would say nothing more -- they are all the same
                # placeholder -- while spending the line budget that the one
                # useful message further down needs. A failure whose only
                # readable line was pushed out by placeholders is the problem
                # this whole channel exists to fix.
                if safe and safe[-1] == REDACTED_LINE:
                    continue
                line = REDACTED_LINE
            else:
                line = _mask(aliases.apply(line))[:MAX_LINE_CHARS]
            if not line:
                continue
            safe.append(line)
            budget -= len(line)
        return tuple(safe)
    except Exception:  # noqa: BLE001 - fail closed, never leak the input
        return (REDACTION_FAILED,)


def diagnose(
    *, exit_code: int, stderr_tail: bytes, captured_limit: int, aliases: PathAliases
) -> ProcessDiagnostic:
    """Build the reportable diagnostic. The raw bytes stop here."""
    try:
        return ProcessDiagnostic(
            exit_code=exit_code,
            stderr_present=bool(stderr_tail),
            stderr_captured_bytes=len(stderr_tail),
            stderr_was_truncated=captured_limit > 0 and len(stderr_tail) >= captured_limit,
            safe_lines=redact(stderr_tail, aliases),
        )
    except Exception:  # noqa: BLE001 - a diagnostic may never be the failure
        return ProcessDiagnostic(exit_code=exit_code, safe_lines=(REDACTION_FAILED,))


__all__ = [
    "EMPTY_ALIASES",
    "MASKED_VALUE",
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "MAX_TOTAL_CHARS",
    "REDACTED_LINE",
    "REDACTION_FAILED",
    "SENSITIVE_TERMS",
    "PathAliases",
    "ProcessDiagnostic",
    "diagnose",
    "redact",
]
