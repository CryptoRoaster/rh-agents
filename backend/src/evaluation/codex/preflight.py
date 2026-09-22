"""Two bounded probes that run before the attempt: which build, and which login.

Each is its own process with its own counter and its own slice of the budget.
Folding either into the attempt would let one start hide behind another, and
"at most one `codex exec`" would stop meaning anything. Both use the same
scrubbed child environment as the attempt -- probing a session the attempt
would not use tells you nothing.

The version probe exists because a configured version string is not a fact. A
build is whatever is on disk, and a stale or edited setting would let this
harness run against a CLI whose event contract, default tools and flag names it
has never seen. So the launcher is asked, and its answer is what decides.

The login probe reads the CLI's own answer. It does not open, copy or parse any
auth file, and no token is ever read into this process.

Both probes read stdout *and* stderr. That is not defensive padding: in Codex
0.153.4 `run_login_status` (`codex-rs/cli/src/login.rs`) reports every outcome
with `eprintln!`, so the ChatGPT status line arrives on stderr and a probe
watching stdout alone would see nothing at all. The exit code cannot stand in
for it either -- an API-key session also exits 0 -- so the marker has to be
matched, and it has to be matched on the channel the CLI actually uses.

The channels stay apart and the match is on a whole line. Concatenating the two
buffers could splice one stream's tail onto the other's head and produce a line
neither ever emitted, and a substring test would accept the marker wherever it
appeared -- quoted inside an error, or as the prefix of a longer status.

Neither probe starts a turn or calls a model.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import CleanupReport, EvaluationFailure, OutputLimits
from src.evaluation.codex.process import ProcessError, run_bounded

# Exactly the line 0.153.4 emits for AuthMode::Chatgpt and ChatgptAuthTokens.
# The other "Logged in using ..." lines -- API key, access token, personal
# access token, Bedrock -- also exit 0, so nothing but this marker may be taken
# as a ChatGPT subscription session.
CHATGPT_MARKER = "Logged in using ChatGPT"
VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)\b")


@dataclass(frozen=True)
class ProbeOutput:
    """What a probe wrote, kept as lines and with the channels kept apart.

    Two channels are never concatenated into one buffer. Joining them could
    splice the tail of one onto the head of the other and manufacture a line
    neither stream ever emitted -- including, in the worst case, the very marker
    the login probe is looking for.
    """

    stdout_lines: tuple[str, ...]
    stderr_lines: tuple[str, ...]
    cleanup: CleanupReport

    def emitted(self) -> tuple[str, ...]:
        return self.stdout_lines + self.stderr_lines

    def text(self) -> str:
        return "\n".join(self.emitted())


@dataclass(frozen=True)
class PreflightResult:
    chatgpt_session: bool
    cleanup: CleanupReport


@dataclass(frozen=True)
class VersionResult:
    version: str | None
    cleanup: CleanupReport


async def _capture(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    limits: OutputLimits,
    deadline: Deadline,
    failure: EvaluationFailure,
) -> ProbeOutput:
    """Run a probe and return what it wrote, bounded the same way the attempt is."""
    collected: list[bytes] = []
    budget = limits.max_final_message_bytes
    held = 0

    def collect(line: bytes) -> None:
        nonlocal held
        if held + len(line) <= budget:
            collected.append(line)
            held += len(line)

    try:
        result = await run_bounded(
            arguments=arguments,
            environment=environment,
            working_directory=working_directory,
            stdin_payload=b"",
            limits=limits,
            deadline=deadline,
            on_stdout_line=collect,
        )
    except asyncio.CancelledError:
        raise
    except ProcessError as error:
        raise ProcessError(failure, error.reason_code, error.cleanup) from None

    if result.exit_code != 0:
        raise ProcessError(failure, f"EXIT_{result.exit_code}", result.cleanup)
    # Both channels, because the CLI answers on stderr and this probe must not
    # depend on guessing which one a given subcommand happens to use.
    return ProbeOutput(
        stdout_lines=_lines(b"".join(collected)),
        stderr_lines=_lines(result.stderr_tail[:budget]),
        cleanup=result.cleanup,
    )


def _lines(raw: bytes) -> tuple[str, ...]:
    text = raw.decode("utf-8", errors="replace")
    return tuple(line.strip() for line in text.splitlines() if line.strip())


async def check_cli_version(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    limits: OutputLimits,
    deadline: Deadline,
) -> VersionResult:
    """Ask the launcher which build it is. `None` means it did not say."""
    output = await _capture(
        arguments=arguments,
        environment=environment,
        working_directory=working_directory,
        limits=limits,
        deadline=deadline,
        failure=EvaluationFailure.VERSION_CHECK_FAILED,
    )
    found = VERSION_PATTERN.search(output.text())
    return VersionResult(
        version=found.group(1) if found is not None else None, cleanup=output.cleanup
    )


async def check_chatgpt_login(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    limits: OutputLimits,
    deadline: Deadline,
) -> PreflightResult:
    """Return whether the CLI reports an active ChatGPT session."""
    output = await _capture(
        arguments=arguments,
        environment=environment,
        working_directory=working_directory,
        limits=limits,
        deadline=deadline,
        failure=EvaluationFailure.PREFLIGHT_FAILED,
    )
    # An exact line, not a substring: a marker quoted inside an error message,
    # or the prefix of a longer status, must not pass for a session. Lines are
    # matched per channel, so nothing can be spliced together across the two.
    return PreflightResult(
        chatgpt_session=any(line == CHATGPT_MARKER for line in output.emitted()),
        cleanup=output.cleanup,
    )
