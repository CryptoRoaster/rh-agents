"""The supervisor's pure judgements: what a child was, how healthy it all is, who may stop."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.jobruns.models import Coverage, EvidenceRow, ExitLivenessGate
from src.supervisor.models import (
    CHILD_HARD_TIMEOUT_SECONDS,
    EXIT_COMMAND,
    EXIT_RUN_MAX_SECONDS,
    INTERVAL_SECONDS,
    LIVENESS_MAX_AGE_SECONDS,
    Category,
    DatabaseFacts,
    HealthExit,
    HealthStatus,
    ServiceStatus,
    SupervisorState,
    TickCode,
    classify_child,
    deploy_refusals,
    evaluate_health,
    settings_refusal,
    stop_refusals,
    tick_gap,
)

NOW = datetime(2026, 10, 13, 18, tzinfo=UTC)
GATE = ExitLivenessGate(max_age_seconds=180, run_max_seconds=60)
RUNNING = ServiceStatus(loaded=True, running=False, last_exit_status=0)
UNLOADED = ServiceStatus(loaded=False)


def summary(**overrides):
    document = {
        "kind": "paper_run_summary",
        "mode": "EXITS_ONLY",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "stop": "NOTHING_LEFT_TO_DO",
        "lock": "ACQUIRED",
        "exit_ready": True,
        "exit_ready_reason": None,
        "errors": [],
    }
    document.update(overrides)
    return json.dumps(document)


def evidence(age_seconds: float, *, ready: bool = True, policies=("EARLY", "NORMAL")):
    finished = NOW - timedelta(seconds=age_seconds)
    return EvidenceRow(
        outcome="SUCCEEDED",
        started_at=finished - timedelta(seconds=5),
        finished_at=finished,
        exit_ready=ready,
        exit_ready_reason=None if ready else "EXIT_ACQUISITION_INCOMPLETE",
        policy_classes=list(policies),
    )


def facts(*, age=30.0, normal=0, early=0, unknown=0, rows=None, **flags):
    return DatabaseFacts(
        evidence=[evidence(age)] if rows is None else rows,
        coverage=Coverage(normal=normal, early=early, unknown=unknown),
        last_ready_finished_at=NOW - timedelta(seconds=age),
        **flags,
    )


def fresh_state(**overrides):
    values = dict(last_tick_started_at=NOW - timedelta(seconds=20), consecutive_failures=0)
    values.update(overrides)
    return SupervisorState(**values)


def health(database, service=RUNNING, state=None, readable=True):
    return evaluate_health(
        now=NOW,
        service=service,
        state=fresh_state() if state is None else state,
        state_readable=readable,
        database=database,
        gate=GATE,
    )


# -- the rhythm --------------------------------------------------------------


def test_the_rhythm_fits_the_liveness_window():
    assert INTERVAL_SECONDS == 60
    assert EXIT_RUN_MAX_SECONDS == 60
    assert LIVENESS_MAX_AGE_SECONDS == 180
    # One interval plus one full run must fit, and the hard kill must come
    # before the gate stops passing an unfinished run over (run bound + 30 s).
    assert INTERVAL_SECONDS + EXIT_RUN_MAX_SECONDS < LIVENESS_MAX_AGE_SECONDS
    assert EXIT_RUN_MAX_SECONDS < CHILD_HARD_TIMEOUT_SECONDS < EXIT_RUN_MAX_SECONDS + 30


def test_the_only_command_is_the_exit_job():
    assert EXIT_COMMAND == ("-m", "src.runner.main", "--exits-once")


def test_a_longer_exit_run_or_a_shorter_window_is_refused():
    assert settings_refusal(60, 180) is None
    assert settings_refusal(61, 180) is TickCode.RUN_BOUND_EXCEEDED
    assert settings_refusal(60, 119) is TickCode.LIVENESS_WINDOW_TOO_SHORT


# -- one child ---------------------------------------------------------------


def test_only_a_ready_exit_summary_is_a_success():
    result = classify_child(0, summary(), timed_out=False)
    assert result.ok and result.code == "EXIT_JOB_READY"


def test_the_last_json_line_counts_and_noise_before_it_does_not():
    result = classify_child(0, "warming up\n" + summary(), timed_out=False)
    assert result.ok


def test_a_hard_timeout_is_a_failure_whatever_was_printed():
    result = classify_child(None, summary(), timed_out=True)
    assert not result.ok and result.category is Category.TIMEOUT


@pytest.mark.parametrize("code", [None, -9, -15])
def test_a_child_killed_by_a_signal_crashed(code):
    result = classify_child(code, "", timed_out=False)
    assert result.code == "EXIT_JOB_CRASHED" and result.category is Category.CRASH


def test_unreadable_output_is_a_failure():
    result = classify_child(0, "not json at all", timed_out=False)
    assert result.code == "EXIT_JOB_OUTPUT_UNREADABLE" and not result.ok


def test_a_summary_of_another_mode_is_never_taken_for_the_exit_job():
    result = classify_child(0, summary(mode="FULL"), timed_out=False)
    assert not result.ok and result.code == "EXIT_JOB_OUTPUT_UNREADABLE"


def test_a_database_outage_is_named():
    startup = json.dumps({"kind": "run_technical_failure", "reason": "RUN_STARTUP_FAILED"})
    assert classify_child(1, startup, timed_out=False).category is Category.DATABASE
    during = summary(errors=["DATABASE_UNAVAILABLE"], exit_ready=False)
    result = classify_child(1, during, timed_out=False)
    assert result.code == "DATABASE_UNAVAILABLE" and result.category is Category.DATABASE


def test_a_provider_outage_is_named():
    result = classify_child(
        0,
        summary(exit_ready=False, exit_ready_reason="EXIT_ACQUISITION_INCOMPLETE"),
        timed_out=False,
    )
    assert result.code == "EXIT_JOB_NOT_READY" and result.category is Category.PROVIDER


def test_a_failed_exit_attempt_reads_as_execution_or_rpc():
    result = classify_child(
        0, summary(exit_ready=False, exit_ready_reason="EXIT_ATTEMPT_FAILED"), timed_out=False
    )
    assert result.category is Category.EXECUTION_OR_RPC


def test_a_refused_configuration_is_its_own_category():
    refused = json.dumps({"kind": "run_configuration_refused", "reason": "PAPER_RUNNER_DISABLED"})
    result = classify_child(2, refused, timed_out=False)
    assert result.category is Category.CONFIGURATION
    assert result.errors == ("PAPER_RUNNER_DISABLED",)


def test_a_held_exit_lock_is_neither_success_nor_failure():
    result = classify_child(0, summary(lock="ALREADY_RUNNING", exit_ready=None), timed_out=False)
    assert result.skipped and not result.ok


def test_nothing_but_codes_survives_from_the_child():
    noisy = summary(
        exit_ready=False,
        exit_ready_reason="postgresql://user:pw@host/db",
        errors=["https://rpc.example/key", "DATABASE_UNAVAILABLE"],
    )
    result = classify_child(1, noisy, timed_out=False)
    assert result.exit_ready_reason is None
    assert result.errors == ("DATABASE_UNAVAILABLE",)
    assert "pw" not in json.dumps(result.as_dict())


# -- health ------------------------------------------------------------------


def test_healthy_needs_everything():
    report = health(facts())
    assert report.status is HealthStatus.HEALTHY and report.reasons == ()
    assert report.exit_code is HealthExit.HEALTHY


def test_open_position_and_stale_exit_is_critical():
    report = health(facts(age=181, normal=1))
    assert report.status is HealthStatus.CRITICAL
    assert "OPEN_POSITIONS_EXIT_STALE:EXIT_JOB_TOO_OLD" in report.reasons
    assert report.exit_code is HealthExit.CRITICAL


def test_open_position_with_the_supervisor_not_running_is_critical():
    report = health(facts(normal=1), service=UNLOADED)
    assert report.status is HealthStatus.CRITICAL
    assert "OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING" in report.reasons


def test_an_unreadable_database_is_critical_because_positions_are_unknown():
    report = health(None)
    assert report.status is HealthStatus.CRITICAL
    assert report.open_positions is None


def test_an_early_position_needs_evidence_for_the_early_policy():
    rows = [evidence(30, policies=("NORMAL",))]
    report = health(facts(early=1, rows=rows))
    assert report.status is HealthStatus.CRITICAL
    assert "OPEN_POSITIONS_POLICY_NOT_COVERED:EARLY" in report.reasons


def test_a_position_no_exit_policy_closes_is_critical():
    assert health(facts(unknown=1)).status is HealthStatus.CRITICAL


def test_no_position_and_no_service_is_the_idle_stopped_state():
    report = health(facts(age=4000), service=UNLOADED)
    assert report.status is HealthStatus.STOPPED
    assert report.exit_code is HealthExit.STOPPED


def test_no_position_and_stale_exit_is_degraded_with_entries_blocked():
    report = health(facts(age=200))
    assert report.status is HealthStatus.DEGRADED
    assert "EXIT_STALE_ENTRIES_BLOCKED:EXIT_JOB_TOO_OLD" in report.reasons


def test_failures_never_read_green_even_while_evidence_is_fresh():
    report = health(facts(normal=1), state=fresh_state(consecutive_failures=2))
    assert report.status is HealthStatus.DEGRADED
    assert "CONSECUTIVE_FAILURES:2" in report.reasons


def test_a_failing_last_exit_status_is_degraded():
    service = ServiceStatus(loaded=True, last_exit_status=1)
    assert health(facts(), service=service).status is HealthStatus.DEGRADED


def test_a_silent_supervisor_is_noticed_by_its_heartbeat():
    stale = fresh_state(last_tick_started_at=NOW - timedelta(seconds=400))
    assert health(facts(), state=stale).status is HealthStatus.DEGRADED
    report = health(facts(normal=1), state=stale)
    assert report.status is HealthStatus.CRITICAL
    assert "SUPERVISOR_HEARTBEAT_STALE" in report.reasons


def test_a_tick_that_died_mid_run_is_reported():
    died = fresh_state(in_progress_since=NOW - timedelta(seconds=600))
    report = health(facts(), state=died)
    assert "SUPERVISOR_TICK_ABANDONED" in report.reasons


def test_a_missing_or_unreadable_state_file_is_not_healthy():
    missing = evaluate_health(
        now=NOW, service=RUNNING, state=None, state_readable=True, database=facts(), gate=GATE
    )
    assert "SUPERVISOR_STATE_MISSING" in missing.reasons
    unreadable = health(facts(), readable=False)
    assert unreadable.status is HealthStatus.DEGRADED


def test_an_unknown_service_status_is_never_healthy():
    report = health(facts(), service=ServiceStatus(loaded=None))
    assert report.status is HealthStatus.DEGRADED
    assert "SERVICE_STATUS_UNKNOWN" in report.reasons


def test_after_sleep_the_position_is_critical_until_a_tick_runs_again():
    """Host slept for ten minutes: the last evidence predates it, the heartbeat too."""
    slept = fresh_state(last_tick_started_at=NOW - timedelta(seconds=620))
    report = health(facts(age=600, normal=1), state=slept)
    assert report.status is HealthStatus.CRITICAL
    woke = fresh_state(warnings=("TICK_GAP_DETECTED",))
    after = health(facts(age=10, normal=1), state=woke)
    assert after.status is HealthStatus.DEGRADED and "TICK_GAP_DETECTED" in after.reasons


def test_after_a_reboot_without_login_nothing_ticks_and_a_position_is_critical():
    report = health(
        facts(age=3600, normal=1),
        service=UNLOADED,
        state=fresh_state(last_tick_started_at=NOW - timedelta(hours=1)),
    )
    assert report.status is HealthStatus.CRITICAL


# -- stop and deploy guards ----------------------------------------------------


def test_a_regular_stop_is_refused_with_an_open_position():
    refusals = stop_refusals(facts(normal=1, entry_halt_manual=True), tick_in_progress=False)
    assert refusals == ["STOP_REFUSED_OPEN_POSITIONS"]


def test_a_regular_stop_needs_entries_durably_blocked():
    assert stop_refusals(facts(), tick_in_progress=False) == ["STOP_REFUSED_ENTRY_NOT_BLOCKED"]
    for flag in ("entry_halt_manual", "entry_halt_auto", "paused"):
        assert stop_refusals(facts(**{flag: True}), tick_in_progress=False) == []


def test_a_regular_stop_never_cuts_an_exit_or_entry_run_short():
    blocked = {"entry_halt_manual": True}
    assert "STOP_REFUSED_EXIT_RUN_ACTIVE" in stop_refusals(
        facts(exit_run_active=True, **blocked), tick_in_progress=False
    )
    assert "STOP_REFUSED_EXIT_RUN_ACTIVE" in stop_refusals(facts(**blocked), tick_in_progress=True)
    assert "STOP_REFUSED_ENTRY_RUN_ACTIVE" in stop_refusals(
        facts(entry_run_active=True, **blocked), tick_in_progress=False
    )


def test_a_regular_stop_is_refused_when_positions_are_unknown():
    assert stop_refusals(None, tick_in_progress=False) == ["STOP_REFUSED_POSITIONS_UNKNOWN"]


def test_a_deploy_waits_for_a_stopped_supervisor_and_an_empty_book():
    assert deploy_refusals(facts(), UNLOADED) == []
    assert deploy_refusals(facts(), RUNNING) == ["DEPLOY_REFUSED_SUPERVISOR_LOADED"]
    assert "DEPLOY_REFUSED_OPEN_POSITIONS" in deploy_refusals(facts(normal=1), UNLOADED)
    assert "DEPLOY_REFUSED_EXIT_RUN_ACTIVE" in deploy_refusals(
        facts(exit_run_active=True), UNLOADED
    )
    assert "DEPLOY_REFUSED_POSITIONS_UNKNOWN" in deploy_refusals(None, UNLOADED)
    assert "DEPLOY_REFUSED_SERVICE_STATUS_UNKNOWN" in deploy_refusals(
        facts(), ServiceStatus(loaded=None)
    )


def test_missed_ticks_are_measured():
    assert tick_gap(None, NOW) is None
    assert tick_gap(NOW - timedelta(seconds=61), NOW) is None
    assert tick_gap(NOW - timedelta(seconds=900), NOW) == 900


def test_a_run_id_is_kept_only_if_it_is_one():
    assert classify_child(0, summary(run_id="not-a-uuid https://x"), timed_out=False).run_id is None
    kept = classify_child(0, summary(), timed_out=False).run_id
    assert kept == "00000000-0000-0000-0000-000000000001"
