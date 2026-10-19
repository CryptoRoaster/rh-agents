"""The job units (V4 pool index, scout, entry) on systemd: what they run, and what they never do.

Nothing here touches systemd. `systemctl`, the backend's Python and `flock`
are stand-ins that record their arguments; unit states live in files under
the test's own temporary directory (the stand-in shared with the exit
supervisor's tests). The units must never start anything on install, the
entry job must never run without watched and alarmed exits and a ready
preflight, and nothing here may touch the exit supervisor's own units.
"""

import configparser
import re
import subprocess
from pathlib import Path

import pytest

from tests.ops.test_exit_supervisor_linux import SYSTEMCTL, calls, changes, executable, state

REPO = Path(__file__).resolve().parents[3]
JOBS = REPO / "ops" / "jobs" / "linux"
EXIT_OPS = REPO / "ops" / "exit-supervisor" / "linux"
INDEX = ("rh-agents-pool-index.service", "rh-agents-pool-index.timer")
SCOUT = ("rh-agents-scout.service", "rh-agents-scout.timer")
ENTRY = ("rh-agents-entry.service", "rh-agents-entry.timer")
UNITS = (*INDEX, *SCOUT, *ENTRY)
TIMERS = (INDEX[1], SCOUT[1], ENTRY[1])
EXIT_UNITS = (
    "rh-agents-exit-supervisor.service",
    "rh-agents-exit-supervisor.timer",
    "rh-agents-exit-alert.service",
    "rh-agents-exit-alert.timer",
    "rh-agents-heartbeat.service",
    "rh-agents-heartbeat.timer",
)
SUPERVISED = {"rh-agents-exit-supervisor.timer": "active", "rh-agents-exit-alert.timer": "active"}
HARDENING = (
    "UMask",
    "NoNewPrivileges",
    "CapabilityBoundingSet",
    "AmbientCapabilities",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectSystem",
    "ProtectHome",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectControlGroups",
    "ProtectClock",
    "ProtectHostname",
    "RestrictSUIDSGID",
    "RestrictRealtime",
    "RestrictNamespaces",
    "LockPersonality",
    "SystemCallArchitectures",
    "RestrictAddressFamilies",
)
SCRIPTS = ("install.sh", "start.sh", "stop.sh", "uninstall.sh", "run-job.sh")


def stand_ins(tmp_path: Path, *, states=None, codes=None, flock=0, preflight=""):
    """`systemctl` with unit states in files; a Python and a `flock` that record their calls."""
    units = tmp_path / "units.state"
    units.mkdir()
    for unit, value in (states or {}).items():
        (units / unit).write_text(value)
    executable(tmp_path / "systemctl", SYSTEMCTL.replace("@TMP@", str(tmp_path)))
    cases = "".join(f"  *' {mode}'*) exit {code} ;;\n" for mode, code in (codes or {}).items())
    executable(
        tmp_path / "python",
        f'echo "$PWD|$*" >> "{tmp_path}/python.calls"\n'
        f'case " $*" in *" --preflight"*) {preflight or ":"} ;; esac\n'
        f'case " $*" in\n{cases}esac\nexit 0\n',
    )
    executable(tmp_path / "pgrep", f'echo "$*" >> "{tmp_path}/pgrep.calls"\nexit 1\n')
    executable(tmp_path / "flock", f'echo "$*" >> "{tmp_path}/flock.calls"\nexit {flock}\n')
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_SYSTEMCTL": str(tmp_path / "systemctl"),
        "RH_AGENTS_PYTHON": str(tmp_path / "python"),
        "RH_AGENTS_FLOCK": str(tmp_path / "flock"),
        "RH_AGENTS_PGREP": str(tmp_path / "pgrep"),
        "RH_AGENTS_SYSTEMD_UNIT_DIR": str(tmp_path / "systemd"),
        "RH_AGENTS_STATE_ROOT": str(tmp_path / "state"),
        "RH_AGENTS_JOB_LOCK_DIR": str(tmp_path / "locks"),
        "RH_AGENTS_SERVICE_USER": "rh-agents",
        "RH_AGENTS_ALLOW_NON_MAIN": "1",
    }


def run(script: str, env, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(JOBS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def unit(env, name: str) -> configparser.ConfigParser:
    rendered = run("install.sh", env, "--dry-run").stdout
    section = rendered.split(f"# ---- {name}\n", 1)[1].split("# ---- ", 1)[0]
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(section)
    return parser


def installed(tmp_path: Path, env) -> None:
    assert run("install.sh", env).returncode == 0
    (tmp_path / "systemctl.calls").unlink()


def python_modes(tmp_path: Path) -> list[str]:
    return [call.split("|", 1)[1] for call in calls(tmp_path, "python")]


def touches_exit_units(tmp_path: Path) -> bool:
    return any(name in call for call in changes(tmp_path) for name in EXIT_UNITS)


# Units ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "job"), [(INDEX[0], "index"), (SCOUT[0], "scout"), (ENTRY[0], "entry")]
)
def test_each_job_unit_runs_its_wrapper_only_bounded_logged_and_confined(tmp_path, service, job):
    env = stand_ins(tmp_path)
    parsed = unit(env, service)
    section = parsed["Service"]
    assert section["Type"] == "oneshot"
    assert section["ExecStart"] == f"/bin/bash {REPO}/ops/jobs/linux/run-job.sh {job}"
    assert (section["User"], section["Group"]) == ("rh-agents", "rh-agents")
    assert section["WorkingDirectory"] == f"{REPO}/backend"
    # Database and network first; the runner is interrupted, never cut mid-write.
    assert "postgresql.service" in parsed["Unit"]["After"]
    assert parsed["Unit"]["Wants"] == "network-online.target"
    assert (section["KillSignal"], section["KillMode"]) == ("SIGINT", "control-group")
    assert int(section["TimeoutStopSec"]) <= 60
    assert 300 <= int(section["TimeoutStartSec"]) <= 1800
    assert section["StandardOutput"] == "journal"
    assert section["SyslogIdentifier"].startswith("rh-agents-")
    for key in (
        "NoNewPrivileges",
        "ProtectSystem",
        "ProtectHome",
        "PrivateTmp",
        "RestrictSUIDSGID",
    ):
        assert key in section
    assert section["CapabilityBoundingSet"] == ""
    assert section["MemoryMax"].endswith("M") and section["CPUQuota"].endswith("%")
    # Jobs hold no alert or heartbeat secret.
    assert "LoadCredential" not in section
    exit_service = (EXIT_OPS / "rh-agents-exit-supervisor.service.template").read_text()
    for key in HARDENING:
        expected = re.search(rf"^{key}=(.*)$", exit_service, re.MULTILINE)
        assert expected is not None and section[key] == expected.group(1), key
    lines = [
        line
        for line in run("install.sh", env, "--dry-run").stdout.splitlines()
        if "ExecStart" in line
    ]
    assert all("run-job.sh" in line for line in lines)


def test_the_index_unit_bounds_each_run_and_treats_a_partial_run_as_success(tmp_path):
    env = stand_ins(tmp_path)
    section = unit(env, INDEX[0])["Service"]
    assert section["SuccessExitStatus"] == "3"
    assert "RH_AGENTS_INDEX_MAX_REQUESTS=50" in section["Environment"]
    rendered = subprocess.run(
        ["/bin/bash", str(JOBS / "install.sh"), "--dry-run", "--index-max-requests", "120"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "RH_AGENTS_INDEX_MAX_REQUESTS=120" in rendered


@pytest.mark.parametrize("value", ["0", "2001", "abc", "", "-5"])
def test_an_unbounded_index_budget_is_refused_at_install(tmp_path, value):
    env = stand_ins(tmp_path)
    result = run("install.sh", env, "--index-max-requests", value)
    assert result.returncode == 2
    assert calls(tmp_path, "systemctl") == []


def test_the_entry_unit_is_ordered_after_the_exit_supervisor(tmp_path):
    env = stand_ins(tmp_path)
    assert "rh-agents-exit-supervisor.service" in unit(env, ENTRY[0])["Unit"]["After"]


@pytest.mark.parametrize(
    ("timer", "service"), [(INDEX[1], INDEX[0]), (SCOUT[1], SCOUT[0]), (ENTRY[1], ENTRY[0])]
)
def test_each_timer_restarts_its_job_every_five_minutes_from_boot(tmp_path, timer, service):
    env = stand_ins(tmp_path)
    parsed = unit(env, timer)
    assert parsed["Timer"]["Unit"] == service
    assert parsed["Timer"]["OnUnitActiveSec"] == "300"
    assert int(parsed["Timer"]["OnBootSec"]) >= 60
    assert parsed["Install"]["WantedBy"] == "timers.target"


def test_after_boot_the_entry_job_is_the_last_to_start(tmp_path):
    env = stand_ins(tmp_path)
    boot = {timer: int(unit(env, timer)["Timer"]["OnBootSec"]) for timer in TIMERS}
    # The exit supervisor ticks 60 s after boot; entries come after the index and the scout.
    assert boot[ENTRY[1]] > max(boot[INDEX[1]], boot[SCOUT[1]]) > 60


# Install ----------------------------------------------------------------------


def test_install_writes_every_unit_and_starts_nothing(tmp_path):
    env = stand_ins(tmp_path)
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (tmp_path / "systemd").iterdir()) == sorted(UNITS)
    made = changes(tmp_path)
    assert not [call for call in made if re.match(r"(enable|start|restart|stop)\b", call)]
    assert all(state(tmp_path, timer) == "inactive" for timer in TIMERS)
    assert not touches_exit_units(tmp_path)


@pytest.mark.parametrize("timer", TIMERS)
def test_install_never_replaces_a_running_job(tmp_path, timer):
    env = stand_ins(tmp_path, states={timer: "active"})
    result = run("install.sh", env)
    assert result.returncode == 1
    assert "INSTALL_REFUSED" in result.stderr
    assert not (tmp_path / "systemd").exists()


def test_install_is_refused_while_systemd_cannot_answer(tmp_path):
    env = stand_ins(tmp_path)
    (tmp_path / "show.fails").write_text("")
    assert run("install.sh", env).returncode == 1
    assert not (tmp_path / "systemd").exists()


# run-job ----------------------------------------------------------------------


def test_the_index_job_runs_one_bounded_increment(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_INDEX_MAX_REQUESTS": "50"}
    result = run("run-job.sh", env, "index")
    assert result.returncode == 0, result.stderr
    assert python_modes(tmp_path) == [
        "-m src.agents.atlas.v4.index_job --chain robinhood --max-requests 50"
    ]
    assert calls(tmp_path, "python")[0].startswith(f"{REPO}/backend|")


@pytest.mark.parametrize("value", ["", "0", "2001", "50;rm"])
def test_an_index_run_without_a_valid_budget_is_refused(tmp_path, value):
    env = stand_ins(tmp_path) | {"RH_AGENTS_INDEX_MAX_REQUESTS": value}
    assert run("run-job.sh", env, "index").returncode == 78
    assert python_modes(tmp_path) == []


def test_a_partial_index_run_passes_its_code_on(tmp_path):
    env = stand_ins(tmp_path, codes={"--max-requests": 3}) | {"RH_AGENTS_INDEX_MAX_REQUESTS": "50"}
    assert run("run-job.sh", env, "index").returncode == 3


def test_the_scout_job_runs_the_scout_only(tmp_path):
    env = stand_ins(tmp_path)
    assert run("run-job.sh", env, "scout").returncode == 0
    assert python_modes(tmp_path) == ["-m src.runner.main --scout-once"]


def test_the_entry_job_runs_after_a_ready_preflight_only_while_exits_are_watched(tmp_path):
    env = stand_ins(tmp_path, states=SUPERVISED)
    result = run("run-job.sh", env, "entry")
    assert result.returncode == 0, result.stderr
    assert python_modes(tmp_path) == [
        "-m src.runner.main --preflight",
        "-m src.runner.main --entries-once",
    ]


@pytest.mark.parametrize(
    "states",
    [
        {},
        {"rh-agents-exit-supervisor.timer": "active"},
        {"rh-agents-exit-alert.timer": "active"},
        {"rh-agents-exit-supervisor.timer": "failed", "rh-agents-exit-alert.timer": "active"},
    ],
    ids=["nothing", "no-alerts", "no-supervisor", "supervisor-failed"],
)
def test_the_entry_job_is_refused_without_watched_and_alarmed_exits(tmp_path, states):
    env = stand_ins(tmp_path, states=states)
    result = run("run-job.sh", env, "entry")
    assert result.returncode == 78
    assert "EXIT_SUPERVISION_NOT_ACTIVE" in result.stderr
    assert python_modes(tmp_path) == []


def test_the_entry_job_is_refused_when_systemd_cannot_answer(tmp_path):
    env = stand_ins(tmp_path, states=SUPERVISED)
    (tmp_path / "show.fails").write_text("")
    assert run("run-job.sh", env, "entry").returncode == 78
    assert python_modes(tmp_path) == []


@pytest.mark.parametrize("code", [1, 2])
def test_the_entry_job_is_refused_when_the_preflight_is_not_ready(tmp_path, code):
    env = stand_ins(tmp_path, states=SUPERVISED, codes={"--preflight": code})
    result = run("run-job.sh", env, "entry")
    assert result.returncode == 78
    assert f"PREFLIGHT_NOT_READY_{code}" in result.stderr
    assert python_modes(tmp_path) == ["-m src.runner.main --preflight"]


@pytest.mark.parametrize("job", ["index", "scout", "entry"])
def test_a_second_run_of_the_same_job_is_skipped(tmp_path, job):
    env = stand_ins(tmp_path, states=SUPERVISED, flock=75) | {"RH_AGENTS_INDEX_MAX_REQUESTS": "50"}
    result = run("run-job.sh", env, job)
    assert result.returncode == 0
    assert "ALREADY_RUNNING" in result.stdout
    assert python_modes(tmp_path) == []
    assert calls(tmp_path, "flock") == ["-n -E 75 9"]


@pytest.mark.parametrize("job", ["index", "scout", "entry"])
def test_a_job_refuses_a_checkout_off_the_required_branch(tmp_path, job):
    env = stand_ins(tmp_path, states=SUPERVISED) | {
        "RH_AGENTS_JOB_REQUIRE_BRANCH": "no-such-branch",
        "RH_AGENTS_INDEX_MAX_REQUESTS": "50",
    }
    result = run("run-job.sh", env, job)
    assert result.returncode == 78
    assert "RUNTIME_NOT_ON_NO-SUCH-BRANCH" in result.stderr
    assert python_modes(tmp_path) == []


def test_a_job_without_its_python_refuses(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_PYTHON": str(tmp_path / "missing")}
    assert run("run-job.sh", env, "scout").returncode == 127


@pytest.mark.parametrize("job", ["", "exits", "once", "index;scout"])
def test_an_unknown_job_runs_nothing(tmp_path, job):
    env = stand_ins(tmp_path)
    assert run("run-job.sh", env, job).returncode == 2
    assert python_modes(tmp_path) == []


# start / stop / uninstall -------------------------------------------------------


def test_a_start_needs_confirmation_and_a_reason(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    assert run("start.sh", env, "--job", "index", "--reason", "PILOT").returncode == 2
    assert run("start.sh", env, "--job", "index", "--confirm").returncode == 2
    assert run("start.sh", env, "--job", "index", "--confirm", "--reason", "lower").returncode == 2
    assert changes(tmp_path) == []


def test_a_start_of_an_uninstalled_job_is_refused(tmp_path):
    env = stand_ins(tmp_path)
    assert run("start.sh", env, "--job", "scout", "--confirm", "--reason", "PILOT").returncode == 1
    assert changes(tmp_path) == []


def test_the_entry_job_never_starts_before_exits_are_watched_and_alarmed(tmp_path):
    env = stand_ins(tmp_path, states={"rh-agents-exit-supervisor.timer": "active"})
    installed(tmp_path, env)
    result = run("start.sh", env, "--job", "entry", "--confirm", "--reason", "PILOT")
    assert result.returncode == 1
    assert changes(tmp_path) == []
    assert state(tmp_path, ENTRY[1]) == "inactive"


def test_a_start_is_audited_and_enables_that_job_only(tmp_path):
    env = stand_ins(tmp_path, states=SUPERVISED)
    installed(tmp_path, env)
    result = run("start.sh", env, "--job", "entry", "--confirm", "--reason", "PILOT_START")
    assert result.returncode == 0, result.stderr
    assert changes(tmp_path) == [f"enable --now {ENTRY[1]}"]
    audit = (tmp_path / "state" / "audit" / "jobs.jsonl").read_text()
    assert '"action":"start","job":"entry","reason":"PILOT_START"' in audit


def test_a_start_that_did_not_take_effect_is_undone(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / f"enable.fails.{INDEX[1]}").write_text("")
    result = run("start.sh", env, "--job", "index", "--confirm", "--reason", "PILOT")
    assert result.returncode == 1
    assert changes(tmp_path)[-1] == f"disable --now {INDEX[1]}"


def test_stop_all_stops_entries_first_and_never_the_exit_supervisor(tmp_path):
    states = {timer: "active" for timer in TIMERS} | SUPERVISED
    env = stand_ins(tmp_path, states=states)
    result = run("stop.sh", env, "--job", "all", "--reason", "PILOT_STOP")
    assert result.returncode == 0, result.stderr
    made = changes(tmp_path)
    assert made[0] == f"disable --now {ENTRY[1]}"
    assert all(state(tmp_path, timer) == "inactive" for timer in TIMERS)
    assert not touches_exit_units(tmp_path)
    assert state(tmp_path, "rh-agents-exit-supervisor.timer") == "active"


def test_a_stop_that_did_not_take_effect_says_so(tmp_path):
    env = stand_ins(tmp_path, states={SCOUT[1]: "active"})
    # A timer systemd does not stop: every disable fails.
    (tmp_path / "disable.fails").write_text("")
    result = run("stop.sh", env, "--job", "scout", "--reason", "PILOT_STOP")
    assert result.returncode == 1
    assert "FAILED" in result.stderr


def test_a_stop_needs_a_reason(tmp_path):
    env = stand_ins(tmp_path)
    assert run("stop.sh", env, "--job", "entry").returncode == 2
    assert changes(tmp_path) == []


def test_uninstall_is_refused_while_a_job_timer_runs(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / "units.state" / INDEX[1]).write_text("active")
    assert run("uninstall.sh", env).returncode == 1
    assert sorted(path.name for path in (tmp_path / "systemd").iterdir()) == sorted(UNITS)


def test_uninstall_removes_the_job_units_only(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    other = tmp_path / "systemd" / "rh-agents-exit-supervisor.timer"
    other.write_text("kept\n")
    assert run("uninstall.sh", env).returncode == 0
    assert [path.name for path in (tmp_path / "systemd").iterdir()] == [other.name]


# Whole directory ------------------------------------------------------------------


def test_only_start_enables_and_no_script_ever_changes_the_exit_supervisor():
    for path in JOBS.iterdir():
        text = path.read_text()
        if path.name != "start.sh":
            assert not re.search(r'SYSTEMCTL" (enable|start|restart|reload-or-restart)', text), path
        for name in EXIT_UNITS:
            assert not re.search(rf'SYSTEMCTL" \S+.*{re.escape(name)}', text), path


def test_no_job_ever_runs_exits_or_an_unbounded_mode():
    text = "\n".join(path.read_text() for path in JOBS.iterdir())
    assert not re.search(r"--(exits-once|once)\b", text)


def test_no_secret_address_or_database_url_in_any_job_file():
    for path in JOBS.iterdir():
        text = path.read_text()
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), path
        assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", text), path
        assert "postgresql+asyncpg://" not in text, path


def test_every_job_script_is_executable():
    for name in SCRIPTS:
        assert (JOBS / name).stat().st_mode & 0o111, name


def test_the_deploy_gate_also_waits_for_a_running_index_job():
    text = (EXIT_OPS / "deploy-check.sh").read_text()
    assert r"src\.agents\.atlas\.v4\.index_job" in text


@pytest.mark.parametrize("service", [SCOUT[0], ENTRY[0]])
def test_a_codex_login_lives_in_a_writable_state_directory(tmp_path, service):
    section = unit(stand_ins(tmp_path), service)["Service"]
    assert "CODEX_HOME=/var/lib/rh-agents/codex" in section["Environment"]
    assert "rh-agents/codex" in section["StateDirectory"].split()


@pytest.mark.parametrize("code", [1, 64, 127])
def test_a_lock_that_fails_for_another_reason_is_refused_not_skipped(tmp_path, code):
    env = stand_ins(tmp_path, flock=code)
    result = run("run-job.sh", env, "scout")
    assert result.returncode == 78
    assert "LOCK_FAILED" in result.stderr
    assert python_modes(tmp_path) == []


def test_a_stop_during_the_preflight_never_starts_the_entry_run(tmp_path):
    # The preflight passes, but the unit was told to stop while it ran.
    env = stand_ins(tmp_path, states=SUPERVISED, preflight="kill -INT $PPID; sleep 0.2")
    result = run("run-job.sh", env, "entry")
    assert result.returncode == 130
    assert python_modes(tmp_path) == ["-m src.runner.main --preflight"]


def test_uninstall_is_refused_while_a_job_runs(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / "units.state" / ENTRY[0]).write_text("activating")
    assert run("uninstall.sh", env).returncode == 1
    assert sorted(path.name for path in (tmp_path / "systemd").iterdir()) == sorted(UNITS)


def deploy_check(env) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(EXIT_OPS / "deploy-check.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "states",
    [{INDEX[1]: "active"}, {SCOUT[0]: "activating"}, {ENTRY[0]: "active"}, {ENTRY[1]: "unknown"}],
    ids=["index-timer", "scout-running", "entry-running", "entry-timer-unknown"],
)
def test_the_deploy_gate_refuses_while_a_job_unit_runs(tmp_path, states):
    env = stand_ins(tmp_path, states=states)
    result = deploy_check(env)
    assert result.returncode == 2
    assert "DEPLOY_REFUSED_JOB_UNIT_ACTIVE" in result.stdout


def test_the_deploy_gate_allows_idle_or_absent_job_units(tmp_path):
    env = stand_ins(tmp_path, states={ENTRY[0]: "failed"})
    result = deploy_check(env)
    assert result.returncode == 0, result.stdout + result.stderr
