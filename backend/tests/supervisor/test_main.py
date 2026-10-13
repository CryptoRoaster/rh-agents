"""The supervisor's command line: exit codes, documents, and the audit log.

`gather` is replaced, so nothing here asks launchd or a database anything.
"""

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.jobruns.models import Coverage, EvidenceRow, ExitLivenessGate
from src.supervisor import main as cli
from src.supervisor.models import DatabaseFacts, ServiceStatus, SupervisorState

GATE = ExitLivenessGate(max_age_seconds=180, run_max_seconds=60)


def database(*, age: float, positions: int = 0, **flags) -> DatabaseFacts:
    now = datetime.now(UTC)
    finished = now - timedelta(seconds=age)
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


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR", str(tmp_path))
    found = {}

    def gather(now):
        state = SupervisorState(last_tick_started_at=now - timedelta(seconds=10))
        return found["service"], state, True, found["database"], GATE

    monkeypatch.setattr(cli, "gather", gather)
    return found


def invoke(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["supervisor", *argv])
    code = cli.main()
    return code, json.loads(capsys.readouterr().out)


def test_health_is_critical_with_a_held_position_and_stale_evidence(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=True), database=database(age=600, positions=1))
    code, document = invoke(monkeypatch, capsys, "health")
    assert code == 2 and document["status"] == "CRITICAL"
    assert document["open_positions"] == 1
    assert document["positions"] == {"normal": 0, "early": 1, "unknown": 0}
    assert document["exit_liveness"] == "EXIT_JOB_TOO_OLD"
    assert document["supervisor"]["consecutive_failures"] == 0


def test_health_is_healthy_and_says_ready(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=True, last_exit_status=0), database=database(age=20))
    code, document = invoke(monkeypatch, capsys, "health")
    assert code == 0 and document["status"] == "HEALTHY"
    assert document["exit_liveness"] == "READY"


def test_health_is_stopped_when_idle(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=False), database=database(age=4000))
    code, document = invoke(monkeypatch, capsys, "health")
    assert code == 3 and document["status"] == "STOPPED"


def test_an_unreadable_database_is_critical(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=True), database=None)
    code, document = invoke(monkeypatch, capsys, "health")
    assert code == 2 and document["open_positions"] is None


def test_stop_check_refuses_with_a_held_position(world, monkeypatch, capsys):
    world.update(
        service=ServiceStatus(loaded=True),
        database=database(age=20, positions=1, entry_halt_manual=True),
    )
    code, document = invoke(monkeypatch, capsys, "stop-check")
    assert code == 2 and not document["allowed"]
    assert document["refusals"] == ["STOP_REFUSED_OPEN_POSITIONS"]


def test_stop_check_allows_an_empty_halted_book(world, monkeypatch, capsys):
    world.update(
        service=ServiceStatus(loaded=True), database=database(age=20, entry_halt_manual=True)
    )
    code, document = invoke(monkeypatch, capsys, "stop-check")
    assert code == 0 and document["allowed"]


def test_deploy_check_refuses_while_loaded(world, monkeypatch, capsys):
    world.update(service=ServiceStatus(loaded=True), database=database(age=20))
    code, document = invoke(monkeypatch, capsys, "deploy-check")
    assert code == 2 and document["refusals"] == ["DEPLOY_REFUSED_SUPERVISOR_LOADED"]


def test_audit_needs_a_reason_code(world, monkeypatch, capsys, tmp_path):
    world.update(service=ServiceStatus(loaded=False), database=database(age=20))
    code, document = invoke(monkeypatch, capsys, "audit", "--action", "stop", "--reason", "x y")
    assert code == 2 and document["refusal"] == "REASON_REQUIRED"
    assert not (tmp_path / "audit.log").exists()


def test_audit_records_what_the_guard_saw(world, monkeypatch, capsys, tmp_path):
    world.update(service=ServiceStatus(loaded=True), database=database(age=20, positions=1))
    code, _ = invoke(
        monkeypatch, capsys, "audit", "--action", "emergency-stop", "--reason", "HOST_FAILING"
    )
    assert code == 0
    entry = json.loads((tmp_path / "audit.log").read_text())
    assert entry["action"] == "emergency-stop" and entry["reason"] == "HOST_FAILING"
    assert entry["open_positions"] == 1 and entry["status"] == "HEALTHY"


def test_tick_exit_codes(monkeypatch, capsys, tmp_path):
    from src.supervisor.models import Category, TickResult

    monkeypatch.setenv("RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR", str(tmp_path))
    for result, expected in (
        (TickResult("EXIT_JOB_READY", Category.OK, 0), 0),
        (TickResult("EXIT_JOB_ALREADY_RUNNING", Category.SKIPPED, 0), 0),
        (TickResult("EXIT_JOB_NOT_READY", Category.PROVIDER, 0), 1),
        (TickResult("SUPERVISOR_SETTINGS_INVALID", Category.CONFIGURATION, None), 2),
    ):
        monkeypatch.setattr(cli, "run_tick", lambda directory, load_settings, r=result: r)
        code, document = invoke(monkeypatch, capsys, "tick")
        assert code == expected and document["code"] == result.code


def test_a_crashing_health_check_reads_as_critical(monkeypatch, capsys):
    def broken(now):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "gather", broken)
    code, document = invoke(monkeypatch, capsys, "health")
    assert code == 2 and document["status"] == "CRITICAL"
    assert document["reasons"] == ["HEALTH_CHECK_FAILED"]
