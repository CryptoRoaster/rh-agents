"""Start one child, read it under caps, and always account for its whole group.

Four rules shape this module.

**Both pipes are drained concurrently and both are capped.** `communicate()` is
not used: it buffers without limit, and a child that writes faster than it exits
would grow the parent's memory until something else broke first. A single
unterminated line is capped separately from the stream total, because one
endless line would otherwise defeat a per-stream cap.

**An overflow ends the attempt.** Whatever was read up to that point is
discarded rather than parsed, so a truncated stream can never be mistaken for a
short one.

**The spawn itself is inside the budget.** Creating a subprocess is an await
like any other; left unbounded it could outlast the deadline it is supposed to
obey. It is also a cancellation race: a spawn cancelled halfway can still
succeed in the kernel, and dropping the handle at that moment would leak a
process nobody owns. The spawn therefore runs shielded under the work budget,
and a timeout or an outer cancellation claims the handle before cleaning up.

Claiming is preferred over cancelling for a concrete reason. Cancelling a
subprocess creation does not undo it. The child may already be running, and
asyncio's cancellation path closes the transport, which kills the *direct* child
and waits for it -- `kill`, not `killpg`. A descendant started in that window
survives, and once the spawn task ends as cancelled the handle is unrecoverable,
so no group is ever recorded, signalled or verified for that tree. The same
cancellation also inherits asyncio's wait for the child to exit, which a
long-lived child turns into a long wait, during cleanup and again at loop close.

So a spawn is claimed even past the cleanup reserve, up to a ceiling, and the
overrun is reported. Only beyond the ceiling is the handle given up, and the
report then says `UNVERIFIED`, because at that point a process may remain and
claiming otherwise would be a guess.

**Cleanup targets the group, not just the child.** The process group id is
recorded at spawn time and kept, because `os.getpgid` stops answering once the
leader is gone. A leader exiting on its own is not the end of the story: it can
leave descendants behind in the same group, so the group is cleared and then
checked whatever the leader did. `GroupState` records what that check found, and
`UNVERIFIED` is used when the cleanup budget ran out before the group could be
confirmed empty -- that is not a synonym for success.

Two limits stay explicit. A descendant that called `setsid` has left the group
and is invisible here, and a group id can in principle be reused once the group
is empty, which is why the group is only signalled while it is known to exist.
An outer `CancelledError` is re-raised after cleanup finishes, never swallowed.
"""

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    CleanupReport,
    EvaluationFailure,
    GroupState,
    OutputLimits,
)

TERMINATE_GRACE_SECONDS = 0.5
GROUP_POLL_SECONDS = 0.02
# How far past the cleanup reserve a spawn may still be claimed. Ownership is
# worth more than punctuality here: a claimed handle can be terminated and
# verified, an abandoned one cannot be touched again.
SPAWN_CLAIM_CEILING_SECONDS = 5.0

# Injected only so a test can hold the handle back while the child and its
# descendants already exist. That window is the one cancellation cannot repair,
# and it is unreachable from outside otherwise.
SpawnProcess = Callable[..., Coroutine[Any, Any, asyncio.subprocess.Process]]


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
    spawn_process: SpawnProcess = asyncio.create_subprocess_exec,
) -> ProcessResult:
    """Run one child to completion under one deadline and fixed output caps."""
    process = await _spawn(
        arguments=arguments,
        environment=environment,
        working_directory=working_directory,
        limits=limits,
        deadline=deadline,
        spawn_process=spawn_process,
    )
    pgid = _group_of(process)

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
        await _terminate(process, pgid, deadline)
        raise
    except ProcessError as error:
        cleanup = await _terminate(process, pgid, deadline)
        raise ProcessError(error.failure, error.reason_code, cleanup) from None
    except Exception as error:
        cleanup = await _terminate(process, pgid, deadline)
        raise AbortedByConsumer(error, cleanup) from None
    except BaseException:
        await _terminate(process, pgid, deadline)
        raise

    cleanup = await _terminate(process, pgid, deadline)
    if not cleanup.complete:
        raise ProcessError(
            EvaluationFailure.CLEANUP_INCOMPLETE,
            cleanup.error_code or "CLEANUP_OVERRAN",
            cleanup,
        )
    return ProcessResult(exit_code=exit_code, stderr_tail=stderr_tail, cleanup=cleanup)


async def _spawn(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    limits: OutputLimits,
    deadline: Deadline,
    spawn_process: SpawnProcess = asyncio.create_subprocess_exec,
) -> asyncio.subprocess.Process:
    """Create the child inside the work budget, without losing it on a race."""
    if deadline.work_exhausted:
        raise ProcessError(EvaluationFailure.DEADLINE_EXCEEDED, "NO_TIME_TO_START", CleanupReport())
    spawn = asyncio.ensure_future(
        spawn_process(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(working_directory),
            start_new_session=True,
            limit=limits.max_line_bytes,
        )
    )
    try:
        # Shielded so a timeout or an outer cancellation cannot detach a child
        # the kernel has already created.
        return await asyncio.wait_for(asyncio.shield(spawn), timeout=deadline.remaining_for_work)
    # TimeoutError is a subclass of OSError, so it has to be caught first or a
    # missed deadline would be misreported as a failure to start -- and the
    # spawn would be left running with nobody to claim it.
    except TimeoutError:
        cleanup = await _abandon_spawn(spawn, deadline)
        raise ProcessError(
            EvaluationFailure.DEADLINE_EXCEEDED, "START_BUDGET_EXHAUSTED", cleanup
        ) from None
    except asyncio.CancelledError:
        await _abandon_spawn(spawn, deadline)
        raise
    except (OSError, ValueError) as error:
        raise ProcessError(
            EvaluationFailure.PROCESS_START_FAILED,
            type(error).__name__.upper(),
            CleanupReport(group=GroupState.EMPTY),
        ) from None


async def _abandon_spawn(
    spawn: "asyncio.Future[asyncio.subprocess.Process]", deadline: Deadline
) -> CleanupReport:
    """Recover a child from an abandoned spawn and terminate it.

    Claiming the handle comes first, and it outranks the cleanup reserve.
    Cancelling a subprocess creation does not undo it: the child may already be
    running, and asyncio's own cancellation path closes the transport, which
    kills the direct child and waits for it -- with `kill`, not `killpg`. A
    descendant started in the meantime survives that, and once the spawn task
    ends as cancelled nothing can recover the handle, so no process group is
    ever recorded, signalled or verified for that tree.

    Waiting is therefore the safer trade. If the wait runs past the reserve the
    overrun is reported, because an honest overrun beats a leaked process tree.
    """
    process, overran = await _claim_spawn(spawn, deadline)
    if process is None:
        return CleanupReport(
            group=GroupState.UNVERIFIED,
            overran_reserve=overran,
            error_code="SPAWN_ABANDONED",
        )
    report = await _terminate(process, _group_of(process), deadline)
    if overran and not report.overran_reserve:
        return report.model_copy(update={"overran_reserve": True})
    return report


async def _claim_spawn(
    spawn: "asyncio.Future[asyncio.subprocess.Process]", deadline: Deadline
) -> tuple[asyncio.subprocess.Process | None, bool]:
    """Take ownership of a spawn the caller stopped waiting for.

    Returns the process and whether claiming it cost more than the reserve.
    Only after the ceiling is the spawn given up, and the caller then reports
    `UNVERIFIED`: at that point a process may well remain, and saying anything
    else would be a guess.
    """
    reserve = max(0.0, deadline.remaining_for_cleanup)
    if reserve > 0 and not spawn.done():
        # `asyncio.wait` neither raises the task's exception nor cancels it.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait([spawn], timeout=reserve)

    overran = False
    if not spawn.done():
        overran = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait([spawn], timeout=SPAWN_CLAIM_CEILING_SECONDS)

    if not spawn.done():
        spawn.cancel()
        return None, overran
    if spawn.cancelled() or spawn.exception() is not None:
        return None, overran
    return spawn.result(), overran


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
        asyncio.create_task(_pump_stdout(process, limits, on_stdout_line)),
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
        line = await _read_line(reader)
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


async def _read_line(reader: asyncio.StreamReader) -> bytes:
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


def _group_of(process: asyncio.subprocess.Process) -> int:
    """Record the child's process group while the leader is still answering.

    `start_new_session=True` makes the child a session and group leader, so its
    group id equals its pid. Asking the kernel is still preferred; the pid is
    the fallback for the case where the child has already exited, because a
    group outlives its leader as long as any member is left.
    """
    try:
        return os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return process.pid


async def _terminate(
    process: asyncio.subprocess.Process, pgid: int, deadline: Deadline
) -> CleanupReport:
    """Reap the child, clear its group, and report what was established."""
    signalled = False
    if process.returncode is None:
        signalled = _signal_group(pgid, signal.SIGTERM)
        reaped, code = await _wait_for_exit(
            process, min(TERMINATE_GRACE_SECONDS, deadline.remaining_for_cleanup)
        )
        if not reaped:
            _signal_group(pgid, signal.SIGKILL)
            reaped, code = await _wait_for_exit(process, deadline.remaining_for_cleanup)
    else:
        # The leader finished on its own. That says nothing about the rest of
        # its group, so cleanup continues rather than returning here.
        reaped, code = True, process.returncode

    group, group_signalled = await _clear_group(pgid, deadline)
    if reaped:
        _release_pipes(process)

    error: str | None = None
    if not reaped:
        error = "CHILD_NOT_REAPED"
    elif group is GroupState.OCCUPIED:
        error = "GROUP_NOT_EMPTY"
    elif group is GroupState.UNVERIFIED:
        error = "GROUP_NOT_VERIFIED"

    return CleanupReport(
        signalled=signalled or group_signalled,
        reaped=reaped,
        exit_code=code,
        group=group,
        overran_reserve=deadline.cleanup_exhausted
        and (not reaped or group is not GroupState.EMPTY),
        error_code=error,
    )


async def _clear_group(pgid: int, deadline: Deadline) -> tuple[GroupState, bool]:
    """Signal whatever is left in the group and confirm the group is empty."""
    if not _group_alive(pgid):
        return GroupState.EMPTY, False

    signalled = _signal_group(pgid, signal.SIGTERM)
    state = await _poll_group(pgid, min(TERMINATE_GRACE_SECONDS, deadline.remaining_for_cleanup))
    if state is GroupState.EMPTY:
        return state, signalled

    # Something in the group ignored SIGTERM, or was too slow to act on it.
    killed = _signal_group(pgid, signal.SIGKILL)
    state = await _poll_group(pgid, deadline.remaining_for_cleanup)
    return state, signalled or killed


async def _poll_group(pgid: int, budget: float) -> GroupState:
    """Wait for the group to empty, and never guess when time runs out."""
    if budget <= 0:
        return GroupState.EMPTY if not _group_alive(pgid) else GroupState.UNVERIFIED
    until = time.monotonic() + budget
    while time.monotonic() < until:
        if not _group_alive(pgid):
            return GroupState.EMPTY
        try:
            await asyncio.sleep(GROUP_POLL_SECONDS)
        except asyncio.CancelledError:
            # Cleanup was cut short. Report what is currently true rather than
            # assuming the signals took effect.
            return GroupState.EMPTY if not _group_alive(pgid) else GroupState.UNVERIFIED
    return GroupState.EMPTY if not _group_alive(pgid) else GroupState.OCCUPIED


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # Something is there that this process may not signal. Treat it as
        # present rather than reporting an empty group we cannot see into.
        return True
    return True


def _signal_group(pgid: int, number: int) -> bool:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


async def _wait_for_exit(
    process: asyncio.subprocess.Process, budget: float
) -> tuple[bool, int | None]:
    if budget <= 0:
        return process.returncode is not None, process.returncode
    try:
        # Shielded so an outer cancellation cannot abandon an unreaped child.
        code = await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(process.wait())), budget)
    except (TimeoutError, asyncio.CancelledError):
        return process.returncode is not None, process.returncode
    return True, code


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
