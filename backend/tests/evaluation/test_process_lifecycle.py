"""Deadline, cancellation, output caps and what cleanup actually achieves.

The process-group assertions here are the strongest statement this suite can
make, and it is still a narrow one: the group the harness created is gone. A
descendant that deliberately left that group would be invisible to both the
harness and this test, which is why `CleanupReport` documents itself as an
account of what was attempted rather than an operating-system guarantee.
"""

import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from src.evaluation.codex import process as process_module
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    CleanupReport,
    EvaluationFailure,
    GroupState,
    OutputLimits,
)
from src.evaluation.codex.process import (
    AbortedByConsumer,
    CleanupBudget,
    ProcessError,
    SpawnProcess,
    _poll_group,
    run_bounded,
)
from tests.evaluation.conftest import CHILD_PATH_ENTRIES, write_launcher


def environment(root: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": str(root / "codex"),
        "HOME": str(root / "home"),
        "PATH": ":".join(str(entry) for entry in CHILD_PATH_ENTRIES),
        "TMPDIR": str(root / "tmp"),
        "LANG": "en_US.UTF-8",
    }


def workspace(root: Path, scenario: str, **extra: object) -> Path:
    directory = root / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    payload: dict[str, object] = {"name": scenario}
    payload.update(extra)
    (directory / "scenario.json").write_text(json.dumps(payload), encoding="utf-8")
    write_launcher(root)
    return directory


async def run(
    root: Path,
    scenario: str,
    *,
    limits: OutputLimits | None = None,
    total: float = 20.0,
    reserve: float = 2.0,
    on_line: Callable[[bytes], None] | None = None,
    **extra: object,
) -> object:
    directory = workspace(root, scenario, **extra)
    return await run_bounded(
        arguments=[str(root / "codex")],
        environment=environment(root),
        working_directory=directory,
        stdin_payload=b'{"candidate":{}}',
        limits=limits or OutputLimits(),
        deadline=Deadline(total_seconds=total, cleanup_reserve_seconds=reserve),
        on_stdout_line=on_line or (lambda _line: None),
    )


async def group_is_gone(pgid_file: Path) -> bool:
    for _ in range(250):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling, not runtime code
            break
        await asyncio.sleep(0.02)
    assert pgid_file.exists(), "the scenario never recorded its process group"  # noqa: ASYNC240
    pgid = int(pgid_file.read_text(encoding="utf-8"))  # noqa: ASYNC240
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    os.killpg(pgid, signal.SIGKILL)
    return False


async def test_a_clean_run_reports_a_complete_cleanup(tmp_path: Path) -> None:
    lines: list[bytes] = []
    result = await run(tmp_path, "success", on_line=lines.append)
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert result.cleanup.reaped is True  # type: ignore[attr-defined]
    assert result.cleanup.complete is True  # type: ignore[attr-defined]
    assert any(b"turn.completed" in line for line in lines)


async def test_a_timeout_ends_the_process_group(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "hang", total=4.0, reserve=0.6, pgid_out=str(pgid_file))
    assert caught.value.failure is EvaluationFailure.DEADLINE_EXCEEDED
    assert caught.value.cleanup.signalled is True
    assert caught.value.cleanup.reaped is True
    assert await group_is_gone(pgid_file)


async def test_a_grandchild_in_the_group_is_ended_too(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    with pytest.raises(ProcessError):
        await run(tmp_path, "hang_with_grandchild", total=4.0, reserve=0.6, pgid_out=str(pgid_file))
    assert await group_is_gone(pgid_file)


async def test_outer_cancellation_cleans_up_and_is_re_raised(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    task = asyncio.create_task(run(tmp_path, "hang", total=30.0, pgid_out=str(pgid_file)))
    for _ in range(100):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling, not runtime code
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await group_is_gone(pgid_file)


async def test_stdout_overflow_aborts_without_a_partial_result(tmp_path: Path) -> None:
    lines: list[bytes] = []
    with pytest.raises(ProcessError) as caught:
        await run(
            tmp_path,
            "stdout_flood",
            limits=OutputLimits(max_stdout_bytes=8192),
            on_line=lines.append,
        )
    assert caught.value.failure is EvaluationFailure.STDOUT_OVERFLOW
    assert caught.value.cleanup.reaped is True


async def test_stderr_overflow_is_its_own_failure(tmp_path: Path) -> None:
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "stderr_flood", limits=OutputLimits(max_stderr_bytes=4096))
    assert caught.value.failure is EvaluationFailure.STDERR_OVERFLOW


async def test_one_endless_line_is_capped_separately(tmp_path: Path) -> None:
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "long_line", limits=OutputLimits(max_line_bytes=4096))
    assert caught.value.failure is EvaluationFailure.LINE_OVERFLOW


async def test_a_failure_to_start_is_reported_as_such(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "success")
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "does-not-exist")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=5.0, cleanup_reserve_seconds=1.0),
            on_stdout_line=lambda _line: None,
        )
    assert caught.value.failure is EvaluationFailure.PROCESS_START_FAILED


async def test_a_consumer_rejection_stops_the_child_and_keeps_the_cause(
    tmp_path: Path,
) -> None:
    class Rejected(Exception):
        pass

    def reject(_line: bytes) -> None:
        raise Rejected("consumer said no")

    with pytest.raises(AbortedByConsumer) as caught:
        await run(tmp_path, "success", on_line=reject)
    assert isinstance(caught.value.cause, Rejected)
    assert caught.value.cleanup.reaped is True


async def test_no_time_left_means_no_process_is_started(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "success")
    deadline = Deadline(total_seconds=0.2, cleanup_reserve_seconds=0.1)
    await asyncio.sleep(0.25)
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=deadline,
            on_stdout_line=lambda _line: None,
        )
    assert caught.value.reason_code == "NO_TIME_TO_START"


async def test_a_finished_leader_does_not_end_cleanup_of_its_group(
    tmp_path: Path,
) -> None:
    """The regression: reaping the child is not the same as clearing the group."""
    pgid_file = tmp_path / "pgid"
    result = await run(tmp_path, "parent_exits_grandchild_runs", pgid_out=str(pgid_file))
    assert result.exit_code == 0  # type: ignore[attr-defined]
    cleanup = result.cleanup  # type: ignore[attr-defined]
    assert cleanup.reaped is True
    # The leader exited on its own, yet the group still had to be signalled.
    assert cleanup.signalled is True
    assert cleanup.group is GroupState.EMPTY
    assert cleanup.complete is True
    assert await group_is_gone(pgid_file)


async def test_a_descendant_that_ignores_sigterm_is_still_removed(
    tmp_path: Path,
) -> None:
    pgid_file = tmp_path / "pgid"
    result = await run(tmp_path, "sigterm_immune_descendant", pgid_out=str(pgid_file))
    cleanup = result.cleanup  # type: ignore[attr-defined]
    assert cleanup.group is GroupState.EMPTY
    assert cleanup.complete is True
    assert await group_is_gone(pgid_file)


async def test_an_occupied_group_is_never_reported_as_complete() -> None:
    occupied = CleanupReport(reaped=True, group=GroupState.OCCUPIED)
    assert occupied.complete is False
    assert occupied.group_cleared is False
    unverified = CleanupReport(reaped=True, group=GroupState.UNVERIFIED)
    assert unverified.complete is False
    abandoned = CleanupReport(group=GroupState.UNVERIFIED, error_code="SPAWN_NEVER_SETTLED")
    assert abandoned.group_cleared is False
    assert abandoned.complete is False
    empty = CleanupReport(reaped=True, group=GroupState.EMPTY)
    assert empty.complete is True
    assert empty.group_cleared is True


async def test_a_group_that_cannot_be_checked_is_unverified_not_empty() -> None:
    """With no budget left, an occupied group is reported as unverified.

    Signals are cheap and still get delivered, but nothing confirmed they took
    effect, and `UNVERIFIED` is what says so.
    """
    process = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 30", start_new_session=True
    )
    pgid = os.getpgid(process.pid)
    try:
        assert await _poll_group(pgid, 0.0) is GroupState.UNVERIFIED
        assert await _poll_group(pgid, 0.1) is GroupState.OCCUPIED
    finally:
        os.killpg(pgid, signal.SIGKILL)
        await process.wait()
    assert await _poll_group(pgid, 0.5) is GroupState.EMPTY


async def test_a_spawn_that_outlives_its_budget_leaves_no_process_behind(
    tmp_path: Path,
) -> None:
    """The start is inside the budget, and losing the race must not leak a child."""
    directory = workspace(tmp_path, "hang", pgid_out=str(tmp_path / "pgid"))

    class Clock:
        """Drive the spawn deterministically instead of racing a real timer.

        Reading 1 is construction. Reading 2 answers "is the work budget gone?"
        with a millisecond left, so the spawn is attempted at all. Reading 3
        supplies the timeout itself as zero, so `wait_for` gives up before any
        subprocess can appear -- on every machine, rather than on whichever one
        loses a millisecond race. Later readings keep a second for cleanup.
        """

        def __init__(self) -> None:
            self.readings = [0.0, 8.999]

        def __call__(self) -> float:
            return self.readings.pop(0) if self.readings else 9.0

    deadline = Deadline(total_seconds=10.0, cleanup_reserve_seconds=1.0, monotonic=Clock())
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=deadline,
            on_stdout_line=lambda _line: None,
        )
    assert caught.value.failure is EvaluationFailure.DEADLINE_EXCEEDED
    assert caught.value.reason_code == "START_BUDGET_EXHAUSTED"
    # Whatever the kernel had already created was claimed and removed; an
    # abandoned spawn would leave the group behind and show up here.
    assert caught.value.cleanup.group is GroupState.EMPTY
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


async def test_cancellation_during_the_spawn_is_re_raised(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "hang", pgid_out=str(tmp_path / "pgid"))
    task = asyncio.create_task(
        run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=20.0, cleanup_reserve_seconds=2.0),
            on_stdout_line=lambda _line: None,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def spawn_held_back(
    pgid_file: Path, hold_seconds: float, *, release: asyncio.Event | None = None
) -> SpawnProcess:
    """A spawn that starts the real command, then withholds the handle.

    It deliberately steps around the launch gate -- dropping the interpreter,
    the gate script and the gate descriptor from the argument list -- and runs
    the command itself. That recreates the exact state the gate exists to
    prevent: a real leader, a real descendant, and a caller holding nothing.

    Production code can no longer reach that state, which is the point of the
    gate. Ownership recovery still has to survive it, so the tests keep a way to
    produce it on purpose.
    """

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        direct = list(args)[5:]
        options = {key: value for key, value in kwargs.items() if key != "pass_fds"}
        process = await asyncio.create_subprocess_exec(*direct, **options)  # type: ignore[arg-type]
        for _ in range(250):
            if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
                break
            await asyncio.sleep(0.02)
        if release is not None:
            await release.wait()
        else:
            await asyncio.sleep(hold_seconds)
        return process

    return spawn


async def test_a_spawn_claimed_after_a_spent_reserve_still_clears_its_group(
    tmp_path: Path,
) -> None:
    """The regression, on a clock that actually advances.

    A frozen clock would hand `_terminate` its cleanup budget back and prove
    nothing about the case that matters: the handle arriving when the reserve is
    genuinely gone. Here the budget really is spent, so clearing the group
    depends on the emergency allowance rather than on arithmetic.
    """
    pgid_file = tmp_path / "pgid"
    directory = workspace(tmp_path, "hang_with_grandchild", pgid_out=str(pgid_file))

    started = time.monotonic()
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            # Real time, no injected clock: work 1.5 s, reserve 0.5 s.
            deadline=Deadline(total_seconds=2.0, cleanup_reserve_seconds=0.5),
            on_stdout_line=lambda _line: None,
            # Settles well after the whole 2 s budget is gone.
            spawn_process=spawn_held_back(pgid_file, hold_seconds=2.5),
        )
    elapsed = time.monotonic() - started

    cleanup = caught.value.cleanup
    assert caught.value.failure is EvaluationFailure.DEADLINE_EXCEEDED
    # The reserve was really spent, not merely declared spent.
    assert elapsed > 2.0
    assert cleanup.reaped is True
    assert cleanup.signalled is True
    assert cleanup.group is GroupState.EMPTY
    assert cleanup.group_cleared is True
    # Holding on past the reserve is the trade that made that possible, and it
    # is reported rather than hidden.
    assert cleanup.overran_reserve is True
    assert cleanup.complete is False
    assert await group_is_gone(pgid_file)
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


async def test_cleanup_that_succeeds_late_is_cleared_but_not_complete(
    tmp_path: Path,
) -> None:
    """Success and punctuality are separate facts, and success never hides one.

    The child has already exited and its group is empty, so cleanup does
    everything it can do. The budget is gone all the same, and the report says
    both things instead of letting the good outcome erase the late one.
    """
    directory = workspace(tmp_path, "success")
    process = await asyncio.create_subprocess_exec(
        str(tmp_path / "codex"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment(tmp_path),
        cwd=str(directory),
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    await process.wait()

    report = await process_module._terminate(process, pgid, CleanupBudget(total_seconds=0.0))
    assert report.reaped is True
    assert report.group is GroupState.EMPTY
    assert report.group_cleared is True
    assert report.overran_reserve is True
    assert report.complete is False


async def test_repeated_cancellation_cannot_take_a_spawn_out_of_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: cancellation may ask to stop, never to let go.

    A leader and a descendant are already running while the caller still has no
    handle. The caller then cancels far more often than the old absorption
    counter allowed. Recovery has to finish anyway: the counter used to break
    out of the wait here and leave the tree unowned, with no group ever recorded
    or checked.
    """
    pgid_file = tmp_path / "pgid"
    directory = workspace(tmp_path, "hang_with_grandchild", pgid_out=str(pgid_file))
    release = asyncio.Event()
    holder: dict[str, asyncio.subprocess.Process] = {}
    reports: list[CleanupReport] = []

    real_terminate = process_module._terminate

    async def recording_terminate(
        process: asyncio.subprocess.Process, pgid: int, budget: CleanupBudget
    ) -> CleanupReport:
        report = await real_terminate(process, pgid, budget)
        reports.append(report)
        return report

    monkeypatch.setattr(process_module, "_terminate", recording_terminate)

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        # Steps around the launch gate on purpose; see `spawn_held_back`.
        direct = list(args)[5:]
        options = {key: value for key, value in kwargs.items() if key != "pass_fds"}
        process = await asyncio.create_subprocess_exec(*direct, **options)  # type: ignore[arg-type]
        holder["process"] = process
        for _ in range(250):
            if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
                break
            await asyncio.sleep(0.02)
        await release.wait()
        return process

    task = asyncio.create_task(
        run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=30.0, cleanup_reserve_seconds=2.0),
            on_stdout_line=lambda _line: None,
            spawn_process=spawn,
        )
    )

    for _ in range(250):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
            break
        await asyncio.sleep(0.02)
    assert pgid_file.exists()  # noqa: ASYNC240 - leader and descendant are up

    # Twelve cancellations: well past the eight the old counter tolerated.
    for _ in range(12):
        task.cancel()
        await asyncio.sleep(0.03)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert holder["process"].returncode is not None, "leader was not reaped"
    assert reports, "recovery never reached cleanup"
    assert reports[-1].group is GroupState.EMPTY
    assert reports[-1].group_cleared is True
    assert await group_is_gone(pgid_file)
    assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]


def terminate_watcher(
    started: asyncio.Event, reports: list[CleanupReport]
) -> Callable[..., object]:
    """Wrap `_terminate` so a test can cancel exactly while it is running."""
    real = process_module._terminate

    async def watched(
        process: asyncio.subprocess.Process, pgid: int, budget: CleanupBudget
    ) -> CleanupReport:
        started.set()
        report = await real(process, pgid, budget)
        reports.append(report)
        return report

    return watched


async def test_cancellation_during_cleanup_cannot_cut_it_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation in both phases: before ownership, and during termination.

    Guarding only `_claim_spawn` left the second half open. A cancellation that
    arrived once the handle was owned used to reach `_wait_for_exit` and
    `_poll_group`, which returned early and produced CHILD_NOT_REAPED or an
    unverified group -- cleanup cut short by the very signal that is supposed to
    wait for it.
    """
    pgid_file = tmp_path / "pgid"
    # The descendant ignores SIGTERM, so clearing the group has to wait out the
    # grace period. A cleanup that gave up on cancellation would stop there.
    directory = workspace(tmp_path, "hang_with_sigterm_immune_descendant", pgid_out=str(pgid_file))
    release = asyncio.Event()
    terminating = asyncio.Event()
    reports: list[CleanupReport] = []
    holder: dict[str, asyncio.subprocess.Process] = {}

    monkeypatch.setattr(process_module, "_terminate", terminate_watcher(terminating, reports))

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        direct = list(args)[5:]
        options = {key: value for key, value in kwargs.items() if key != "pass_fds"}
        process = await asyncio.create_subprocess_exec(*direct, **options)  # type: ignore[arg-type]
        holder["process"] = process
        for _ in range(250):
            if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
                break
            await asyncio.sleep(0.02)
        await release.wait()
        return process

    task = asyncio.create_task(
        run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=30.0, cleanup_reserve_seconds=2.0),
            on_stdout_line=lambda _line: None,
            spawn_process=spawn,
        )
    )

    for _ in range(250):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
            break
        await asyncio.sleep(0.02)
    assert pgid_file.exists()  # noqa: ASYNC240

    # Phase one: cancel while the handle is still withheld.
    for _ in range(5):
        task.cancel()
        await asyncio.sleep(0.02)

    release.set()
    await terminating.wait()

    # Phase two: keep cancelling throughout termination, across the SIGTERM
    # grace and the SIGKILL check. This pins the end-to-end contract -- the
    # caller is answered only after the tree is gone. The narrower proof that a
    # cancellation cannot be dropped mid-cleanup is the emergency-cleanup test
    # below, which fails outright without the shield.
    for _ in range(30):
        task.cancel()
        await asyncio.sleep(0.02)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert holder["process"].returncode is not None, "leader was not reaped"
    assert reports, "cleanup never ran"
    assert reports[-1].reaped is True
    assert reports[-1].group is GroupState.EMPTY
    assert reports[-1].group_cleared is True
    assert await group_is_gone(pgid_file)
    assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]


async def test_cancellation_during_emergency_cleanup_beats_the_deadline_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation on the timeout path is delivered, not overwritten.

    The spawn deadline expires, the handle is recovered afterwards, and the
    caller cancels while the emergency cleanup runs. What comes back has to be
    the caller's CancelledError -- reporting DEADLINE_EXCEEDED would answer a
    question nobody asked any more.
    """
    pgid_file = tmp_path / "pgid"
    directory = workspace(tmp_path, "hang_with_grandchild", pgid_out=str(pgid_file))
    terminating = asyncio.Event()
    reports: list[CleanupReport] = []

    monkeypatch.setattr(process_module, "_terminate", terminate_watcher(terminating, reports))

    task = asyncio.create_task(
        run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            # Work 1.5 s, reserve 0.5 s; the handle arrives well after both.
            deadline=Deadline(total_seconds=2.0, cleanup_reserve_seconds=0.5),
            on_stdout_line=lambda _line: None,
            spawn_process=spawn_held_back(pgid_file, hold_seconds=2.5),
        )
    )

    await terminating.wait()
    for _ in range(8):
        task.cancel()
        await asyncio.sleep(0.02)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert reports[-1].group is GroupState.EMPTY
    assert reports[-1].group_cleared is True
    assert await group_is_gone(pgid_file)
    assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]


async def test_a_spawn_failing_after_the_fork_never_starts_the_command(
    tmp_path: Path,
) -> None:
    """The launch gate, doing the job it exists for.

    asyncio forks before it connects the pipes, so a spawn can raise while a
    child is already running -- and, without the gate, while that child has
    already started descendants of its own. Here the failure happens after the
    fork, and the command still never executes: the gate was holding the process
    group and waiting, and the parent closed its end instead of releasing it.
    """
    pgid_file = tmp_path / "pgid"
    directory = workspace(tmp_path, "hang_with_grandchild", pgid_out=str(pgid_file))
    holder: dict[str, asyncio.subprocess.Process] = {}

    async def failing_after_fork(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        # Goes through the gate exactly as production does, then fails the way
        # a pipe-connection error would.
        process = await asyncio.create_subprocess_exec(*args, **kwargs)  # type: ignore[arg-type]
        holder["gate"] = process
        await asyncio.sleep(0.2)
        raise OSError("pipe connection failed")

    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=20.0, cleanup_reserve_seconds=2.0),
            on_stdout_line=lambda _line: None,
            spawn_process=failing_after_fork,
        )

    assert caught.value.failure is EvaluationFailure.PROCESS_START_FAILED
    # Nothing ran: the scenario writes this file as its first act.
    assert not pgid_file.exists()  # noqa: ASYNC240
    # And the report does not pretend to know the group is empty.
    assert caught.value.cleanup.group is GroupState.UNVERIFIED
    assert caught.value.cleanup.error_code == "SPAWN_FAILED_AFTER_FORK"
    assert caught.value.cleanup.group_cleared is False

    # The gate read EOF and exited on its own once the parent closed its end.
    assert await asyncio.wait_for(holder["gate"].wait(), timeout=5) == 0


async def test_a_failed_spawn_is_never_reported_as_an_empty_group(
    tmp_path: Path,
) -> None:
    """A spawn that raises does not prove that nothing was started.

    Reproduced with the gate stepped around, so a real leader and a real
    descendant exist at the moment the spawn fails. Claiming `EMPTY` here -- as
    the report used to -- would be a statement about a process tree nobody ever
    looked at.
    """
    pgid_file = tmp_path / "pgid"
    directory = workspace(tmp_path, "hang_with_grandchild", pgid_out=str(pgid_file))
    holder: dict[str, asyncio.subprocess.Process] = {}

    async def failing_after_real_start(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        direct = list(args)[5:]
        options = {key: value for key, value in kwargs.items() if key != "pass_fds"}
        process = await asyncio.create_subprocess_exec(*direct, **options)  # type: ignore[arg-type]
        holder["leader"] = process
        for _ in range(250):
            if pgid_file.exists():  # noqa: ASYNC240 - test-local polling
                break
            await asyncio.sleep(0.02)
        raise OSError("pipe connection failed")

    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=20.0, cleanup_reserve_seconds=2.0),
            on_stdout_line=lambda _line: None,
            spawn_process=failing_after_real_start,
        )

    assert pgid_file.exists(), "the scenario did start"  # noqa: ASYNC240
    assert caught.value.cleanup.group is not GroupState.EMPTY
    assert caught.value.cleanup.group is GroupState.UNVERIFIED
    assert caught.value.cleanup.group_cleared is False

    # This test created the tree deliberately, so this test removes it.
    pgid = int(pgid_file.read_text(encoding="utf-8"))  # noqa: ASYNC240
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    await asyncio.wait_for(holder["leader"].wait(), timeout=5)
    assert await group_is_gone(pgid_file)
