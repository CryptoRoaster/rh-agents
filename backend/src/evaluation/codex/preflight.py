"""Check that the CLI is signed in with ChatGPT, without starting a turn.

This is its own process with its own budget, counted separately from the
evaluation attempt. Folding it into the attempt would let one start hide behind
another, and "at most one `codex exec`" would stop meaning anything.

It runs `codex login status`, which opens no turn and calls no model, and it
uses the same scrubbed child environment as the attempt. Running it with an
inherited environment would defeat the point: the probe would report on a
session the attempt never uses.

The probe reads the CLI's own answer. It does not open, copy or parse any auth
file, and no token is ever read into this process.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import CleanupReport, EvaluationFailure, OutputLimits
from src.evaluation.codex.process import ProcessError, run_bounded

CHATGPT_MARKER = "Logged in using ChatGPT"


@dataclass(frozen=True)
class PreflightResult:
    chatgpt_session: bool
    cleanup: CleanupReport


async def check_chatgpt_login(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    limits: OutputLimits,
    deadline: Deadline,
) -> PreflightResult:
    """Return whether the CLI reports an active ChatGPT session."""
    lines: list[bytes] = []
    budget = limits.max_final_message_bytes

    def collect(line: bytes) -> None:
        if sum(len(item) for item in lines) + len(line) <= budget:
            lines.append(line)

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
        raise ProcessError(
            EvaluationFailure.PREFLIGHT_FAILED, error.reason_code, error.cleanup
        ) from None

    if result.exit_code != 0:
        raise ProcessError(
            EvaluationFailure.PREFLIGHT_FAILED, "LOGIN_STATUS_FAILED", result.cleanup
        )
    answer = b"".join(lines).decode("utf-8", errors="replace")
    return PreflightResult(chatgpt_session=CHATGPT_MARKER in answer, cleanup=result.cleanup)
