"""The supervisor's local state file and audit log.

One small JSON document, rewritten atomically by each tick (temporary file,
fsync, rename) with mode 0600. It is the tick's heartbeat and failure counter —
never evidence a BUY rests on: that is `paper_job_runs`, in the database.

The audit log is append-only JSON lines: who started or stopped the service,
when, and with what the guard saw. Codes, counts and instants only.
"""

import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.supervisor.models import MAX_WARNINGS, STATE_VERSION, SupervisorState, TickResult

STATE_DIR_ENV = "RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR"
STATE_FILE = "state.json"
AUDIT_FILE = "audit.log"


def state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "state" / "rh-agents" / "exit-supervisor"


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("An instant must be an ISO string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("An instant must carry its offset")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


class StateUnreadable(Exception):
    """The state file exists and is not one this code wrote."""


def load_state(directory: Path) -> SupervisorState | None:
    """The recorded state; `None` when no tick ever wrote one."""
    path = directory / STATE_FILE
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateUnreadable from error
    try:
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != STATE_VERSION:
            raise ValueError("Unknown state version")
        failures = document.get("consecutive_failures", 0)
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
            raise ValueError("Failures must be a count")
        warnings = document.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise ValueError("Warnings must be codes")
        gap = document.get("last_gap_seconds")
        if gap is not None and (not isinstance(gap, int) or isinstance(gap, bool)):
            raise ValueError("A gap must be whole seconds")
        boot_id = document.get("boot_id")
        if boot_id is not None and not isinstance(boot_id, str):
            raise ValueError("A boot identity must be a string")
        last_result = document.get("last_result")
        last_failure = document.get("last_failure")
        for item in (last_result, last_failure):
            if item is not None and not isinstance(item, dict):
                raise ValueError("A result must be an object")
        return SupervisorState(
            last_tick_started_at=_instant(document.get("last_tick_started_at")),
            last_tick_finished_at=_instant(document.get("last_tick_finished_at")),
            last_success_at=_instant(document.get("last_success_at")),
            consecutive_failures=failures,
            last_result=last_result,
            last_failure=last_failure,
            in_progress_since=_instant(document.get("in_progress_since")),
            last_gap_seconds=gap,
            warnings=tuple(warnings[:MAX_WARNINGS]),
            boot_id=boot_id,
            host_restarted_at=_instant(document.get("host_restarted_at")),
        )
    except (ValueError, TypeError) as error:
        raise StateUnreadable from error


def _document(state: SupervisorState) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_tick_started_at": _iso(state.last_tick_started_at),
        "last_tick_finished_at": _iso(state.last_tick_finished_at),
        "last_success_at": _iso(state.last_success_at),
        "consecutive_failures": state.consecutive_failures,
        "last_result": state.last_result,
        "last_failure": state.last_failure,
        "in_progress_since": _iso(state.in_progress_since),
        "last_gap_seconds": state.last_gap_seconds,
        "warnings": list(state.warnings[:MAX_WARNINGS]),
        "boot_id": state.boot_id,
        "host_restarted_at": _iso(state.host_restarted_at),
    }


def save_state(directory: Path, state: SupervisorState) -> None:
    """Replace the state file atomically, readable by its owner only."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(_document(state), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / STATE_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def record_result(
    state: SupervisorState, result: TickResult, *, finished_at: datetime
) -> SupervisorState:
    """The state after one finished tick: a success resets the count, a skip changes nothing."""
    summary = result.as_dict() | {"at": _iso(finished_at)}
    if result.ok:
        return replace(
            state,
            last_tick_finished_at=finished_at,
            last_success_at=finished_at,
            consecutive_failures=0,
            last_result=summary,
            in_progress_since=None,
        )
    if result.skipped:
        return replace(
            state,
            last_tick_finished_at=finished_at,
            last_result=summary,
            in_progress_since=None,
        )
    return replace(
        state,
        last_tick_finished_at=finished_at,
        consecutive_failures=state.consecutive_failures + 1,
        last_result=summary,
        last_failure=summary,
        in_progress_since=None,
    )


def append_audit(directory: Path, entry: dict[str, Any]) -> None:
    """Append one audit line, fsynced, readable by its owner only."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / AUDIT_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(entry, sort_keys=True, default=str) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "STATE_DIR_ENV",
    "StateUnreadable",
    "append_audit",
    "load_state",
    "record_result",
    "save_state",
    "state_dir",
]
