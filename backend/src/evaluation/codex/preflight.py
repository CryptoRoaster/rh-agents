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

Neither probe starts a turn or calls a model.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import CleanupReport, EvaluationFailure, OutputLimits
from src.evaluation.codex.process import ProcessError, run_bounded

CHATGPT_MARKER = "Logged in using ChatGPT"
VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)\b")


@dataclass(frozen=True)
class ProbeOutput:
    text: str
    cleanup: CleanupReport


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
    """Run a probe and return its stdout, bounded the same way the attempt is."""
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
    return ProbeOutput(
        text=b"".join(collected).decode("utf-8", errors="replace"),
        cleanup=result.cleanup,
    )


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
    found = VERSION_PATTERN.search(output.text)
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
    return PreflightResult(chatgpt_session=CHATGPT_MARKER in output.text, cleanup=output.cleanup)
