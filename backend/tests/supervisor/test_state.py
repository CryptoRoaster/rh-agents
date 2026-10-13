"""The supervisor's state file and audit log."""

import json
import stat
from datetime import UTC, datetime

import pytest

from src.supervisor.models import Category, SupervisorState, TickResult
from src.supervisor.state import (
    StateUnreadable,
    append_audit,
    load_state,
    record_result,
    save_state,
)

AT = datetime(2026, 10, 13, 18, tzinfo=UTC)


def test_no_file_is_no_state(tmp_path):
    assert load_state(tmp_path) is None


def test_a_round_trip_keeps_every_field(tmp_path):
    state = SupervisorState(
        last_tick_started_at=AT,
        last_tick_finished_at=AT,
        last_success_at=AT,
        consecutive_failures=3,
        last_result={"code": "EXIT_JOB_READY"},
        last_failure={"code": "DATABASE_UNAVAILABLE"},
        in_progress_since=None,
        last_gap_seconds=900,
        warnings=("TICK_GAP_DETECTED",),
    )
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".state.*"))


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        json.dumps({"version": 99}),
        json.dumps({"version": 1, "consecutive_failures": -1}),
        json.dumps({"version": 1, "last_tick_started_at": "2026-10-13T18:00:00"}),
        json.dumps({"version": 1, "warnings": "nope"}),
    ],
)
def test_a_state_file_this_code_did_not_write_is_unreadable(tmp_path, content):
    (tmp_path / "state.json").write_text(content)
    with pytest.raises(StateUnreadable):
        load_state(tmp_path)


def test_a_skip_changes_neither_count_nor_last_success():
    before = SupervisorState(consecutive_failures=2, last_success_at=AT)
    after = record_result(
        before, TickResult("EXIT_JOB_ALREADY_RUNNING", Category.SKIPPED, 0), finished_at=AT
    )
    assert after.consecutive_failures == 2 and after.last_success_at == AT


def test_the_audit_log_appends_privately(tmp_path):
    append_audit(tmp_path, {"action": "start", "reason": "A"})
    append_audit(tmp_path, {"action": "stop", "reason": "B"})
    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert [json.loads(line)["action"] for line in lines] == ["start", "stop"]
    assert stat.S_IMODE((tmp_path / "audit.log").stat().st_mode) == 0o600
