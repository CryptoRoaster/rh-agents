"""The Linux side of the supervisor: systemd status, host restarts, and new guards.

`systemctl` is a stand-in script; nothing here asks a real service manager.
"""

import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.orchestration.jobruns.models import Coverage, EvidenceRow, ExitLivenessGate
from src.supervisor import main as cli
from src.supervisor.deadman import verify
from src.supervisor.models import (
    DatabaseFacts,
    HealthStatus,
    ServiceStatus,
    SupervisorState,
    deploy_refusals,
    evaluate_health,
)
from src.supervisor.probe import (
    parse_systemd,
    service_backend,
    service_status,
    systemd_service_status,
)
from src.supervisor.state import load_state, save_state
from src.supervisor.tick import read_boot_id, run_tick
from tests.supervisor.test_tick import FakeSettings, ready_summary

T0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
GATE = ExitLivenessGate(max_age_seconds=180, run_max_seconds=60)


def fake_systemctl(tmp_path: Path, timer: str, service: str, code: int = 0) -> str:
    path = tmp_path / "systemctl"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"[[ {code} -ne 0 ]] && exit {code}\n"
        'case "$2" in\n'
        f"  *.timer) printf '%s' '{timer}' ;;\n"
        f"  *.service) printf '%s' '{service}' ;;\n"
        "esac\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


ARMED = "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
IDLE_TICK = (
    "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n"
)
RUNNING_TICK = "LoadState=loaded\nActiveState=activating\nSubState=start\nExecMainStatus=0\n"


def test_an_armed_timer_is_a_loaded_supervisor(tmp_path):
    status = systemd_service_status(systemctl=fake_systemctl(tmp_path, ARMED, IDLE_TICK))
    assert status == ServiceStatus(loaded=True, running=False, last_exit_status=0)


def test_a_running_oneshot_tick_is_running(tmp_path):
    status = systemd_service_status(systemctl=fake_systemctl(tmp_path, ARMED, RUNNING_TICK))
    assert status.loaded is True and status.running is True


def test_not_installed_and_stopped_are_not_loaded():
    assert parse_systemd({"LoadState": "not-found"}, {}).loaded is False
    stopped = {"LoadState": "loaded", "ActiveState": "inactive"}
    assert parse_systemd(stopped, {"ExecMainStatus": "1"}) == ServiceStatus(
        loaded=False, last_exit_status=1
    )


@pytest.mark.parametrize("state", ["failed", "activating", "deactivating", ""])
def test_any_other_timer_state_is_unknown(state):
    assert parse_systemd({"LoadState": "loaded", "ActiveState": state}, {}).loaded is None


def test_a_systemctl_that_cannot_answer_is_unknown(tmp_path):
    assert systemd_service_status(systemctl=fake_systemctl(tmp_path, "", "", code=1)).loaded is None
    assert systemd_service_status(systemctl=str(tmp_path / "absent")).loaded is None


def test_the_backend_follows_the_setting_then_the_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("RH_AGENTS_SUPERVISOR_BACKEND", "systemd")
    assert service_backend() == "systemd"
    monkeypatch.setenv("RH_AGENTS_SYSTEMCTL", fake_systemctl(tmp_path, ARMED, IDLE_TICK))
    assert service_status().loaded is True
    monkeypatch.setenv("RH_AGENTS_SUPERVISOR_BACKEND", "upstart")
    assert service_backend() is None
    assert service_status().loaded is None
    monkeypatch.delenv("RH_AGENTS_SUPERVISOR_BACKEND")
    monkeypatch.setattr(sys, "platform", "linux")
    assert service_backend() == "systemd"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert service_backend() == "launchd"


# Host restarts -------------------------------------------------------------------


def test_the_boot_identity_is_read_where_it_exists(tmp_path):
    path = tmp_path / "boot_id"
    path.write_text("4b2f0c8e-0000-4000-8000-000000000001\n")
    assert read_boot_id(path) == "4b2f0c8e-0000-4000-8000-000000000001"
    assert read_boot_id(tmp_path / "absent") is None


def ready(argv):
    from src.supervisor.tick import ChildOutcome

    return ChildOutcome(0, ready_summary(), False)


def tick(tmp_path, at, boot):
    return run_tick(
        tmp_path, load_settings=FakeSettings, spawn=ready, clock=lambda: at, boot_id=lambda: boot
    )


def test_a_changed_boot_identity_is_a_host_restart(tmp_path):
    tick(tmp_path, T0, "boot-a")
    tick(tmp_path, T0 + timedelta(minutes=1), "boot-a")
    state = load_state(tmp_path)
    assert state is not None and state.host_restarted_at is None
    tick(tmp_path, T0 + timedelta(minutes=5), "boot-b")
    state = load_state(tmp_path)
    assert state is not None
    assert state.boot_id == "boot-b"
    assert state.host_restarted_at == T0 + timedelta(minutes=5)
    assert "HOST_RESTART_DETECTED" in state.warnings


def test_no_boot_identity_is_no_restart(tmp_path):
    tick(tmp_path, T0, None)
    tick(tmp_path, T0 + timedelta(minutes=1), None)
    state = load_state(tmp_path)
    assert state is not None and state.host_restarted_at is None and state.boot_id is None


def test_a_state_file_without_the_new_fields_still_reads(tmp_path):
    save_state(tmp_path, SupervisorState(last_tick_started_at=T0))
    document = json.loads((tmp_path / "state.json").read_text())
    for key in ("boot_id", "host_restarted_at"):
        document.pop(key)
    (tmp_path / "state.json").write_text(json.dumps(document))
    assert load_state(tmp_path) == SupervisorState(last_tick_started_at=T0)


def facts(*, positions: int = 0, **flags) -> DatabaseFacts:
    finished = T0 - timedelta(seconds=20)
    return DatabaseFacts(
        evidence=[
            EvidenceRow(
                outcome="SUCCEEDED",
                started_at=finished - timedelta(seconds=5),
                finished_at=finished,
                exit_ready=True,
                exit_ready_reason=None,
                policy_classes=["EARLY", "NORMAL"],
            )
        ],
        coverage=Coverage(early=positions),
        last_ready_finished_at=finished,
        **flags,
    )


def health(state: SupervisorState, database: DatabaseFacts | None):
    return evaluate_health(
        now=T0,
        service=ServiceStatus(loaded=True),
        state=state,
        state_readable=True,
        database=database,
        gate=GATE,
    )


def test_a_recent_host_restart_degrades_health_for_a_while():
    restarted = SupervisorState(
        last_tick_started_at=T0 - timedelta(seconds=10),
        host_restarted_at=T0 - timedelta(minutes=5),
    )
    report = health(restarted, facts())
    assert report.status is HealthStatus.DEGRADED
    assert "HOST_RESTARTED_RECENTLY" in report.reasons
    long_ago = SupervisorState(
        last_tick_started_at=T0 - timedelta(seconds=10),
        host_restarted_at=T0 - timedelta(hours=1),
    )
    assert health(long_ago, facts()).status is HealthStatus.HEALTHY
    in_the_future = SupervisorState(
        last_tick_started_at=T0 - timedelta(seconds=10),
        host_restarted_at=T0 + timedelta(hours=1),
    )
    assert health(in_the_future, facts()).status is HealthStatus.HEALTHY


def test_a_deploy_waits_for_a_running_entry_job():
    refusals = deploy_refusals(facts(entry_run_active=True), ServiceStatus(loaded=False))
    assert refusals == ["DEPLOY_REFUSED_ENTRY_RUN_ACTIVE"]
    assert deploy_refusals(facts(), ServiceStatus(loaded=False)) == []


# The alert and heartbeat commands -----------------------------------------------


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RH_AGENTS_ALERT_STATE_DIR", str(tmp_path / "alerts"))
    monkeypatch.setenv("RH_AGENTS_ALERT_HOST_ALIAS", "rh-vps")
    monkeypatch.delenv("RH_AGENTS_ALERT_TRANSPORT", raising=False)
    found: dict = {}

    def gather(now):
        if "crash" in found:
            raise RuntimeError("postgresql+asyncpg://user:secret@host/db")
        state = SupervisorState(last_tick_started_at=now - timedelta(seconds=10))
        return found["service"], state, True, found["database"], GATE

    monkeypatch.setattr(cli, "gather", gather)
    return found


def invoke(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["supervisor", *argv])
    code = cli.main()
    captured = capsys.readouterr()
    return code, captured


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> str | None:
        self.sent.append(text)
        return None


def test_an_open_position_without_supervision_alerts_critical(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=False), database=facts(positions=1))
    recorder = Recorder()
    monkeypatch.setattr(cli, "transport_from_environment", lambda: recorder)
    code, captured = invoke(monkeypatch, capsys, "alert")
    document = json.loads(captured.out)
    assert code == 0 and document["sent"] is True and document["status"] == "CRITICAL"
    (text,) = recorder.sent
    assert "OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING" in text and "open positions: 1" in text


def test_a_crashed_health_check_alerts_without_its_message(world, monkeypatch, capsys):
    world["crash"] = True
    recorder = Recorder()
    monkeypatch.setattr(cli, "transport_from_environment", lambda: recorder)
    code, captured = invoke(monkeypatch, capsys, "alert")
    (text,) = recorder.sent
    assert "HEALTH_CHECK_FAILED" in text and "CRITICAL" in text
    assert "secret" not in text and "secret" not in captured.out


def test_the_alert_pass_refuses_without_a_transport(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=True), database=facts())
    code, captured = invoke(monkeypatch, capsys, "alert")
    document = json.loads(captured.out)
    assert code == 2 and document["transport_refusal"] == "ALERT_TRANSPORT_NOT_CONFIGURED"


def test_a_dry_run_alert_needs_no_transport_and_sends_nothing(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=False), database=facts(positions=1))
    code, captured = invoke(monkeypatch, capsys, "alert", "--dry-run")
    document = json.loads(captured.out)
    assert code == 0 and document["sent"] is False
    assert "RH-AGENTS ALERT: CRITICAL" in document["dry_run_text"]


def test_the_heartbeat_command_prints_one_signed_line(world, monkeypatch, capsys, tmp_path):
    key = tmp_path / "key"
    key.write_text(bytes(range(32)).hex())
    key.chmod(0o600)
    monkeypatch.setenv("RH_AGENTS_HEARTBEAT_KEY_FILE", str(key))
    world.update(service=ServiceStatus(loaded=True), database=facts())
    code, captured = invoke(monkeypatch, capsys, "heartbeat")
    assert code == 0
    (line,) = captured.out.splitlines()
    heartbeat = verify(
        line,
        bytes(range(32)),
        expected_host="rh-vps",
        now=datetime.now(UTC),
        last_sent_at=None,
    )
    # The fixture's evidence is from T0, long stale by now: the verdict is passed on.
    assert heartbeat.status == "DEGRADED"


def test_the_heartbeat_command_refuses_without_a_key(world, monkeypatch, capsys):
    monkeypatch.delenv("RH_AGENTS_HEARTBEAT_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    world.update(service=ServiceStatus(loaded=True), database=facts())
    code, captured = invoke(monkeypatch, capsys, "heartbeat")
    assert code == 2 and captured.out == ""
    assert "HEARTBEAT_KEY_NOT_CONFIGURED" in captured.err


def test_the_alerts_stop_is_an_audited_action(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=False), database=facts())
    code, captured = invoke(
        monkeypatch, capsys, "audit", "--action", "alerts-stop", "--reason", "MAINTENANCE"
    )
    assert code == 0 and json.loads(captured.out)["action"] == "alerts-stop"
