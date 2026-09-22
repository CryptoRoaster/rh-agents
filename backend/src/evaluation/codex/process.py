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

**The spawn itself is inside the budget, but recovery is not.** Creating a
subprocess is an await like any other; left unbounded it could outlast the
deadline it is supposed to obey, so the creation runs shielded under the work
budget. Recovering the handle afterwards obeys no budget at all, and that
asymmetry is deliberate: the deadline governs how much *work* is allowed and
when a result may still be accepted, not how long it may take to make sure
nothing is left running.

Cancelling a spawn is never the answer. It does not undo the creation: the child
may already exist, and asyncio's cancellation path closes the transport, which
kills the *direct* child and waits for it -- `kill`, not `killpg`. A descendant
started in that window survives, and once the spawn task ends as cancelled the
handle is unrecoverable, so no group is ever recorded, signalled or verified for
that tree. With a real Codex process that means network and model activity
continuing after the harness believes it stopped.

So `_claim_spawn` waits until the spawn settles, and until nothing else. There
is no time cap and no cancellation count: both are ways of walking away from a
process that already exists. A caller's cancellation is absorbed and remembered,
and delivered once the child is terminated and its group checked. The practical
cost is small, because a subprocess creation settles as soon as the kernel has
forked and the pipes are connected -- it never waits for the child to do
anything. The practical consequence is that one attempt can exceed its total
wall clock, and that is the intended trade.

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
import sys
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.evaluation.codex import launch_gate
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    CleanupReport,
    EvaluationFailure,
    GroupState,
    OutputLimits,
)

TERMINATE_GRACE_SECONDS = 0.5
GROUP_POLL_SECONDS = 0.02
# Polling granularity while waiting for a spawn to settle. The waiting itself is
# deliberately uncapped; see `_claim_spawn`.
SPAWN_CLAIM_SLICE_SECONDS = 0.25
LAUNCH_GATE_SCRIPT = Path(launch_gate.__file__)
# A fresh allowance for cleaning up a spawn that arrived after its budget was
# already spent. Without it `_terminate` would inherit an exhausted deadline and
# could neither reap the child nor check its group: the budget would be honoured
# and the process tree would survive.
EMERGENCY_CLEANUP_SECONDS = 5.0

# Injected only so a test can hold the handle back while the child and its
# descendants already exist. That window is the one cancellation cannot repair,
# and it is unreachable from outside otherwise.
SpawnProcess = Callable[..., Coroutine[Any, Any, asyncio.subprocess.Process]]


@dataclass
class CleanupBudget:
    """Time available for terminating and verifying, on its own clock.

    Separate from `Deadline` because cleanup sometimes has to outlive it. A
    spawn claimed after its reserve was spent leaves nothing for the work that
    actually removes the process, so that case gets a fresh allowance instead of
    a budget already at zero.

    `exhausted` is a statement about time alone. It says nothing about whether
    cleanup succeeded, and success never clears it.
    """

    total_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.monotonic()

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - (self.monotonic() - self.started_at))

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0


async def _run_cleanup(
    coroutine: Coroutine[Any, Any, CleanupReport],
) -> tuple[CleanupReport, bool]:
    """Run a cleanup to completion, whatever the caller does meanwhile.

    Cleanup runs as its own task and is awaited through a shield, so a
    cancellation delivered to the caller never reaches it. Each `CancelledError`
    is recorded as owed and the wait resumes on the *same* task -- restarting or
    cancelling it would be the shortcut the safety contract exists to forbid.

    Catching `CancelledError` inside the individual steps is not a substitute.
    That only lets one step give up early, which is how a cancelled attempt used
    to end with `CHILD_NOT_REAPED` or an unverified group.

    Returns the report and whether a cancellation is owed to the caller.
    """
    task = asyncio.ensure_future(coroutine)
    owed = False
    while True:
        try:
            return await asyncio.shield(task), owed
        except asyncio.CancelledError:
            owed = True
            _absorb_cancellation()
            if task.done():
                return task.result(), owed


async def _cleanup(
    process: asyncio.subprocess.Process, pgid: int, budget: CleanupBudget
) -> tuple[CleanupReport, bool]:
    return await _run_cleanup(_terminate(process, pgid, budget))


def _absorb_cancellation() -> None:
    """Clear the current task's cancelling state so cleanup can still await.

    A task that has been cancelled but has not un-cancelled itself makes the
    next `wait_for` give up immediately. Cleanup runs entirely on awaits, so
    without this the very code that removes the process would be the code that
    refuses to run. The cancellation is re-raised by the caller afterwards.
    """
    current = asyncio.current_task()
    if current is not None:
        current.uncancel()


def cleanup_budget(deadline: Deadline) -> CleanupBudget:
    """The cleanup allowance the attempt's own deadline still leaves."""
    return CleanupBudget(total_seconds=deadline.remaining_for_cleanup)


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
class SpawnedChild:
    """A child the parent owns, still waiting for permission to become Codex."""

    process: asyncio.subprocess.Process
    gate_write: int


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
    spawned = await _spawn(
        arguments=arguments,
        environment=environment,
        working_directory=working_directory,
        limits=limits,
        deadline=deadline,
        spawn_process=spawn_process,
    )
    process = spawned.process
    pgid = _group_of(process)
    # Ownership first, then execution: the gate has been holding the process
    # group and nothing else, and only now may it become Codex.
    _release_gate(spawned.gate_write)

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
        # The caller gave up. Cleanup finishes first, in full; the cancellation
        # is delivered afterwards rather than swallowed.
        _absorb_cancellation()
        await _cleanup(process, pgid, cleanup_budget(deadline))
        raise
    except ProcessError as error:
        cleanup, owed = await _cleanup(process, pgid, cleanup_budget(deadline))
        if owed:
            raise asyncio.CancelledError from None
        raise ProcessError(error.failure, error.reason_code, cleanup) from None
    except Exception as error:
        cleanup, owed = await _cleanup(process, pgid, cleanup_budget(deadline))
        if owed:
            raise asyncio.CancelledError from None
        raise AbortedByConsumer(error, cleanup) from None
    except BaseException:
        await _cleanup(process, pgid, cleanup_budget(deadline))
        raise

    cleanup, owed = await _cleanup(process, pgid, cleanup_budget(deadline))
    if owed:
        # A cancellation arrived while the group was being cleared. It was held
        # until the tree was gone, and it is delivered now.
        raise asyncio.CancelledError
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
) -> SpawnedChild:
    """Create the child inside the work budget, without losing it on a race.

    The child is the launch gate, not Codex. It takes the process group and then
    waits, so the window in which a spawn can fail while a Codex process and its
    descendants are already running does not exist: nothing is executed until
    the caller holds the handle and releases the gate.
    """
    if deadline.work_exhausted:
        raise ProcessError(EvaluationFailure.DEADLINE_EXCEEDED, "NO_TIME_TO_START", CleanupReport())
    if not arguments or not os.access(arguments[0], os.X_OK):
        # Checked before anything is forked, so this failure really does mean
        # that nothing was started.
        raise ProcessError(
            EvaluationFailure.PROCESS_START_FAILED,
            "EXECUTABLE_NOT_RUNNABLE",
            CleanupReport(group=GroupState.EMPTY),
        )

    gate_read, gate_write = os.pipe()
    os.set_inheritable(gate_read, True)
    spawn = asyncio.ensure_future(
        spawn_process(
            sys.executable,
            str(LAUNCH_GATE_SCRIPT),
            str(gate_read),
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(working_directory),
            start_new_session=True,
            limit=limits.max_line_bytes,
            pass_fds=(gate_read,),
        )
    )
    try:
        # Shielded so a timeout or an outer cancellation cannot detach a child
        # the kernel has already created.
        process = await asyncio.wait_for(asyncio.shield(spawn), timeout=deadline.remaining_for_work)
    # TimeoutError is a subclass of OSError, so it has to be caught first or a
    # missed deadline would be misreported as a failure to start -- and the
    # spawn would be left running with nobody to claim it.
    except TimeoutError:
        _close_fd(gate_write)
        cleanup, cancelled = await _abandon_spawn(spawn, deadline)
        _close_fd(gate_read)
        if cancelled:
            # The caller asked to stop while recovery was running. Recovery was
            # finished first; the cancellation is delivered now, not dropped.
            raise asyncio.CancelledError from None
        raise ProcessError(
            EvaluationFailure.DEADLINE_EXCEEDED, "START_BUDGET_EXHAUSTED", cleanup
        ) from None
    except asyncio.CancelledError:
        _absorb_cancellation()
        _close_fd(gate_write)
        await _abandon_spawn(spawn, deadline)
        _close_fd(gate_read)
        raise
    except (OSError, ValueError) as error:
        # The creation failed after the fork may already have happened. Closing
        # the gate is what makes that safe: the child, if there is one, reads
        # EOF and exits without ever executing Codex. What it does not give us
        # is a handle, so the group is unverified rather than empty.
        _close_fd(gate_write)
        _close_fd(gate_read)
        raise ProcessError(
            EvaluationFailure.PROCESS_START_FAILED,
            type(error).__name__.upper(),
            CleanupReport(group=GroupState.UNVERIFIED, error_code="SPAWN_FAILED_AFTER_FORK"),
        ) from None

    _close_fd(gate_read)
    return SpawnedChild(process=process, gate_write=gate_write)


def _release_gate(gate_write: int) -> None:
    """Let the gate become Codex. Called only once the handle is owned."""
    try:
        os.write(gate_write, launch_gate.RELEASE_BYTE)
    except OSError:
        pass
    finally:
        _close_fd(gate_write)


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


async def _abandon_spawn(
    spawn: "asyncio.Future[asyncio.subprocess.Process]", deadline: Deadline
) -> tuple[CleanupReport, bool]:
    """Recover a child from an abandoned spawn and terminate it.

    Ownership is never given up here. Cancelling a subprocess creation does not
    undo it: the child may already be running, and asyncio's cancellation path
    closes the transport, which kills the direct child and waits for it -- with
    `kill`, not `killpg`. A descendant started in that window survives, and once
    the spawn task ends as cancelled the handle is unrecoverable, so no group is
    ever recorded, signalled or checked for that tree. Returning early with a
    live Codex process would also mean network and model activity continuing
    after the harness believes it has stopped.

    So the handle is waited for until the spawn settles, and a caller's
    cancellation cannot shorten that. If the wait runs past the cleanup reserve,
    the overrun is reported and termination gets a fresh emergency allowance --
    an exhausted budget would otherwise "honour the deadline" by leaving the
    process alive.

    Returns the cleanup report and whether a cancellation was absorbed while
    recovering, so the caller can deliver it once the tree is actually gone.
    """
    process, overran, cancelled = await _claim_spawn(spawn, deadline)
    budget = (
        CleanupBudget(total_seconds=EMERGENCY_CLEANUP_SECONDS)
        if overran
        else cleanup_budget(deadline)
    )
    if process is None:
        # The spawn settled without handing over a process: it raised, or
        # someone outside this module cancelled it. That does NOT prove nothing
        # was started -- the fork can precede the failure -- so the group is
        # reported as unverified rather than empty. What makes it safe is the
        # launch gate: without a handle the gate is never released, so no Codex
        # process exists to keep running.
        return (
            CleanupReport(
                group=GroupState.UNVERIFIED,
                overran_reserve=overran,
                error_code="SPAWN_NOT_RECOVERED",
            ),
            cancelled,
        )
    report, owed = await _cleanup(process, _group_of(process), budget)
    if overran and not report.overran_reserve:
        report = report.model_copy(update={"overran_reserve": True})
    # A cancellation that only arrives while the group is being cleared counts
    # just as much as one that arrived while the handle was still pending.
    return report, cancelled or owed


async def _claim_spawn(
    spawn: "asyncio.Future[asyncio.subprocess.Process]", deadline: Deadline
) -> tuple[asyncio.subprocess.Process | None, bool, bool]:
    """Wait for a spawn to settle and take ownership of whatever it produced.

    The loop ends when the spawn settles and at no other point. There is no
    time cap and no cancellation count, because both would be ways of walking
    away from a process that already exists. A subprocess creation settles as
    soon as the kernel has forked and the pipes are connected -- it does not
    wait for the child to do anything -- so waiting costs a moment, while giving
    up costs an unowned process tree.

    Cancellation is recorded, not obeyed. Each `CancelledError` is absorbed and
    the task is un-cancelled so the recovery can keep running; the caller gets
    its cancellation once the child is terminated and its group checked.

    Returns the process if one exists, whether the cleanup reserve was already
    gone by then, and whether a cancellation is owed to the caller.
    """
    cancelled = False
    current = asyncio.current_task()
    while not spawn.done():
        try:
            await asyncio.wait([spawn], timeout=SPAWN_CLAIM_SLICE_SECONDS)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                # Clear the cancelling state, or the next await would refuse to
                # run and recovery would stall exactly where it must not.
                current.uncancel()
    # Asked once the waiting is over: was the reserve already gone by the time
    # this process owned the handle?
    overran = deadline.cleanup_exhausted
    if spawn.cancelled() or spawn.exception() is not None:
        return None, overran, cancelled
    return spawn.result(), overran, cancelled


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
    process: asyncio.subprocess.Process, pgid: int, budget: CleanupBudget
) -> CleanupReport:
    """Reap the child, clear its group, and report what was established.

    Whether cleanup succeeded and whether it was on time are answered
    separately. `group_cleared` reports the first, `overran_reserve` the second,
    and a successful outcome never erases an overrun.
    """
    signalled = False
    if process.returncode is None:
        signalled = _signal_group(pgid, signal.SIGTERM)
        reaped, code = await _wait_for_exit(process, min(TERMINATE_GRACE_SECONDS, budget.remaining))
        if not reaped:
            _signal_group(pgid, signal.SIGKILL)
            reaped, code = await _wait_for_exit(process, budget.remaining)
    else:
        # The leader finished on its own. That says nothing about the rest of
        # its group, so cleanup continues rather than returning here.
        reaped, code = True, process.returncode

    group, group_signalled = await _clear_group(pgid, budget)
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
        # Read once, at the end, and about time only. A group that was cleared
        # late is still a group that was cleared late.
        overran_reserve=budget.exhausted,
        error_code=error,
    )


async def _clear_group(pgid: int, budget: CleanupBudget) -> tuple[GroupState, bool]:
    """Signal whatever is left in the group and confirm the group is empty."""
    if not _group_alive(pgid):
        return GroupState.EMPTY, False

    signalled = _signal_group(pgid, signal.SIGTERM)
    state = await _poll_group(pgid, min(TERMINATE_GRACE_SECONDS, budget.remaining))
    if state is GroupState.EMPTY:
        return state, signalled

    # Something in the group ignored SIGTERM, or was too slow to act on it.
    killed = _signal_group(pgid, signal.SIGKILL)
    state = await _poll_group(pgid, budget.remaining)
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
