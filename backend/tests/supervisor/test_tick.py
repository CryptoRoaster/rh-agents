"""One supervised tick: what it starts, how it bounds it, and what it remembers.

Children are real processes where the behaviour is about processes — a hard
timeout, a crash, a grandchild — and a stand-in elsewhere. No child here is
ever the real runner: nothing touches a database, a provider or launchd.
"""

import json
import os
import signal
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.supervisor.models import TickCode
from src.supervisor.state import load_state
from src.supervisor.tick import ChildOutcome, exit_command, run_child, run_tick

T0 = datetime(2026, 10, 13, 18, tzinfo=UTC)


@dataclass
class FakeSettings:
    paper_exit_run_max_seconds: int = 60
    paper_exit_liveness_max_age_seconds: int = 180
    reasoning_provider: str = "disabled"
    codex_executable: str = ""


def ready_summary(**overrides) -> str:
    document = {
        "kind": "paper_run_summary",
        "mode": "EXITS_ONLY",
        "stop": "NOTHING_LEFT_TO_DO",
        "lock": "ACQUIRED",
        "exit_ready": True,
        "errors": [],
    }
    document.update(overrides)
    return json.dumps(document)


class Spawner:
    """Records every command a tick asks for and answers with a canned outcome."""

    def __init__(self, *outcomes: ChildOutcome):
        self.outcomes = list(outcomes)
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str]) -> ChildOutcome:
        self.commands.append(argv)
        return self.outcomes.pop(0)


def clock(*instants: datetime):
    pending = list(instants)
    return lambda: pending.pop(0) if len(pending) > 1 else pending[0]


def tick(tmp_path, spawner, *, at=T0, settings=None):
    return run_tick(
        tmp_path,
        load_settings=lambda: settings or FakeSettings(),
        spawn=spawner,
        clock=clock(at, at + timedelta(seconds=3)),
    )


OK = ChildOutcome(0, ready_summary(), False)


def test_a_tick_starts_the_exit_job_and_nothing_else(tmp_path):
    spawner = Spawner(OK)
    result = tick(tmp_path, spawner)
    assert result.ok
    assert spawner.commands == [[sys.executable, "-m", "src.runner.main", "--exits-once"]]


def test_the_command_cannot_name_any_other_mode():
    command = exit_command("/py")
    assert command == ["/py", "-m", "src.runner.main", "--exits-once"]
    for forbidden in ("--once", "--entries-once", "--scout-once", "--preflight"):
        assert forbidden not in command


def test_success_resets_and_failures_count(tmp_path):
    failed = ChildOutcome(1, json.dumps({"kind": "run_technical_failure", "reason": "X"}), False)
    tick(tmp_path, Spawner(failed), at=T0)
    tick(tmp_path, Spawner(failed), at=T0 + timedelta(seconds=60))
    state = load_state(tmp_path)
    assert state.consecutive_failures == 2
    assert state.last_failure["code"] == "EXIT_JOB_TECHNICAL_FAILURE"
    tick(tmp_path, Spawner(OK), at=T0 + timedelta(seconds=120))
    state = load_state(tmp_path)
    assert state.consecutive_failures == 0
    assert state.last_success_at == T0 + timedelta(seconds=123)
    assert state.in_progress_since is None


def test_a_held_lock_leaves_the_failure_count_alone(tmp_path):
    failed = ChildOutcome(
        0, ready_summary(exit_ready=False, exit_ready_reason="EXIT_BACKLOG"), False
    )
    tick(tmp_path, Spawner(failed))
    held = ChildOutcome(0, ready_summary(lock="ALREADY_RUNNING", exit_ready=None), False)
    result = tick(tmp_path, Spawner(held), at=T0 + timedelta(seconds=60))
    assert result.skipped
    assert load_state(tmp_path).consecutive_failures == 1


def test_a_database_failure_is_recorded_as_one(tmp_path):
    down = ChildOutcome(
        1, json.dumps({"kind": "run_technical_failure", "reason": "RUN_STARTUP_FAILED"}), False
    )
    result = tick(tmp_path, Spawner(down))
    assert result.code == "DATABASE_UNAVAILABLE"
    assert load_state(tmp_path).last_failure["category"] == "DATABASE"


def test_a_provider_failure_is_recorded_as_one(tmp_path):
    blind = ChildOutcome(
        0, ready_summary(exit_ready=False, exit_ready_reason="EXIT_ACQUISITION_INCOMPLETE"), False
    )
    result = tick(tmp_path, Spawner(blind))
    assert not result.ok and result.category.value == "PROVIDER"


def test_invalid_settings_start_nothing(tmp_path):
    spawner = Spawner()

    def broken():
        raise ValueError("DATABASE_URL is required")

    result = run_tick(tmp_path, load_settings=broken, spawn=spawner, clock=clock(T0))
    assert result.code == TickCode.SETTINGS_INVALID.value
    assert spawner.commands == []
    assert load_state(tmp_path).consecutive_failures == 1


def test_a_longer_exit_run_than_the_supervisor_allows_starts_nothing(tmp_path):
    spawner = Spawner()
    result = tick(tmp_path, spawner, settings=FakeSettings(paper_exit_run_max_seconds=120))
    assert result.code == "SUPERVISOR_EXIT_RUN_BOUND_EXCEEDED"
    assert spawner.commands == []


def test_a_child_that_cannot_be_started_is_a_crash(tmp_path):
    def unstartable(argv):
        raise FileNotFoundError("python")

    result = run_tick(tmp_path, load_settings=FakeSettings, spawn=unstartable, clock=clock(T0))
    assert result.code == "EXIT_JOB_START_FAILED"


def test_a_restart_continues_from_the_recorded_state(tmp_path):
    """A host reboot loses the process, not the record: the count goes on."""
    failed = ChildOutcome(-9, "", False)
    tick(tmp_path, Spawner(failed), at=T0)
    # A new process, a new tick: nothing carried over in memory.
    result = tick(tmp_path, Spawner(failed), at=T0 + timedelta(seconds=60))
    assert result.code == "EXIT_JOB_CRASHED"
    assert load_state(tmp_path).consecutive_failures == 2


def test_a_tick_that_died_mid_run_is_noticed_by_the_next(tmp_path):
    def killed(argv):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_tick(tmp_path, load_settings=FakeSettings, spawn=killed, clock=clock(T0))
    assert load_state(tmp_path).in_progress_since == T0
    tick(tmp_path, Spawner(OK), at=T0 + timedelta(seconds=60))
    assert "PREVIOUS_TICK_ABANDONED" in load_state(tmp_path).warnings


def test_sleep_and_wake_show_up_as_a_gap(tmp_path):
    tick(tmp_path, Spawner(OK), at=T0)
    tick(tmp_path, Spawner(OK), at=T0 + timedelta(minutes=45))
    state = load_state(tmp_path)
    assert state.last_gap_seconds == 45 * 60
    assert "TICK_GAP_DETECTED" in state.warnings
    tick(tmp_path, Spawner(OK), at=T0 + timedelta(minutes=46))
    assert load_state(tmp_path).warnings == ()


def test_a_missing_codex_is_a_warning_not_a_silent_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    tick(tmp_path, Spawner(OK), settings=FakeSettings(reasoning_provider="codex"))
    assert "CODEX_NOT_ON_PATH" in load_state(tmp_path).warnings


def test_an_unreadable_state_file_is_replaced_and_said(tmp_path):
    (tmp_path / "state.json").write_text("{broken")
    tick(tmp_path, Spawner(OK))
    assert "STATE_RESET_UNREADABLE" in load_state(tmp_path).warnings


def test_the_state_file_is_private(tmp_path):
    tick(tmp_path, Spawner(OK))
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


# -- real child processes ------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_hung_child_and_its_children_are_killed_at_the_hard_bound(tmp_path):
    pids = tmp_path / "pids"
    script = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    outcome = run_child([sys.executable, "-c", script], cwd=tmp_path, timeout=2, kill_grace=1)
    assert outcome.timed_out and outcome.stdout == ""
    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 5
    parent, grandchild = (int(item) for item in pids.read_text().split())
    while time.monotonic() < deadline and (alive(parent) or alive(grandchild)):
        time.sleep(0.05)
    assert not alive(parent) and not alive(grandchild)


def test_a_child_that_ignores_sigterm_is_killed(tmp_path):
    script = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    outcome = run_child([sys.executable, "-c", script], cwd=tmp_path, timeout=1, kill_grace=1)
    assert outcome.timed_out and outcome.returncode == -signal.SIGKILL


def test_a_crashing_child_is_reported_by_its_signal(tmp_path):
    script = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    outcome = run_child([sys.executable, "-c", script], cwd=tmp_path)
    assert outcome.returncode == -signal.SIGKILL and not outcome.timed_out


def test_the_childs_stderr_is_counted_never_kept(tmp_path):
    script = "import sys\nsys.stderr.write('postgresql://user:secret@host/db')\nprint('{}')\n"
    outcome = run_child([sys.executable, "-c", script], cwd=tmp_path)
    assert outcome.stderr_bytes > 0
    assert "secret" not in outcome.stdout


def test_an_interrupted_tick_takes_its_child_with_it(tmp_path):
    """launchd's SIGTERM becomes an exit in the tick; the child must not outlive it."""
    pid_file = tmp_path / "pid"
    child = (
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )

    def interrupt(signum, frame):
        raise SystemExit(143)

    previous = signal.signal(signal.SIGALRM, interrupt)
    try:
        signal.alarm(1)
        with pytest.raises(SystemExit):
            run_child([sys.executable, "-c", child], cwd=tmp_path, timeout=30)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.05)
    assert not alive(pid)


def test_the_real_child_command_path_exists():
    backend = Path(__file__).resolve().parents[2]
    assert (backend / "src" / "runner" / "main.py").is_file()


# -- review follow-ups -------------------------------------------------------


def test_one_tick_at_a_time_and_the_second_leaves_the_state_alone(tmp_path):
    import fcntl

    tick(tmp_path, Spawner(OK))
    before = (tmp_path / "state.json").read_text()
    holder = os.open(tmp_path / "tick.lock", os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spawner = Spawner()
        result = tick(tmp_path, spawner, at=T0 + timedelta(seconds=60))
    finally:
        os.close(holder)
    assert result.code == "SUPERVISOR_TICK_ALREADY_RUNNING" and result.skipped
    assert spawner.commands == []
    assert (tmp_path / "state.json").read_text() == before


def test_a_descendant_holding_the_pipes_cannot_hang_the_tick(tmp_path):
    """A grandchild in its own session survives the group kill; the tick must not wait on it."""
    pid_file = tmp_path / "escapee"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    try:
        outcome = run_child([sys.executable, "-c", script], cwd=tmp_path, timeout=1, kill_grace=1)
        assert outcome.timed_out
        assert time.monotonic() - started < 10
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_an_interruption_during_the_kill_grace_still_kills_the_child(tmp_path):
    pid_file = tmp_path / "pid"
    child = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    def interrupt(signum, frame):
        raise SystemExit(143)

    previous = signal.signal(signal.SIGALRM, interrupt)
    try:
        # Timeout after 1 s, then SIGTERM is ignored; the interruption lands
        # inside the 10-second grace.
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        with pytest.raises(SystemExit):
            run_child([sys.executable, "-c", child], cwd=tmp_path, timeout=1, kill_grace=10)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.05)
    assert not alive(pid)
