"""The exit supervisor's systemd scripts: what they run, and what they never do.

Nothing here touches systemd. `systemctl`, the backend's Python and `pgrep`
are stand-ins that record their arguments and keep unit states in files, so
the exact calls — and the calls that must never happen — are observed. Units,
state and credentials all live under the test's own temporary directory.
"""

import configparser
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OPS = REPO / "ops" / "exit-supervisor" / "linux"
SUPERVISOR_TIMER = "rh-agents-exit-supervisor.timer"
SUPERVISOR_SERVICE = "rh-agents-exit-supervisor.service"
ALERT_SERVICE = "rh-agents-exit-alert.service"
HEARTBEAT_SERVICE = "rh-agents-heartbeat.service"
ALERT_TIMER = "rh-agents-exit-alert.timer"
HEARTBEAT_TIMER = "rh-agents-heartbeat.timer"
UNITS = (
    SUPERVISOR_SERVICE,
    SUPERVISOR_TIMER,
    "rh-agents-exit-alert.service",
    ALERT_TIMER,
    "rh-agents-heartbeat.service",
    HEARTBEAT_TIMER,
)
SCRIPTS = (
    "install.sh",
    "start.sh",
    "stop.sh",
    "stop-alerts.sh",
    "uninstall.sh",
    "health.sh",
    "deploy-check.sh",
    "run-exit-supervisor.sh",
    "run-alert.sh",
    "run-heartbeat.sh",
)


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# A stand-in `systemctl`. Unit state lives in files under units.state/:
# `<unit>` ActiveState, `<unit>.enabled` UnitFileState, `<unit>.load` LoadState,
# `<unit>.result` Result. Without a file: inactive; loaded when the unit file
# exists, not-found otherwise; disabled when loaded. Failure switches are
# files in the test directory.
SYSTEMCTL = r"""
T="@TMP@"
U="$T/units.state"
echo "$*" >> "$T/systemctl.calls"
load_state() {
  if [[ -f "$U/$1.load" ]]; then cat "$U/$1.load"
  elif [[ -f "$RH_AGENTS_SYSTEMD_UNIT_DIR/$1" ]]; then echo loaded
  else echo not-found; fi
}
case "$1" in
  show)
    [[ -f "$T/show.fails" ]] && exit 1
    unit="$2" property="${3#--property=}"
    case "$property" in
      Result) if [[ -f "$U/$unit.result" ]]; then cat "$U/$unit.result"; else echo success; fi ;;
      ActiveState) if [[ -f "$U/$unit" ]]; then cat "$U/$unit"; else echo inactive; fi ;;
      LoadState) load_state "$unit" ;;
      UnitFileState)
        if [[ -f "$U/$unit.enabled" ]]; then cat "$U/$unit.enabled"
        elif [[ "$(load_state "$unit")" == loaded ]]; then echo disabled
        else echo; fi ;;
    esac ;;
  start)
    [[ -f "$T/start.fails.$2" ]] && exit 1
    exit 0 ;;
  enable)
    shift; now=""; [[ "$1" == --now ]] && { now=1; shift; }
    for u in "$@"; do
      [[ -f "$T/enable.fails.$u" ]] && exit 5
      echo enabled > "$U/$u.enabled"
      [[ -n "$now" ]] && echo active > "$U/$u"
    done ;;
  disable)
    shift; now=""; [[ "$1" == --now ]] && { now=1; shift; }
    [[ -f "$T/disable.fails" ]] && exit 5
    [[ -f "$T/disable.fails.after-reload" && -f "$T/reloaded" ]] && exit 5
    for u in "$@"; do
      [[ -f "$T/disable.fails.$u" ]] && exit 5
      [[ -f "$T/disable.ignored.$u" ]] || echo disabled > "$U/$u.enabled"
      [[ -n "$now" ]] && echo inactive > "$U/$u"
    done ;;
  daemon-reload)
    [[ -f "$T/daemon-reload.fails" ]] && exit 1
    touch "$T/reloaded"
    for f in "$T"/after-reload.*; do
      [[ -f "$f" ]] && cp "$f" "$U/${f##*/after-reload.}"
    done ;;
  stop)
    [[ -f "$T/stop.fails" ]] && exit 5
    echo inactive > "$U/$2" ;;
esac
exit 0
"""


def stand_ins(tmp_path: Path, *, states=None, codes=None, pgrep: int = 1, credentials=True):
    """A `systemctl` with unit states in files, a Python per action, a `pgrep`."""
    units = tmp_path / "units.state"
    units.mkdir()
    for unit, state in (states or {}).items():
        (units / unit).write_text(state)
    executable(tmp_path / "systemctl", SYSTEMCTL.replace("@TMP@", str(tmp_path)))
    cases = "".join(f"  *' {action}'*) exit {code} ;;\n" for action, code in (codes or {}).items())
    executable(
        tmp_path / "python",
        f'echo "$PWD|$*" >> "{tmp_path}/python.calls"\ncase " $*" in\n{cases}esac\nexit 0\n',
    )
    executable(tmp_path / "pgrep", f'echo "$*" >> "{tmp_path}/pgrep.calls"\nexit {pgrep}\n')
    secrets = tmp_path / "credentials"
    secrets.mkdir()
    if credentials:
        for name in ("telegram-token", "telegram-chat-id"):
            (secrets / name).write_text("placeholder\n")
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_SYSTEMCTL": str(tmp_path / "systemctl"),
        "RH_AGENTS_PYTHON": str(tmp_path / "python"),
        "RH_AGENTS_PGREP": str(tmp_path / "pgrep"),
        "RH_AGENTS_SYSTEMD_UNIT_DIR": str(tmp_path / "systemd"),
        "RH_AGENTS_STATE_ROOT": str(tmp_path / "state"),
        "RH_AGENTS_CREDENTIALS_DIR": str(secrets),
        "RH_AGENTS_SERVICE_USER": "rh-agents",
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


def changes(tmp_path: Path) -> list[str]:
    """Every systemctl call that is not a read."""
    return [call for call in calls(tmp_path, "systemctl") if not call.startswith("show ")]


def state(tmp_path: Path, unit: str) -> str:
    path = tmp_path / "units.state" / unit
    return path.read_text().strip() if path.exists() else "inactive"


def installed(tmp_path: Path, env) -> None:
    assert run("install.sh", env).returncode == 0
    (tmp_path / "systemctl.calls").unlink()


def unit(tmp_path: Path, env, name: str) -> configparser.ConfigParser:
    rendered = run("install.sh", env, "--dry-run").stdout
    section = rendered.split(f"# ---- {name}\n", 1)[1].split("# ---- ", 1)[0]
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(section)
    return parser


# Units ------------------------------------------------------------------------


def test_the_tick_unit_runs_the_wrapper_only_bounded_and_confined(tmp_path):
    env = stand_ins(tmp_path)
    service = unit(tmp_path, env, SUPERVISOR_SERVICE)["Service"]
    assert service["Type"] == "oneshot"
    assert (
        service["ExecStart"] == f"/bin/bash {REPO}/ops/exit-supervisor/linux/run-exit-supervisor.sh"
    )
    assert service["User"] == "rh-agents" and service["Group"] == "rh-agents"
    assert service["WorkingDirectory"] == f"{REPO}/backend"
    assert service["KillMode"] == "control-group"
    # Beyond the tick's own 85 s + 5 s bound, below nothing that could overlap.
    assert 95 <= int(service["TimeoutStartSec"]) <= 150
    for key, value in {
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "CapabilityBoundingSet": "",
        "MemoryMax": "800M",
        "UMask": "0077",
    }.items():
        assert service[key] == value, key
    assert "LoadCredential" not in service, "the exit tick never holds an alert credential"


def test_the_timers_tick_every_minute_from_boot_on(tmp_path):
    env = stand_ins(tmp_path)
    for timer in (SUPERVISOR_TIMER, ALERT_TIMER, HEARTBEAT_TIMER):
        parsed = unit(tmp_path, env, timer)
        assert parsed["Timer"]["OnUnitActiveSec"] == "60"
        assert "OnBootSec" in parsed["Timer"]
        assert parsed["Install"]["WantedBy"] == "timers.target"
    supervisor = unit(tmp_path, env, SUPERVISOR_TIMER)["Timer"]
    assert supervisor["Unit"] == SUPERVISOR_SERVICE


def test_the_alert_unit_gets_its_credentials_from_systemd(tmp_path):
    env = stand_ins(tmp_path)
    rendered = run("install.sh", env, "--dry-run", "--host-alias", "rh-vps").stdout
    section = rendered.split("# ---- rh-agents-exit-alert.service\n", 1)[1].split("# ---- ", 1)[0]
    credentials = env["RH_AGENTS_CREDENTIALS_DIR"]
    assert f"LoadCredential=telegram-token:{credentials}/telegram-token" in section
    assert f"LoadCredential=telegram-chat-id:{credentials}/telegram-chat-id" in section
    assert "Environment=RH_AGENTS_ALERT_HOST_ALIAS=rh-vps" in section
    assert "Environment=RH_AGENTS_ALERT_TRANSPORT=telegram" in section


@pytest.mark.parametrize("alias", ["10.1.2.3", "vps.example.com", "Upper", "a b"])
def test_install_refuses_an_alias_that_is_a_hostname_or_address(tmp_path, alias):
    env = stand_ins(tmp_path)
    result = run("install.sh", env, "--dry-run", "--host-alias", alias)
    assert result.returncode == 2


def test_the_heartbeat_target_may_name_a_user_and_a_port(tmp_path):
    env = stand_ins(tmp_path)
    rendered = run(
        "install.sh",
        env,
        "--dry-run",
        "--heartbeat-target",
        "deadman@watch.example",
        "--heartbeat-port",
        "2222",
    ).stdout
    assert "Environment=RH_AGENTS_HEARTBEAT_TARGET=deadman@watch.example" in rendered
    assert "Environment=RH_AGENTS_HEARTBEAT_PORT=2222" in rendered
    for bad in (("--heartbeat-target", "a b"), ("--heartbeat-port", "99999")):
        assert run("install.sh", env, "--dry-run", *bad).returncode == 2


def test_install_refuses_a_path_that_would_break_a_unit(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_CREDENTIALS_DIR": "/etc/rh agents|x"}
    assert run("install.sh", env, "--dry-run").returncode == 2


# Install -----------------------------------------------------------------------


def test_installing_enables_and_starts_nothing(tmp_path):
    env = stand_ins(tmp_path)
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert changes(tmp_path) == [
        "daemon-reload",
        f"disable {SUPERVISOR_TIMER} {ALERT_TIMER} {HEARTBEAT_TIMER}",
    ]
    for name in UNITS:
        path = tmp_path / "systemd" / name
        assert path.exists() and path.stat().st_mode & 0o777 == 0o644
    for timer in (SUPERVISOR_TIMER, ALERT_TIMER, HEARTBEAT_TIMER):
        assert state(tmp_path, timer) == "inactive"
    assert calls(tmp_path, "python") == []


@pytest.mark.parametrize("current", ["active", "activating", "failed"])
def test_install_never_replaces_a_timer_that_is_not_stopped(tmp_path, current):
    env = stand_ins(tmp_path, states={ALERT_TIMER: current})
    assert run("install.sh", env).returncode == 1
    assert changes(tmp_path) == []
    assert not (tmp_path / "systemd").exists()


def test_install_refuses_when_systemd_cannot_answer(tmp_path):
    env = stand_ins(tmp_path)
    (tmp_path / "show.fails").write_text("")
    assert run("install.sh", env).returncode == 1
    assert changes(tmp_path) == []


def test_install_refuses_a_branch_other_than_main(tmp_path):
    env = stand_ins(tmp_path)
    del env["RH_AGENTS_ALLOW_NON_MAIN"]
    branch = subprocess.run(
        ["git", "-C", str(REPO), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if branch == "main":
        pytest.skip("checked out on main")
    assert run("install.sh", env).returncode == 1
    assert changes(tmp_path) == []


# Install order and failures ---------------------------------------------------

TIMERS = (SUPERVISOR_TIMER, ALERT_TIMER, HEARTBEAT_TIMER)
SERVICES = (SUPERVISOR_SERVICE, ALERT_SERVICE, HEARTBEAT_SERVICE)


def present(tmp_path: Path, *units: str, text: str = "old\n") -> None:
    """Unit files from an earlier install."""
    directory = tmp_path / "systemd"
    directory.mkdir(exist_ok=True)
    for name in units:
        (directory / name).write_text(text)


def enablement(tmp_path: Path, unit: str) -> str:
    path = tmp_path / "units.state" / f"{unit}.enabled"
    return path.read_text().strip() if path.exists() else "disabled"


def never_starts(tmp_path: Path) -> None:
    for call in changes(tmp_path):
        assert not call.startswith(("enable", "start", "stop", "restart")), call


def test_an_enabled_idle_timer_is_disabled_before_any_file_changes(tmp_path):
    env = stand_ins(tmp_path, states={f"{SUPERVISOR_TIMER}.enabled": "enabled"})
    present(tmp_path, *UNITS)
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert "INSTALLED" in result.stdout
    made = changes(tmp_path)
    assert made == [
        f"disable {SUPERVISOR_TIMER}",
        f"disable {ALERT_TIMER}",
        f"disable {HEARTBEAT_TIMER}",
        "daemon-reload",
        f"disable {SUPERVISOR_TIMER} {ALERT_TIMER} {HEARTBEAT_TIMER}",
    ]
    assert all(enablement(tmp_path, timer) == "disabled" for timer in TIMERS)
    never_starts(tmp_path)


def test_a_failed_disable_refuses_before_any_file_changes(tmp_path):
    env = stand_ins(tmp_path, states={f"{ALERT_TIMER}.enabled": "enabled"})
    present(tmp_path, *UNITS)
    (tmp_path / f"disable.fails.{ALERT_TIMER}").write_text("")
    result = run("install.sh", env)
    assert result.returncode == 1
    assert f"INSTALL_REFUSED: could not disable {ALERT_TIMER}" in result.stderr
    assert all((tmp_path / "systemd" / name).read_text() == "old\n" for name in UNITS)
    assert "daemon-reload" not in changes(tmp_path)
    never_starts(tmp_path)


def test_a_disable_that_did_not_take_refuses(tmp_path):
    env = stand_ins(tmp_path, states={f"{HEARTBEAT_TIMER}.enabled": "enabled"})
    present(tmp_path, *UNITS)
    (tmp_path / f"disable.ignored.{HEARTBEAT_TIMER}").write_text("")
    result = run("install.sh", env)
    assert result.returncode == 1 and "still enabled" in result.stderr
    assert all((tmp_path / "systemd" / name).read_text() == "old\n" for name in UNITS)


@pytest.mark.parametrize(
    ("unit", "value"),
    [
        (f"{SUPERVISOR_TIMER}.load", "masked"),
        (f"{ALERT_TIMER}.load", "error"),
        (f"{HEARTBEAT_TIMER}.enabled", "masked"),
        (f"{SUPERVISOR_TIMER}.enabled", "unknown"),
        (SUPERVISOR_SERVICE, "activating"),
        (ALERT_SERVICE, "active"),
        (HEARTBEAT_SERVICE, "deactivating"),
    ],
)
def test_an_unexpected_or_running_state_refuses_without_changes(tmp_path, unit, value):
    env = stand_ins(tmp_path, states={unit: value})
    present(tmp_path, *UNITS)
    result = run("install.sh", env)
    assert result.returncode == 1 and "INSTALL_REFUSED" in result.stderr
    assert changes(tmp_path) == []


def test_a_failed_service_runs_nothing_and_does_not_block(tmp_path):
    env = stand_ins(tmp_path, states={SUPERVISOR_SERVICE: "failed"})
    assert run("install.sh", env).returncode == 0


def test_a_unit_that_cannot_be_written_fails_with_timers_disabled(tmp_path):
    env = stand_ins(tmp_path, states={f"{SUPERVISOR_TIMER}.enabled": "enabled"})
    present(tmp_path, *UNITS)
    (tmp_path / "systemd").chmod(0o555)
    try:
        result = run("install.sh", env)
    finally:
        (tmp_path / "systemd").chmod(0o755)
    assert result.returncode == 3 and "INSTALL_FAILED" in result.stderr
    assert "INSTALLED" not in result.stdout
    assert all(enablement(tmp_path, timer) == "disabled" for timer in TIMERS)
    assert "daemon-reload" not in changes(tmp_path)
    never_starts(tmp_path)


def test_a_failed_reload_fails_with_timers_disabled(tmp_path):
    env = stand_ins(tmp_path, states={f"{ALERT_TIMER}.enabled": "enabled"})
    present(tmp_path, *UNITS)
    (tmp_path / "daemon-reload.fails").write_text("")
    result = run("install.sh", env)
    assert result.returncode == 3 and "daemon-reload failed" in result.stderr
    assert all(enablement(tmp_path, timer) == "disabled" for timer in TIMERS)
    never_starts(tmp_path)


def test_a_failed_second_disable_fails(tmp_path):
    env = stand_ins(tmp_path)
    (tmp_path / "disable.fails.after-reload").write_text("")
    result = run("install.sh", env)
    assert result.returncode == 3 and "after the reload" in result.stderr


@pytest.mark.parametrize(
    ("unit", "value"),
    [
        (SUPERVISOR_TIMER, "active"),
        (f"{ALERT_TIMER}.enabled", "enabled"),
        (HEARTBEAT_SERVICE, "activating"),
    ],
)
def test_an_unexpected_state_after_the_install_fails_and_stops_nothing(tmp_path, unit, value):
    env = stand_ins(tmp_path)
    (tmp_path / f"after-reload.{unit}").write_text(value)
    if unit.endswith(".enabled"):
        # Enabled again behind the install's back, after its second disable.
        (tmp_path / f"disable.ignored.{ALERT_TIMER}").write_text("")
    result = run("install.sh", env)
    assert result.returncode == 3 and "INSTALL_FAILED" in result.stderr
    assert "INSTALLED" not in result.stdout
    never_starts(tmp_path)


def test_a_stale_enablement_link_does_not_come_back_with_the_file(tmp_path):
    # Enabled once, its file removed by hand: no file, but the link remains.
    env = stand_ins(tmp_path, states={f"{ALERT_TIMER}.enabled": "enabled"})
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert enablement(tmp_path, ALERT_TIMER) == "disabled"


def test_a_partial_earlier_install_is_completed_disabled(tmp_path):
    env = stand_ins(tmp_path, states={f"{SUPERVISOR_TIMER}.enabled": "enabled"})
    present(tmp_path, SUPERVISOR_SERVICE, SUPERVISOR_TIMER)
    result = run("install.sh", env)
    assert result.returncode == 0, result.stderr
    assert changes(tmp_path)[0] == f"disable {SUPERVISOR_TIMER}"
    assert all((tmp_path / "systemd" / name).read_text() != "old\n" for name in UNITS)
    assert all(enablement(tmp_path, timer) == "disabled" for timer in TIMERS)


def test_installing_twice_is_safe_and_starts_nothing(tmp_path):
    env = stand_ins(tmp_path)
    assert run("install.sh", env).returncode == 0
    first = {name: (tmp_path / "systemd" / name).read_text() for name in UNITS}
    assert run("install.sh", env).returncode == 0
    assert first == {name: (tmp_path / "systemd" / name).read_text() for name in UNITS}
    assert all(enablement(tmp_path, timer) == "disabled" for timer in TIMERS)
    assert all(state(tmp_path, unit) == "inactive" for unit in (*TIMERS, *SERVICES))
    never_starts(tmp_path)


def test_uninstall_disables_before_removing(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / "units.state" / f"{ALERT_TIMER}.enabled").write_text("enabled")
    assert run("uninstall.sh", env).returncode == 0
    made = changes(tmp_path)
    assert made.index(f"disable {ALERT_TIMER}") < made.index("daemon-reload")
    assert enablement(tmp_path, ALERT_TIMER) == "disabled"


# Start ------------------------------------------------------------------------


def test_start_needs_an_explicit_confirmation_and_a_reason(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    for args in ((), ("--confirm",), ("--reason", "FIRST_RUN")):
        assert run("start.sh", env, *args).returncode == 2
    assert changes(tmp_path) == [] and calls(tmp_path, "python") == []


def test_start_refuses_without_alert_credentials(tmp_path):
    env = stand_ins(tmp_path, credentials=False)
    installed(tmp_path, env)
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN")
    assert result.returncode == 1 and "without alerting" in result.stderr
    assert changes(tmp_path) == [] and calls(tmp_path, "python") == []


def test_start_refuses_without_an_install(tmp_path):
    env = stand_ins(tmp_path)
    assert run("start.sh", env, "--confirm", "--reason", "FIRST_RUN").returncode == 1
    assert changes(tmp_path) == []


def test_start_audits_then_starts_alerts_before_the_supervisor(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN")
    assert result.returncode == 0, result.stderr
    (audit,) = calls(tmp_path, "python")
    assert audit.endswith("-m src.supervisor.main audit --action start --reason FIRST_RUN")
    assert changes(tmp_path) == [
        f"enable --now {ALERT_TIMER}",
        f"start {ALERT_SERVICE}",
        f"enable --now {SUPERVISOR_TIMER}",
    ]
    assert state(tmp_path, HEARTBEAT_TIMER) == "inactive"


@pytest.mark.parametrize("failure", ["start", "result"])
def test_no_supervisor_unless_one_alert_pass_succeeds_first(tmp_path, failure):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    if failure == "start":
        (tmp_path / f"start.fails.{ALERT_SERVICE}").write_text("")
    else:
        (tmp_path / "units.state" / f"{ALERT_SERVICE}.result").write_text("exit-code")
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN")
    assert result.returncode == 1 and "alert path does not work" in result.stderr
    assert not any(SUPERVISOR_TIMER in call for call in changes(tmp_path))
    assert changes(tmp_path)[-1] == f"disable --now {ALERT_TIMER}"


def test_a_failed_audit_starts_nothing(tmp_path):
    env = stand_ins(tmp_path, codes={"audit": 2})
    installed(tmp_path, env)
    assert run("start.sh", env, "--confirm", "--reason", "FIRST_RUN").returncode != 0
    assert changes(tmp_path) == []


def test_a_supervisor_that_does_not_start_is_left_disabled(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / f"enable.fails.{SUPERVISOR_TIMER}").write_text("")
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN")
    assert result.returncode == 1
    assert changes(tmp_path)[-1] == f"disable --now {SUPERVISOR_TIMER}"
    assert state(tmp_path, SUPERVISOR_TIMER) == "inactive"


def test_no_supervisor_without_running_alerts(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / f"enable.fails.{ALERT_TIMER}").write_text("")
    assert run("start.sh", env, "--confirm", "--reason", "FIRST_RUN").returncode == 1
    assert not any(SUPERVISOR_TIMER in call for call in changes(tmp_path))


def test_the_heartbeat_starts_only_on_request_and_with_its_credentials(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN", "--with-heartbeat")
    assert result.returncode == 1 and "heartbeat credential" in result.stderr
    for name in ("heartbeat-key", "heartbeat-ssh-key", "heartbeat-known-hosts"):
        (Path(env["RH_AGENTS_CREDENTIALS_DIR"]) / name).write_text("placeholder\n")
    result = run("start.sh", env, "--confirm", "--reason", "FIRST_RUN", "--with-heartbeat")
    assert result.returncode == 0, result.stderr
    assert f"enable --now {HEARTBEAT_TIMER}" in changes(tmp_path)
    assert f"start {HEARTBEAT_SERVICE}" in changes(tmp_path)


# Stop -------------------------------------------------------------------------


def running(tmp_path: Path, **extra):
    return stand_ins(
        tmp_path, states={SUPERVISOR_TIMER: "active", ALERT_TIMER: "active", **extra}, **{}
    )


def test_a_refused_stop_check_stops_nothing(tmp_path):
    env = stand_ins(tmp_path, states={SUPERVISOR_TIMER: "active"}, codes={"stop-check": 2})
    assert run("stop.sh", env, "--reason", "DONE").returncode == 2
    assert changes(tmp_path) == []
    assert not any("audit" in call for call in calls(tmp_path, "python"))


@pytest.mark.parametrize("tick", ["activating", "unknown-state"])
def test_a_regular_stop_never_cuts_a_running_tick_short(tmp_path, tick):
    env = running(tmp_path, **{SUPERVISOR_SERVICE: tick})
    assert run("stop.sh", env, "--reason", "DONE").returncode == 2
    assert changes(tmp_path) == []


def test_an_allowed_stop_is_audited_and_disables_the_supervisor_only(tmp_path):
    env = running(tmp_path)
    result = run("stop.sh", env, "--reason", "DONE")
    assert result.returncode == 0, result.stderr
    assert [call.split("|", 1)[1] for call in calls(tmp_path, "python")] == [
        "-m src.supervisor.main stop-check",
        "-m src.supervisor.main audit --action stop --reason DONE",
    ]
    assert changes(tmp_path) == [f"disable --now {SUPERVISOR_TIMER}"]
    assert state(tmp_path, ALERT_TIMER) == "active"


def test_a_stop_that_did_not_take_effect_says_so(tmp_path):
    env = running(tmp_path)
    (tmp_path / "disable.fails").write_text("")
    result = run("stop.sh", env, "--reason", "DONE")
    assert result.returncode == 1 and "did not take effect" in result.stderr


def test_an_emergency_stop_needs_its_own_acknowledgement(tmp_path):
    env = running(tmp_path)
    assert run("stop.sh", env, "--emergency", "--reason", "INCIDENT").returncode == 2
    assert changes(tmp_path) == []


def test_an_emergency_stop_warns_audits_and_skips_the_guard(tmp_path):
    env = running(tmp_path, **{SUPERVISOR_SERVICE: "activating"})
    result = run(
        "stop.sh",
        env,
        "--emergency",
        "--reason",
        "INCIDENT",
        "--i-understand-exit-monitoring-stops",
    )
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    (audit,) = calls(tmp_path, "python")
    assert audit.endswith("audit --action emergency-stop --reason INCIDENT")
    assert changes(tmp_path) == [
        f"disable --now {SUPERVISOR_TIMER}",
        f"stop {SUPERVISOR_SERVICE}",
    ]
    assert state(tmp_path, ALERT_TIMER) == "active"


def test_an_emergency_stop_still_stops_when_the_audit_fails(tmp_path):
    env = stand_ins(tmp_path, states={SUPERVISOR_TIMER: "active"}, codes={"audit": 1})
    result = run(
        "stop.sh", env, "--emergency", "--reason", "X", "--i-understand-exit-monitoring-stops"
    )
    assert result.returncode == 0
    assert "could not be audited" in result.stderr
    assert state(tmp_path, SUPERVISOR_TIMER) == "inactive"


def test_an_emergency_stop_that_failed_says_so(tmp_path):
    env = running(tmp_path)
    (tmp_path / "disable.fails").write_text("")
    result = run(
        "stop.sh", env, "--emergency", "--reason", "X", "--i-understand-exit-monitoring-stops"
    )
    assert result.returncode == 1 and "has NOT stopped" in result.stderr


def test_an_emergency_stop_that_left_a_tick_running_says_so(tmp_path):
    env = running(tmp_path, **{SUPERVISOR_SERVICE: "activating"})
    (tmp_path / "stop.fails").write_text("")
    result = run(
        "stop.sh", env, "--emergency", "--reason", "X", "--i-understand-exit-monitoring-stops"
    )
    assert result.returncode == 1 and "a tick may still run" in result.stderr


def test_alerts_never_stop_while_the_supervisor_runs(tmp_path):
    env = running(tmp_path)
    assert run("stop-alerts.sh", env, "--reason", "MAINTENANCE").returncode == 2
    assert changes(tmp_path) == []


def test_alerts_stop_audited_once_the_supervisor_is_stopped(tmp_path):
    env = stand_ins(tmp_path, states={ALERT_TIMER: "active"})
    result = run("stop-alerts.sh", env, "--reason", "MAINTENANCE")
    assert result.returncode == 0, result.stderr
    (audit,) = calls(tmp_path, "python")
    assert audit.endswith("audit --action alerts-stop --reason MAINTENANCE")
    assert changes(tmp_path) == [f"disable --now {ALERT_TIMER} {HEARTBEAT_TIMER}"]


def test_uninstall_refuses_while_any_timer_runs(tmp_path):
    env = stand_ins(tmp_path)
    installed(tmp_path, env)
    (tmp_path / "units.state" / ALERT_TIMER).write_text("active")
    assert run("uninstall.sh", env).returncode == 1
    assert all((tmp_path / "systemd" / name).exists() for name in UNITS)
    (tmp_path / "units.state" / ALERT_TIMER).write_text("inactive")
    assert run("uninstall.sh", env).returncode == 0
    assert not any((tmp_path / "systemd" / name).exists() for name in UNITS)


# Health and deploy gate ---------------------------------------------------------


def test_health_passes_its_code_through(tmp_path):
    env = stand_ins(tmp_path, codes={"health": 2})
    assert run("health.sh", env).returncode == 2
    assert changes(tmp_path) == []


def test_the_deploy_gate_passes_only_when_everything_agrees(tmp_path):
    env = stand_ins(tmp_path)
    assert run("deploy-check.sh", env).returncode == 0


def test_the_deploy_gate_refuses_a_refused_check(tmp_path):
    env = stand_ins(tmp_path, codes={"deploy-check": 2})
    result = run("deploy-check.sh", env)
    assert result.returncode == 2 and "DEPLOY_REFUSED" in result.stderr


@pytest.mark.parametrize(
    ("pgrep", "code"),
    [(0, "DEPLOY_REFUSED_RUNNER_PROCESS_ALIVE"), (3, "DEPLOY_REFUSED_PROCESS_LIST_UNKNOWN")],
)
def test_the_deploy_gate_refuses_a_live_or_unknown_runner_process(tmp_path, pgrep, code):
    env = stand_ins(tmp_path, pgrep=pgrep)
    result = run("deploy-check.sh", env)
    assert result.returncode == 2 and code in result.stdout
    assert changes(tmp_path) == []


def test_a_crashed_deploy_check_refuses(tmp_path):
    env = stand_ins(tmp_path, codes={"deploy-check": 1})
    assert run("deploy-check.sh", env).returncode == 2


# Wrappers ----------------------------------------------------------------------


def test_the_tick_wrapper_runs_one_tick_from_the_backend(tmp_path):
    env = stand_ins(tmp_path)
    result = run("run-exit-supervisor.sh", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(tmp_path, "python") == [f"{REPO}/backend|-m src.supervisor.main tick"]


def test_the_tick_wrapper_keeps_a_failed_ticks_code(tmp_path):
    env = stand_ins(tmp_path, codes={"tick": 1})
    assert run("run-exit-supervisor.sh", env).returncode == 1


def test_the_tick_wrapper_refuses_a_checkout_that_is_not_main(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH": "no-such-branch"}
    result = run("run-exit-supervisor.sh", env)
    assert result.returncode == 78 and "RUNTIME_NOT_ON_NO-SUCH-BRANCH" in result.stdout
    assert calls(tmp_path, "python") == []


def test_the_tick_wrapper_refuses_without_python(tmp_path):
    env = stand_ins(tmp_path) | {"RH_AGENTS_PYTHON": str(tmp_path / "absent")}
    assert run("run-exit-supervisor.sh", env).returncode == 127


def test_the_alert_wrapper_runs_one_alert_pass(tmp_path):
    env = stand_ins(tmp_path)
    assert run("run-alert.sh", env).returncode == 0
    assert calls(tmp_path, "python") == [f"{REPO}/backend|-m src.supervisor.main alert"]


def test_the_heartbeat_wrapper_refuses_without_a_target_or_credentials(tmp_path):
    env = stand_ins(tmp_path)
    assert run("run-heartbeat.sh", env).returncode == 78
    env["RH_AGENTS_HEARTBEAT_TARGET"] = "deadman"
    assert run("run-heartbeat.sh", env).returncode == 78
    assert calls(tmp_path, "python") == []


def test_the_heartbeat_wrapper_pipes_one_line_over_pinned_ssh(tmp_path):
    env = stand_ins(tmp_path)
    executable(
        tmp_path / "ssh",
        f'echo "$*" >> "{tmp_path}/ssh.calls"\ncat >> "{tmp_path}/ssh.stdin"\n',
    )
    env |= {
        "RH_AGENTS_HEARTBEAT_TARGET": "deadman",
        "CREDENTIALS_DIRECTORY": str(tmp_path / "credentials"),
        "RH_AGENTS_SSH": str(tmp_path / "ssh"),
    }
    assert run("run-heartbeat.sh", env).returncode == 0
    (ssh,) = calls(tmp_path, "ssh")
    assert "StrictHostKeyChecking=yes" in ssh and "BatchMode=yes" in ssh
    assert "GlobalKnownHostsFile=/dev/null" in ssh and "-p 22" in ssh
    assert f"UserKnownHostsFile={tmp_path}/credentials/heartbeat-known-hosts" in ssh
    assert ssh.endswith("deadman")
    assert calls(tmp_path, "python") == [f"{REPO}/backend|-m src.supervisor.main heartbeat"]


# What never happens --------------------------------------------------------------


def test_nothing_but_the_supervisor_command_line_is_ever_run():
    forbidden = re.compile(r"--(entries-once|scout-once|once|preflight)\b|src\.runner\.main")
    for path in OPS.iterdir():
        assert not forbidden.search(path.read_text()), path.name


def test_only_start_enables_or_starts_a_unit():
    pattern = re.compile(r'SYSTEMCTL"? (enable|start|restart|reload-or-restart)\b')
    for script in SCRIPTS:
        found = pattern.findall((OPS / script).read_text())
        if script == "start.sh":
            assert set(found) == {"enable", "start"}
        else:
            assert found == [], script


def test_no_secret_or_address_is_committed_in_the_linux_supervisor():
    for path in OPS.iterdir():
        text = path.read_text()
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), path.name
        assert not re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", text), path.name
        assert "postgresql+asyncpg://" not in text, path.name


def test_the_scripts_are_executable():
    for script in SCRIPTS:
        assert (OPS / script).stat().st_mode & stat.S_IXUSR, script
