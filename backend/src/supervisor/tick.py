"""One supervised tick: one `--exits-once` child, bounded, classified and recorded.

The child is the existing exit job, unchanged: it takes `PAPER_EXIT_JOB`'s
advisory lock, records its own `paper_job_runs` row and judges its own health.
The tick adds what a single run cannot give itself:

- a hard wall clock. The run bounds itself at `PAPER_EXIT_RUN_MAX_SECONDS`, but
  a process stuck before that bound starts (start-up, a connect that never
  returns) bounds nothing. After `CHILD_HARD_TIMEOUT_SECONDS` the child's whole
  process group gets SIGTERM, then SIGKILL; its unfinished row then reads as a
  run that never finished, which no gate takes for evidence;
- a record across runs: consecutive failures, the last success, missed ticks
  (sleep, a stalled launchd job) and a tick that died mid-run.

Only codes leave the child: its stdout is parsed for the run summary's codes,
and its stderr is counted, never stored or printed.
"""

import fcntl
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from src.supervisor.models import (
    CHILD_HARD_TIMEOUT_SECONDS,
    CHILD_KILL_GRACE_SECONDS,
    EXIT_COMMAND,
    Category,
    SupervisorState,
    TickCode,
    TickResult,
    classify_child,
    settings_refusal,
    tick_gap,
)
from src.supervisor.state import StateUnreadable, load_state, record_result, save_state

BACKEND = Path(__file__).resolve().parents[2]


class SupervisedSettings(Protocol):
    """The few settings a tick reads before it starts anything."""

    @property
    def paper_exit_run_max_seconds(self) -> int: ...

    @property
    def paper_exit_liveness_max_age_seconds(self) -> int: ...

    @property
    def reasoning_provider(self) -> str: ...

    @property
    def codex_executable(self) -> str: ...


@dataclass(frozen=True)
class ChildOutcome:
    returncode: int | None
    stdout: str
    timed_out: bool
    stderr_bytes: int = 0


def exit_command(interpreter: str = sys.executable) -> list[str]:
    """The only command a tick starts: this interpreter, the exit job, nothing else."""
    return [interpreter, *EXIT_COMMAND]


def _signal_group(process: "subprocess.Popen[bytes]", sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_child(
    argv: list[str],
    *,
    cwd: Path = BACKEND,
    timeout: float = CHILD_HARD_TIMEOUT_SECONDS,
    kill_grace: float = CHILD_KILL_GRACE_SECONDS,
) -> ChildOutcome:
    """Run the child in its own process group; kill the whole group past the bound."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGTERM)
            try:
                out, err = process.communicate(timeout=kill_grace)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                try:
                    out, err = process.communicate(timeout=kill_grace)
                except subprocess.TimeoutExpired:
                    # A descendant outside the group still holds the pipes:
                    # stop reading rather than wait on it for ever.
                    err = b""
            process.poll()
            return ChildOutcome(process.returncode, "", True, len(err or b""))
    except BaseException:
        # The tick itself is being stopped (SIGTERM from launchd, an interrupt),
        # at any point, the kill grace included: the child lives in its own
        # process group and would outlive it.
        _signal_group(process, signal.SIGKILL)
        process.wait()
        raise
    return ChildOutcome(
        process.returncode, (out or b"").decode("utf-8", "replace"), False, len(err or b"")
    )


def _codex_missing(settings: SupervisedSettings) -> bool:
    """A configured Codex reasoning provider whose executable this PATH cannot find."""
    if settings.reasoning_provider != "codex":
        return False
    configured = settings.codex_executable.strip()
    if configured:
        return not (os.path.isfile(configured) and os.access(configured, os.X_OK))
    return shutil.which("codex") is None


TICK_LOCK = "tick.lock"
# Linux's identity of the current boot; it changes with every restart.
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def read_boot_id(path: Path = BOOT_ID_PATH) -> str | None:
    """This boot's identity where the platform keeps one; `None` elsewhere (macOS)."""
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if 0 < len(value) <= 64 else None


def run_tick(
    directory: Path,
    *,
    load_settings: Callable[[], SupervisedSettings],
    spawn: Callable[[list[str]], ChildOutcome] = run_child,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    boot_id: Callable[[], str | None] = read_boot_id,
) -> TickResult:
    """One tick, recorded whatever happens to it. Never raises for a failed child.

    One tick at a time per state directory: a second one (a manual wrapper run
    beside launchd's) finds the lock held and leaves the state file alone.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / TICK_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return TickResult(TickCode.TICK_ALREADY_RUNNING.value, Category.SKIPPED, None)
        return _locked_tick(
            directory, load_settings=load_settings, spawn=spawn, clock=clock, boot_id=boot_id
        )
    finally:
        os.close(descriptor)


def _locked_tick(
    directory: Path,
    *,
    load_settings: Callable[[], SupervisedSettings],
    spawn: Callable[[list[str]], ChildOutcome],
    clock: Callable[[], datetime],
    boot_id: Callable[[], str | None],
) -> TickResult:
    warnings: list[str] = []
    try:
        previous = load_state(directory) or SupervisorState()
    except StateUnreadable:
        previous = SupervisorState()
        warnings.append("STATE_RESET_UNREADABLE")
    started = clock()
    gap = tick_gap(previous.last_tick_started_at, started)
    if gap is not None:
        warnings.append("TICK_GAP_DETECTED")
    if previous.in_progress_since is not None:
        warnings.append("PREVIOUS_TICK_ABANDONED")
    boot = boot_id()
    restarted_at = previous.host_restarted_at
    if boot is not None and previous.boot_id is not None and boot != previous.boot_id:
        warnings.append("HOST_RESTART_DETECTED")
        restarted_at = started
    state = replace(
        previous,
        last_tick_started_at=started,
        in_progress_since=started,
        last_gap_seconds=gap,
        warnings=(),
        boot_id=boot if boot is not None else previous.boot_id,
        host_restarted_at=restarted_at,
    )
    result: TickResult | None = None
    try:
        settings = load_settings()
    except ValueError:
        result = TickResult(TickCode.SETTINGS_INVALID.value, Category.CONFIGURATION, None)
    else:
        refusal = settings_refusal(
            settings.paper_exit_run_max_seconds, settings.paper_exit_liveness_max_age_seconds
        )
        if refusal is not None:
            result = TickResult(refusal.value, Category.CONFIGURATION, None)
        elif _codex_missing(settings):
            warnings.append("CODEX_NOT_ON_PATH")
    state = replace(state, warnings=tuple(warnings))
    if result is None:
        save_state(directory, state)
        try:
            outcome = spawn(exit_command())
        except OSError:
            result = TickResult(TickCode.START_FAILED.value, Category.CRASH, None)
        else:
            result = classify_child(outcome.returncode, outcome.stdout, timed_out=outcome.timed_out)
    save_state(directory, record_result(state, result, finished_at=clock()))
    return result


__all__ = ["ChildOutcome", "exit_command", "read_boot_id", "run_child", "run_tick"]
