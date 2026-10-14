"""The exit supervisor's launchd scripts: what they run, and what they never do.

Nothing here touches launchd. `launchctl` and `uv` are stand-ins that record
their arguments, so the exact calls — and the calls that must never happen —
are observed rather than read off a string. LaunchAgents, logs and state all
live under the test's own temporary directory.
"""

import os
import plistlib
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OPS = REPO / "ops" / "exit-supervisor"
LABEL = "com.rh-agents.exit-supervisor"
SCRIPTS = (
    "install.sh",
    "start.sh",
    "stop.sh",
    "uninstall.sh",
    "health.sh",
    "deploy-check.sh",
    "run-exit-supervisor.sh",
)
FILES = (*SCRIPTS, "common.sh", f"{LABEL}.plist.template")


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def stand_ins(tmp_path: Path, *, loaded: bool = False, running: bool = False, codes=None):
    """A `launchctl` that is loaded or not, and a `uv` answering per supervisor action."""
    codes = codes or {}
    (tmp_path / "launchd.state").write_text(
        "running" if running else "loaded" if loaded else "unloaded"
    )
    executable(
        tmp_path / "launchctl",
        f'echo "$*" >> "{tmp_path}/launchctl.calls"\n'
        f'state="$(cat "{tmp_path}/launchd.state")"\n'
        'case "$1" in\n'
        "  print)\n"
        '    [[ "$state" == unloaded ]] && exit 113\n'
        '    if [[ "$state" == running ]]; then printf "\\tstate = running\\n"\n'
        '    else printf "\\tstate = not running\\n"; fi\n'
        f'    [[ -f "{tmp_path}/print.padding" ]] && cat "{tmp_path}/print.padding"\n'
        "    exit 0 ;;\n"
        f'  bootout) [[ -f "{tmp_path}/bootout.fails" ]] && exit 5\n'
        f'    echo unloaded > "{tmp_path}/launchd.state" ;;\n'
        f'  bootstrap) [[ -f "{tmp_path}/bootstrap.fails" ]] && exit 5\n'
        f'    echo loaded > "{tmp_path}/launchd.state" ;;\n'
        "esac\nexit 0\n",
    )
    cases = "".join(f"  *' {action}'*) exit {code} ;;\n" for action, code in codes.items())
    executable(
        tmp_path / "uv",
        f'echo "$PWD|$*" >> "{tmp_path}/uv.calls"\ncase " $*" in\n{cases}esac\nexit 0\n',
    )
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_UV": str(tmp_path / "uv"),
        "RH_AGENTS_LAUNCHCTL": str(tmp_path / "launchctl"),
        "RH_AGENTS_LAUNCH_AGENTS": str(tmp_path / "agents"),
        "RH_AGENTS_EXIT_SUPERVISOR_LOG_DIR": str(tmp_path / "logs"),
        "RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR": str(tmp_path / "state"),
        "RH_AGENTS_EXIT_SUPERVISOR_ENV": str(tmp_path / "absent.env"),
        # The tests run on a feature branch; the branch rule has its own test.
        "RH_AGENTS_ALLOW_NON_MAIN": "1",
    }


def run(script: str, env, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(OPS / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def calls(tmp_path: Path, tool: str) -> list[str]:
    path = tmp_path / f"{tool}.calls"
    return path.read_text().splitlines() if path.exists() else []


def launchd_changes(tmp_path: Path) -> list[str]:
    return [line for line in calls(tmp_path, "launchctl") if not line.startswith("print")]


def installed(tmp_path: Path) -> Path:
    target = tmp_path / "agents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<plist/>")
    return target


# -- install ---------------------------------------------------------------


def test_the_rendered_plist_ticks_every_minute_from_the_runtime_backend(tmp_path):
    result = run("install.sh", stand_ins(tmp_path), "--dry-run")
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        f"{REPO}/ops/exit-supervisor/run-exit-supervisor.sh",
    ]
    assert plist["WorkingDirectory"] == f"{REPO}/backend"
    assert plist["StartInterval"] == 60
    assert plist["RunAtLoad"] is True
    assert plist["ProcessType"] == "Standard"
    assert "KeepAlive" not in plist
    variables = plist["EnvironmentVariables"]
    assert set(variables) == {
        "PATH",
        "RH_AGENTS_EXIT_SUPERVISOR_LOG_DIR",
        "RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR",
        "RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH",
    }
    assert variables["RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH"] == "main"
    assert variables["PATH"].startswith(str(tmp_path))
    # A dry run touches nothing.
    assert not (tmp_path / "agents").exists() and launchd_changes(tmp_path) == []


def test_installing_starts_nothing(tmp_path):
    env = stand_ins(tmp_path)
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "agents" / f"{LABEL}.plist").is_file()
    # Disabled, never enabled, bootstrapped, loaded or kickstarted.
    assert launchd_changes(tmp_path) == [f"disable gui/{os.getuid()}/{LABEL}"]
    assert calls(tmp_path, "uv") == []
    assert "not started" in result.stdout


def test_install_disables_before_the_plist_exists(tmp_path):
    env = stand_ins(tmp_path)
    target = tmp_path / "agents" / f"{LABEL}.plist"
    executable(
        tmp_path / "launchctl",
        f"present=$([[ -f {target} ]] && echo PRESENT || echo ABSENT)\n"
        f'echo "$* $present" >> "{tmp_path}/launchctl.calls"\n'
        '[[ "$1" == print ]] && exit 113\nexit 0\n',
    )
    run("install.sh", env)
    assert f"disable gui/{os.getuid()}/{LABEL} ABSENT" in calls(tmp_path, "launchctl")


def test_install_never_replaces_a_loaded_supervisor(tmp_path):
    result = run("install.sh", stand_ins(tmp_path, loaded=True))
    assert result.returncode != 0
    assert launchd_changes(tmp_path) == []


def test_install_refuses_a_branch_other_than_main(tmp_path):
    env = stand_ins(tmp_path)
    env.pop("RH_AGENTS_ALLOW_NON_MAIN")
    result = run("install.sh", env)
    branch = subprocess.run(
        ["git", "-C", str(REPO), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if branch == "main":
        assert result.returncode == 0
    else:
        assert result.returncode != 0 and launchd_changes(tmp_path) == []


# -- start -----------------------------------------------------------------


def test_start_needs_an_explicit_confirmation_and_a_reason(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path)
    for args in ((), ("--confirm",), ("--reason", "FIRST_EARLY_RUN")):
        result = run("start.sh", env, *args)
        assert result.returncode == 2
    assert launchd_changes(tmp_path) == [] and calls(tmp_path, "uv") == []


def test_start_is_audited_then_enables_and_loads(tmp_path):
    env = stand_ins(tmp_path)
    target = installed(tmp_path)
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_EARLY_RUN")
    assert result.returncode == 0, result.stderr
    assert [line.split("|")[1] for line in calls(tmp_path, "uv")] == [
        "run --locked python -m src.supervisor.main audit --action start --reason FIRST_EARLY_RUN"
    ]
    uid = os.getuid()
    assert launchd_changes(tmp_path) == [
        f"enable gui/{uid}/{LABEL}",
        f"bootstrap gui/{uid} {target}",
    ]


def test_start_refuses_without_an_install_or_when_already_loaded(tmp_path):
    assert run("start.sh", stand_ins(tmp_path), "--confirm", "--reason", "X").returncode == 1
    installed(tmp_path)
    loaded = stand_ins(tmp_path, loaded=True)
    assert run("start.sh", loaded, "--confirm", "--reason", "X").returncode == 1
    assert launchd_changes(tmp_path) == []


def test_a_failed_audit_starts_nothing(tmp_path):
    env = stand_ins(tmp_path, codes={"audit": 2})
    installed(tmp_path)
    assert run("start.sh", env, "--confirm", "--reason", "X").returncode != 0
    assert launchd_changes(tmp_path) == []


# -- stop ------------------------------------------------------------------


def test_a_refused_stop_check_stops_nothing(tmp_path):
    env = stand_ins(tmp_path, loaded=True, codes={"stop-check": 2})
    result = run("stop.sh", env, "--reason", "DONE")
    assert result.returncode == 2
    assert launchd_changes(tmp_path) == []
    assert not any("audit" in line for line in calls(tmp_path, "uv"))


def test_a_regular_stop_never_cuts_a_running_tick_short(tmp_path):
    env = stand_ins(tmp_path, loaded=True, running=True)
    assert run("stop.sh", env, "--reason", "DONE").returncode == 2
    assert launchd_changes(tmp_path) == []


def test_an_allowed_stop_is_audited_unloads_and_disables(tmp_path):
    env = stand_ins(tmp_path, loaded=True)
    result = run("stop.sh", env, "--reason", "BOOK_EMPTY")
    assert result.returncode == 0, result.stderr
    actions = [line.split("|")[1].split()[5] for line in calls(tmp_path, "uv")]
    assert actions == ["stop-check", "audit"]
    uid = os.getuid()
    assert launchd_changes(tmp_path) == [
        f"bootout gui/{uid}/{LABEL}",
        f"disable gui/{uid}/{LABEL}",
    ]


def test_an_emergency_stop_needs_its_own_acknowledgement(tmp_path):
    env = stand_ins(tmp_path, loaded=True)
    assert run("stop.sh", env, "--emergency", "--reason", "HOST_FAILING").returncode == 2
    assert launchd_changes(tmp_path) == [] and calls(tmp_path, "uv") == []


def test_an_emergency_stop_warns_audits_and_skips_the_guard(tmp_path):
    env = stand_ins(tmp_path, loaded=True, codes={"stop-check": 2})
    result = run(
        "stop.sh",
        env,
        "--emergency",
        "--reason",
        "HOST_FAILING",
        "--i-understand-exit-monitoring-stops",
    )
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr and "without automatic exits" in result.stderr
    assert [line.split("|")[1] for line in calls(tmp_path, "uv")] == [
        "run --locked python -m src.supervisor.main audit --action emergency-stop "
        "--reason HOST_FAILING"
    ]
    assert [line.split()[0] for line in launchd_changes(tmp_path)] == ["bootout", "disable"]


def test_an_emergency_stop_still_stops_when_the_audit_fails(tmp_path):
    env = stand_ins(tmp_path, loaded=True, codes={"audit": 1})
    result = run(
        "stop.sh", env, "--emergency", "--reason", "X", "--i-understand-exit-monitoring-stops"
    )
    assert result.returncode == 0
    assert "could not be audited" in result.stderr
    assert [line.split()[0] for line in launchd_changes(tmp_path)] == ["bootout", "disable"]


def test_uninstall_refuses_while_loaded(tmp_path):
    target = installed(tmp_path)
    assert run("uninstall.sh", stand_ins(tmp_path, loaded=True)).returncode == 1
    assert target.exists()
    assert run("uninstall.sh", stand_ins(tmp_path)).returncode == 0
    assert not target.exists()
    assert launchd_changes(tmp_path) == []


# -- read-only checks and the wrapper ----------------------------------------


def test_health_and_deploy_check_pass_their_codes_through(tmp_path):
    env = stand_ins(tmp_path, codes={"health": 2, "deploy-check": 2})
    assert run("health.sh", env).returncode == 2
    assert run("deploy-check.sh", env).returncode == 2
    assert launchd_changes(tmp_path) == []


def test_the_wrapper_runs_one_tick_from_the_backend(tmp_path):
    env = stand_ins(tmp_path)
    env.pop("RH_AGENTS_ALLOW_NON_MAIN")
    result = run("run-exit-supervisor.sh", env)
    assert result.returncode == 0, result.stderr
    cwd, args = calls(tmp_path, "uv")[0].split("|")
    assert Path(cwd).resolve() == (REPO / "backend").resolve()
    assert args.split() == ["run", "--locked", "python", "-m", "src.supervisor.main", "tick"]
    log = (tmp_path / "logs" / "exit-supervisor.log").read_text()
    assert "tick start" in log and "tick exit 0" in log


def test_the_wrapper_keeps_a_failed_ticks_code(tmp_path):
    result = run("run-exit-supervisor.sh", stand_ins(tmp_path, codes={"tick": 1}))
    assert result.returncode == 1


def test_the_wrapper_refuses_a_worktree_that_is_not_main(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH": "no-such-branch"}
    result = run("run-exit-supervisor.sh", env)
    assert result.returncode == 78
    assert calls(tmp_path, "uv") == []
    assert (
        "RUNTIME_NOT_ON_NO-SUCH-BRANCH" in (tmp_path / "logs" / "exit-supervisor.log").read_text()
    )


def test_nothing_but_the_exit_job_is_ever_started():
    for name in FILES:
        text = (OPS / name).read_text()
        for mode in ("--once", "--entries-once", "--scout-once", "--preflight"):
            assert mode not in text.replace("--exits-once", ""), (name, mode)
        assert "src.runner.main" not in text, name


def test_no_script_kickstarts_or_loads_outside_start():
    for name in FILES:
        text = (OPS / name).read_text()
        assert "kickstart" not in text, name
        if name != "start.sh":
            assert '"$LAUNCHCTL" enable' not in text, name
            assert '"$LAUNCHCTL" bootstrap' not in text, name


def test_no_secret_is_committed_in_the_supervisor():
    for name in FILES:
        text = (OPS / name).read_text().lower()
        for marker in ("sk-ant", "api_key=", "apikey", "password", "secret=", "postgresql://"):
            assert marker not in text, (name, marker)


def test_the_scripts_are_executable():
    for name in SCRIPTS:
        assert os.access(OPS / name, os.X_OK), name


# -- review follow-ups -------------------------------------------------------


def test_a_running_tick_is_seen_even_in_a_long_launchctl_answer(tmp_path):
    """`grep -q` on a pipe under pipefail would miss it; the guard must not."""
    env = stand_ins(tmp_path, loaded=True, running=True)
    (tmp_path / "print.padding").write_text("\tfiller = x\n" * 4000)
    assert run("stop.sh", env, "--reason", "DONE").returncode == 2
    assert launchd_changes(tmp_path) == []


def test_a_stop_that_did_not_take_effect_says_so(tmp_path):
    env = stand_ins(tmp_path, loaded=True)
    (tmp_path / "bootout.fails").write_text("")
    result = run("stop.sh", env, "--reason", "DONE")
    assert result.returncode == 1 and "still loaded" in result.stderr
    emergency = run(
        "stop.sh", env, "--emergency", "--reason", "X", "--i-understand-exit-monitoring-stops"
    )
    assert emergency.returncode == 1 and "NOT stopped" in emergency.stderr


def test_a_failed_load_leaves_the_service_disabled(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path)
    (tmp_path / "bootstrap.fails").write_text("")
    result = run("start.sh", env, "--confirm", "--reason", "X")
    assert result.returncode == 1
    assert [line.split()[0] for line in launchd_changes(tmp_path)] == [
        "enable",
        "bootstrap",
        "disable",
    ]


def test_the_env_file_cannot_lift_the_branch_rule(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH": "no-such-branch"}
    env_file = tmp_path / "supervisor.env"
    env_file.write_text("RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH=\n")
    env["RH_AGENTS_EXIT_SUPERVISOR_ENV"] = str(env_file)
    assert run("run-exit-supervisor.sh", env).returncode == 78
    assert calls(tmp_path, "uv") == []
