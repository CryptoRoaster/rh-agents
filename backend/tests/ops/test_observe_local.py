"""The local observation scripts (macOS): what they run, what they refuse, how they stop.

Nothing here loads a launchd agent, reaches a network or touches a database.
The scripts run from a throwaway git checkout holding a copy of ops/observe,
against a stand-in Python that records its arguments and environment, a
stand-in launchctl and a stand-in psql.
"""

import os
import plistlib
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OBSERVE = REPO / "ops" / "observe"
FILES = (
    "common.sh",
    "run-observe.sh",
    "start.sh",
    "stop.sh",
    "status.sh",
    "observe.env.example",
    "com.rh-agents.observe.plist.template",
)
PROFILE = (OBSERVE / "observe.env.example").read_text().replace("YOUR_ROLE", "observer")


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    checkout = tmp_path / "checkout"
    shutil.copytree(OBSERVE, checkout / "ops" / "observe")
    (checkout / "backend").mkdir()
    git = ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "observe"], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    python = executable(
        bin_dir / "python",
        f'echo "$PWD" > "{record}/cwd"\n'
        f'printf "%s\\n" "$@" > "{record}/args"\n'
        f'env | sort > "{record}/env"\n'
        f'if [[ -e "{record}/sleep" ]]; then\n'
        f"  trap 'echo terminated > \"{record}/signal\"; exit 143' TERM\n"
        "  for _ in $(seq 1 100); do sleep 0.1; done\n"
        "fi\n"
        f'exit "$(cat "{record}/exit" 2>/dev/null || echo 0)"\n',
    )
    executable(bin_dir / "launchctl", f'printf "%s\\n" "$*" >> "{record}/launchctl"\n')
    env_file = tmp_path / "observe.env"
    env_file.write_text(PROFILE)
    return {
        "tmp": tmp_path,
        "checkout": checkout,
        "bin": bin_dir,
        "record": record,
        "python": python,
        "env_file": env_file,
        "state": tmp_path / "state",
        "logs": tmp_path / "logs",
    }


def environment(box: dict[str, Path], **extra: str) -> dict[str, str]:
    return {
        "PATH": f"{box['bin']}:/usr/bin:/bin",
        "HOME": str(box["tmp"]),
        "RH_AGENTS_OBSERVE_ENV": str(box["env_file"]),
        "RH_AGENTS_OBSERVE_STATE_DIR": str(box["state"]),
        "RH_AGENTS_OBSERVE_LOG_DIR": str(box["logs"]),
        "RH_AGENTS_OBSERVE_PYTHON": str(box["python"]),
        "RH_AGENTS_LAUNCH_AGENTS": str(box["tmp"] / "agents"),
        **extra,
    }


def script(box: dict[str, Path], name: str, *args: str, **extra: str):
    return subprocess.run(
        ["/bin/bash", str(box["checkout"] / "ops" / "observe" / name), *args],
        env=environment(box, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def profile_with(box: dict[str, Path], **values: str) -> None:
    lines = [line for line in PROFILE.splitlines() if line.split("=", 1)[0] not in values]
    lines += [f"{key}={value}" for key, value in values.items()]
    box["env_file"].write_text("\n".join(lines) + "\n")


def log(box: dict[str, Path]) -> str:
    return (box["logs"] / "observe.log").read_text()


# ------------------------------------------------------------ one run


def test_a_run_starts_only_the_scout_mode_from_the_backend(box):
    result = script(box, "run-observe.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (box["record"] / "args").read_text().split() == ["-m", "src.runner.main", "--scout-once"]
    cwd = Path((box["record"] / "cwd").read_text().strip()).resolve()
    assert cwd == (box["checkout"] / "backend").resolve()
    assert "observe: start (database rh_agents_observe" in log(box)
    assert "observe: exit 0" in log(box)
    assert not (box["state"] / "running.pid").exists()


def test_the_scout_sees_the_profile_and_nothing_inherited(box):
    script(
        box,
        "run-observe.sh",
        PAPER_RUNNER_ENABLED="true",
        TRADING_MODE="PAPER",
        ANTHROPIC_API_KEY="inherited",
        RH_RPC_HTTP_URL="https://inherited.example",
    )
    seen = dict(
        line.split("=", 1)
        for line in (box["record"] / "env").read_text().splitlines()
        if "=" in line
    )
    assert seen["TRADING_MODE"] == "OBSERVE"
    assert seen["PAPER_RUNNER_ENABLED"] == "false"
    assert seen["EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY"] == "0"
    assert "ANTHROPIC_API_KEY" not in seen
    assert "RH_RPC_HTTP_URL" not in seen
    assert not [key for key in seen if key.startswith("RH_AGENTS_")]


def test_a_failed_run_keeps_its_exit_code(box):
    (box["record"] / "exit").write_text("3")
    result = script(box, "run-observe.sh")
    assert result.returncode == 3
    assert "observe: exit 3" in log(box)


def test_check_validates_and_runs_nothing(box):
    result = script(box, "run-observe.sh", "--check")
    assert result.returncode == 0
    assert "profile ok (database rh_agents_observe" in result.stdout
    assert not (box["record"] / "args").exists()


# ------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"TRADING_MODE": "PAPER"}, "OBSERVE_REQUIRES_TRADING_MODE"),
        ({"PAPER_RUNNER_ENABLED": "true"}, "OBSERVE_REQUIRES_PAPER_RUNNER_ENABLED"),
        ({"EARLY_SCOUT_ENABLED": "false"}, "OBSERVE_REQUIRES_EARLY_SCOUT_ENABLED"),
        ({"MARKET_CHAINS": "robinhood,bsc"}, "OBSERVE_REQUIRES_MARKET_CHAINS"),
        ({"REASONING_PROVIDER": "anthropic"}, "OBSERVE_REQUIRES_REASONING_PROVIDER"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@localhost/rh_agents"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@localhost/rh_agents_test"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer:pw@localhost/rh_agents_observe"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@db.example.com/rh_agents_observe"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        ({"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN": "1"}, "OBSERVE_MODEL_BUDGET_NOT_APPROVED"),
        ({"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY": "96"}, "OBSERVE_MODEL_BUDGET_NOT_APPROVED"),
        (
            {"COMMANDER_INTAKE_ENABLED": "true"},
            "OBSERVE_ENV_KEY_NOT_ALLOWED_COMMANDER_INTAKE_ENABLED",
        ),
        ({"PRE_VECTOR_EARLY_ENTRY_ENABLED": "true"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_PRE_VECTOR"),
        ({"EVM_RUNTIME_ENABLED": "true"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_EVM_RUNTIME_ENABLED"),
        ({"ANTHROPIC_API_KEY": "sk"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_ANTHROPIC_API_KEY"),
    ],
)
def test_a_profile_that_could_trade_or_leave_its_database_is_refused(box, values, reason):
    profile_with(box, **values)
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert reason in log(box)
    assert not (box["record"] / "args").exists()


def test_an_approved_model_budget_allows_orbit_reviews(box):
    profile_with(
        box,
        EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN="2",
        RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED="yes",
    )
    assert script(box, "run-observe.sh").returncode == 0


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("TRADING_MODE=OBSERVE\nTRADING_MODE=OBSERVE\n", "OBSERVE_ENV_KEY_REPEATED_TRADING_MODE"),
        ("CODEX_EXECUTABLE=$(touch /tmp/x)\n", "OBSERVE_ENV_MALFORMED"),
        ('TRADING_MODE="OBSERVE"\n', "OBSERVE_ENV_MALFORMED"),
        ("export TRADING_MODE=OBSERVE\n", "OBSERVE_ENV_MALFORMED"),
    ],
)
def test_the_env_file_is_read_never_sourced(box, text, reason):
    box["env_file"].write_text(text + PROFILE)
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert reason in log(box)


def test_a_missing_profile_is_refused(box):
    box["env_file"].unlink()
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_ENV_MISSING" in log(box)


def test_a_repository_env_file_is_refused(box):
    (box["checkout"] / ".env").write_text("PAPER_RUNNER_ENABLED=true\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_REPOSITORY_ENV_PRESENT" in log(box)


def test_a_modified_checkout_is_refused(box):
    with (box["checkout"] / "ops" / "observe" / "observe.env.example").open("a") as handle:
        handle.write("# changed\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_an_uncommitted_file_in_the_code_is_refused(box):
    (box["checkout"] / "ops" / "observe" / "extra.sh").write_text("echo\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)
    result = script(box, "start.sh", "--confirm", RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1")
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr
    assert not (box["record"] / "launchctl").exists()


# ------------------------------------------------------------ stop and restart


def test_the_stop_file_skips_every_run(box):
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    result = script(box, "run-observe.sh")
    assert result.returncode == 0
    assert "skipped STOP_FILE_PRESENT" in log(box)
    assert not (box["record"] / "args").exists()


def test_a_run_in_progress_is_never_joined_by_a_second(box):
    box["state"].mkdir()
    (box["state"] / "running.pid").write_text(f"{os.getpid()}\n")
    result = script(box, "run-observe.sh")
    assert result.returncode == 0
    assert "skipped RUN_IN_PROGRESS" in log(box)
    assert not (box["record"] / "args").exists()


def test_a_stale_pid_file_does_not_block_the_next_run(box):
    box["state"].mkdir()
    (box["state"] / "running.pid").write_text("999999\n")
    assert script(box, "run-observe.sh").returncode == 0
    assert (box["record"] / "args").exists()


def test_a_terminated_wrapper_stops_the_scout_and_waits_for_it(box):
    (box["record"] / "sleep").touch()
    wrapper = subprocess.Popen(
        ["/bin/bash", str(box["checkout"] / "ops" / "observe" / "run-observe.sh")],
        env=environment(box),
    )
    deadline = time.monotonic() + 10
    while not (box["record"] / "args").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    wrapper.send_signal(signal.SIGTERM)
    assert wrapper.wait(timeout=20) == 143
    assert (box["record"] / "signal").read_text().strip() == "terminated"
    assert "observe: exit 143" in log(box)
    assert not (box["state"] / "running.pid").exists()


def detached_sleep(seconds: int) -> int:
    """A process that is not ours: it is reaped by init, never left a zombie."""
    started = subprocess.run(
        ["/bin/bash", "-c", f"/bin/sleep {seconds} >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(started.stdout.strip())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_stop_sets_the_stop_file_and_unloads(box):
    result = script(box, "stop.sh")
    assert result.returncode == 0, result.stderr
    assert (box["state"] / "STOP").exists()
    assert "bootout" in (box["record"] / "launchctl").read_text()


def test_stop_waits_for_the_run_in_progress(box):
    box["state"].mkdir()
    pid = detached_sleep(2)
    (box["state"] / "running.pid").write_text(f"{pid}\n")
    started = time.monotonic()
    result = script(box, "stop.sh")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started >= 1.5
    assert not alive(pid)
    assert "bootout" in (box["record"] / "launchctl").read_text()


def test_stop_gives_up_loudly_and_keeps_the_stop_file(box):
    box["state"].mkdir()
    pid = detached_sleep(30)
    try:
        (box["state"] / "running.pid").write_text(f"{pid}\n")
        result = script(box, "stop.sh", RH_AGENTS_OBSERVE_STOP_WAIT_SECONDS="1")
        assert result.returncode == 1
        assert "OBSERVE_STOP_TIMEOUT" in result.stderr
        assert (box["state"] / "STOP").exists()
        assert not (box["record"] / "launchctl").exists()
    finally:
        os.kill(pid, signal.SIGKILL)


def test_stop_now_interrupts_the_run_in_progress(box):
    box["state"].mkdir()
    pid = detached_sleep(30)
    (box["state"] / "running.pid").write_text(f"{pid}\n")
    result = script(box, "stop.sh", "--now")
    assert result.returncode == 0, result.stderr
    assert not alive(pid)


# ------------------------------------------------------------ start


def test_start_needs_an_explicit_mode(box):
    result = script(box, "start.sh")
    assert result.returncode == 78
    assert not (box["record"] / "launchctl").exists()


def test_the_rendered_agent_runs_the_wrapper_every_five_minutes(box):
    result = script(box, "start.sh", "--dry-run", RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1")
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode())
    checkout = box["checkout"]
    assert plist["Label"] == "com.rh-agents.observe"
    assert plist["ProgramArguments"][1] == f"{checkout}/ops/observe/run-observe.sh"
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"] is False
    assert plist["EnvironmentVariables"]["RH_AGENTS_OBSERVE_ENV"] == str(box["env_file"])
    assert not (box["record"] / "launchctl").exists()


def test_start_refuses_a_commit_that_is_not_on_main(box):
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 78
    assert "OBSERVE_COMMIT_NOT_ON_MAIN" in result.stderr
    assert not (box["record"] / "launchctl").exists()


def test_start_refuses_a_profile_that_could_trade(box):
    profile_with(box, PAPER_RUNNER_ENABLED="true")
    result = script(box, "start.sh", "--confirm", RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1")
    assert result.returncode == 78
    assert "OBSERVE_REQUIRES_PAPER_RUNNER_ENABLED" in result.stderr


def test_start_clears_the_stop_file_and_loads_the_agent(box):
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    executable(box["bin"] / "plutil", "exit 0\n")
    result = script(box, "start.sh", "--confirm", RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1")
    assert result.returncode == 0, result.stderr
    assert not (box["state"] / "STOP").exists()
    assert "bootstrap" in (box["record"] / "launchctl").read_text()
    assert (box["tmp"] / "agents" / "com.rh-agents.observe.plist").exists()


@pytest.mark.parametrize("interval", ["60", "299", "3601", "5m"])
def test_the_interval_stays_within_five_minutes_and_an_hour(box, interval):
    result = script(
        box,
        "start.sh",
        "--dry-run",
        RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1",
        RH_AGENTS_OBSERVE_INTERVAL_SECONDS=interval,
    )
    assert result.returncode == 78


# ------------------------------------------------------------ status


def fake_psql(box: dict[str, Path], invariants: str) -> None:
    executable(
        box["bin"] / "psql",
        'query="${@: -1}"\n'
        'case "$query" in\n'
        "  *alembic_version*) echo 0026 ;;\n"
        "  *extract*) echo 120 ;;\n"
        '  *"order by started_at desc limit 1"*) echo COMPLETED ;;\n'
        f'  *trade_cases*) echo "{invariants}" ;;\n'
        "  *) echo 0 ;;\n"
        "esac\n",
    )


def test_status_is_healthy_without_trading_state(box):
    fake_psql(box, "0 0 0")
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    result = script(box, "status.sh")
    assert result.returncode == 0, result.stdout
    assert "trade_cases=0 order_intents=0 positions=0" in result.stdout
    assert "HEALTHY" in result.stdout


def test_status_flags_any_trading_state_in_the_observation_database(box):
    fake_psql(box, "0 0 1")
    result = script(box, "status.sh")
    assert result.returncode == 2
    assert "VIOLATED" in result.stdout


# ------------------------------------------------------------ the files themselves


def test_nothing_but_the_scout_mode_is_ever_started():
    for name in FILES:
        text = (OBSERVE / name).read_text()
        for mode in ("--once", "--entries-once", "--exits-once"):
            assert mode not in text.replace("--scout-once", ""), (name, mode)


def test_no_secret_is_committed():
    for name in FILES:
        text = (OBSERVE / name).read_text().lower()
        for marker in ("sk-ant", "api_key=", "apikey", "password=", "secret="):
            assert marker not in text, (name, marker)
