"""Start one child, read it under caps, and always account for its end.

Three rules shape this module.

Both pipes are drained concurrently and both are capped. `communicate()` is not
used: it buffers without limit, and a child that writes faster than it exits
would grow the parent's memory until something else broke first. A single
unterminated line is capped separately from the stream total, because one
endless line would otherwise defeat a per-stream cap.

An overflow ends the attempt. Whatever was read up to that point is discarded
rather than parsed, so a truncated stream can never be mistaken for a short one.

Cleanup always runs -- after success, after a timeout, after an error during
startup, and after an outer cancellation -- and it always reports what it
achieved. The child is started in its own session so a signal reaches the whole
process group rather than only the direct child, which matters because the npm
entry point re-spawns the real binary. Even so, a clean report is not an
operating-system guarantee: it says a signal was delivered and the direct child
was reaped inside the reserved time. A descendant that left its process group is
beyond what this can see, and an overrun is reported instead of ignored.

An outer `CancelledError` is re-raised after cleanup finishes, never swallowed.
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import CleanupReport, EvaluationFailure, OutputLimits

TERMINATE_GRACE_SECONDS = 0.5


class ProcessError(Exception):
    """The child could not be run to a usable end."""

    def __init__(
        self, failure: EvaluationFailure, reason_code: str, cleanup: CleanupReport
    ) -> None:
        self.failure = failure
        self.reason_code = reason_code
        self.cleanup = cleanup
        super().__init__(f"{failure.value}:{reason_code}")


class AbortedByConsumer(Exception):
    """The stdout consumer rejected the stream, so the child was stopped.

    The consumer's own exception is carried unchanged; only the cleanup report
    is added, because the caller cannot observe cleanup from outside.
    """

    def __init__(self, cause: Exception, cleanup: CleanupReport) -> None:
        self.cause = cause
        self.cleanup = cleanup
        super().__init__(str(cause))


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stderr_tail: bytes
    cleanup: CleanupReport


async def run_bounded(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    stdin_payload: bytes,
    limits: OutputLimits,
    deadline: Deadline,
    on_stdout_line: Callable[[bytes], None],
) -> ProcessResult:
    """Run one child to completion under one deadline and fixed output caps."""
    if deadline.work_exhausted:
        raise ProcessError(EvaluationFailure.DEADLINE_EXCEEDED, "NO_TIME_TO_START", CleanupReport())
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(working_directory),
            start_new_session=True,
            limit=limits.max_line_bytes,
        )
    except (OSError, ValueError) as error:
        raise ProcessError(
            EvaluationFailure.PROCESS_START_FAILED,
            type(error).__name__.upper(),
            CleanupReport(),
        ) from None

    stderr_tail = b""
    try:
        stderr_tail = await _run_pipes(
            process=process,
            stdin_payload=stdin_payload,
            limits=limits,
            deadline=deadline,
            on_stdout_line=on_stdout_line,
        )
        exit_code = await _await_exit(process, deadline)
    except asyncio.CancelledError:
        # The caller gave up. Clean up first, then let the cancellation through
        # unchanged; swallowing it would strand the caller's own shutdown.
        await _terminate(process, deadline)
        raise
    except ProcessError as error:
        cleanup = await _terminate(process, deadline)
        raise ProcessError(error.failure, error.reason_code, cleanup) from None
    except Exception as error:
        cleanup = await _terminate(process, deadline)
        raise AbortedByConsumer(error, cleanup) from None
    except BaseException:
        await _terminate(process, deadline)
        raise

    cleanup = await _terminate(process, deadline)
    if not cleanup.complete:
        raise ProcessError(
            EvaluationFailure.CLEANUP_INCOMPLETE, cleanup.error_code or "CLEANUP_OVERRAN", cleanup
        )
    return ProcessResult(exit_code=exit_code, stderr_tail=stderr_tail, cleanup=cleanup)


async def _run_pipes(
    *,
    process: asyncio.subprocess.Process,
    stdin_payload: bytes,
    limits: OutputLimits,
    deadline: Deadline,
    on_stdout_line: Callable[[bytes], None],
) -> bytes:
    stderr_chunks: list[bytes] = []
    tasks = [
        asyncio.create_task(_feed_stdin(process, stdin_payload)),
        asyncio.create_task(
            _pump_stdout(process, limits, on_stdout_line),
        ),
        asyncio.create_task(_pump_stderr(process, limits, stderr_chunks)),
    ]
    try:
        await _gather_within(tasks, deadline)
    except TimeoutError:
        raise ProcessError(
            EvaluationFailure.DEADLINE_EXCEEDED, "WORK_BUDGET_EXHAUSTED", CleanupReport()
        ) from None
    return b"".join(stderr_chunks)


async def _gather_within(tasks: list[asyncio.Task[None]], deadline: Deadline) -> None:
    gathered = asyncio.gather(*tasks)
    try:
        await asyncio.wait_for(gathered, timeout=deadline.remaining_for_work)
    except BaseException:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _feed_stdin(process: asyncio.subprocess.Process, payload: bytes) -> None:
    writer = process.stdin
    if writer is None:  # pragma: no cover - PIPE is always requested
        return
    try:
        writer.write(payload)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        # A child that exits before reading its input is not an error here; the
        # event stream decides whether the attempt succeeded.
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            writer.close()


async def _pump_stdout(
    process: asyncio.subprocess.Process,
    limits: OutputLimits,
    on_stdout_line: Callable[[bytes], None],
) -> None:
    reader = process.stdout
    if reader is None:  # pragma: no cover - PIPE is always requested
        return
    total = 0
    while True:
        line = await _read_line(reader, EvaluationFailure.STDOUT_OVERFLOW)
        if not line:
            return
        total += len(line)
        if total > limits.max_stdout_bytes:
            raise ProcessError(
                EvaluationFailure.STDOUT_OVERFLOW, "STDOUT_CAP_EXCEEDED", CleanupReport()
            )
        on_stdout_line(line)


async def _pump_stderr(
    process: asyncio.subprocess.Process,
    limits: OutputLimits,
    chunks: list[bytes],
) -> None:
    reader = process.stderr
    if reader is None:  # pragma: no cover - PIPE is always requested
        return
    total = 0
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return
        total += len(chunk)
        if total > limits.max_stderr_bytes:
            raise ProcessError(
                EvaluationFailure.STDERR_OVERFLOW, "STDERR_CAP_EXCEEDED", CleanupReport()
            )
        chunks.append(chunk)


async def _read_line(reader: asyncio.StreamReader, overflow: EvaluationFailure) -> bytes:
    try:
        return await reader.readline()
    except (ValueError, asyncio.LimitOverrunError):
        # asyncio raises when a single line exceeds the stream limit. That is a
        # separate budget from the per-stream cap and gets its own failure.
        raise ProcessError(
            EvaluationFailure.LINE_OVERFLOW, "LINE_CAP_EXCEEDED", CleanupReport()
        ) from None


async def _await_exit(process: asyncio.subprocess.Process, deadline: Deadline) -> int:
    try:
        return await asyncio.wait_for(process.wait(), timeout=deadline.remaining_for_work)
    except TimeoutError:
        raise ProcessError(
            EvaluationFailure.DEADLINE_EXCEEDED, "EXIT_BUDGET_EXHAUSTED", CleanupReport()
        ) from None


async def _terminate(process: asyncio.subprocess.Process, deadline: Deadline) -> CleanupReport:
    """Signal the process group, reap the child, and say what was achieved."""
    if process.returncode is not None:
        return CleanupReport(signalled=False, reaped=True, exit_code=process.returncode)

    signalled = _signal_group(process, signal.SIGTERM)
    reaped, code = await _wait_for_exit(
        process, min(TERMINATE_GRACE_SECONDS, deadline.remaining_for_cleanup)
    )
    if not reaped:
        _signal_group(process, signal.SIGKILL)
        reaped, code = await _wait_for_exit(process, deadline.remaining_for_cleanup)

    if reaped:
        _release_pipes(process)
    return CleanupReport(
        signalled=signalled,
        reaped=reaped,
        exit_code=code,
        overran_reserve=deadline.cleanup_exhausted and not reaped,
        error_code=None if reaped else "CHILD_NOT_REAPED",
    )


def _release_pipes(process: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport once the child is gone.

    Cancelling the pipe readers leaves their transports open, and asyncio only
    closes them when the Process object is collected. If that happens after the
    event loop has shut down, the cleanup raises inside `__del__` and surfaces as
    an unraisable exception. Closing here keeps teardown quiet and deterministic.
    """
    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    with contextlib.suppress(Exception):
        transport.close()


def _signal_group(process: asyncio.subprocess.Process, number: int) -> bool:
    try:
        os.killpg(os.getpgid(process.pid), number)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


async def _wait_for_exit(
    process: asyncio.subprocess.Process, budget: float
) -> tuple[bool, int | None]:
    if budget <= 0:
        return process.returncode is not None, process.returncode
    waiter: Coroutine[Any, Any, int] = process.wait()
    try:
        # Shielded so an outer cancellation cannot abandon an unreaped child.
        code = await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(waiter)), budget)
    except (TimeoutError, asyncio.CancelledError):
        return process.returncode is not None, process.returncode
    return True, code
