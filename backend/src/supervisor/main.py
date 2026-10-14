"""The exit supervisor's command line. One action per call.

    python -m src.supervisor.main tick          one supervised `--exits-once` (launchd)
    python -m src.supervisor.main health        read-only verdict: HEALTHY, DEGRADED,
                                                CRITICAL or STOPPED
    python -m src.supervisor.main stop-check    may a regular stop happen now?
    python -m src.supervisor.main deploy-check  may a code deploy happen now?
    python -m src.supervisor.main audit --action ACTION --reason CODE [--actor REF]
    python -m src.supervisor.main alert [--dry-run]  one alert pass on the health verdict
    python -m src.supervisor.main heartbeat     one signed heartbeat line on stdout

`tick` is the only action that starts anything, and what it starts is the exit
job and nothing else. `health`, `stop-check` and `deploy-check` read launchd,
the state file and the database and write nothing. `audit` appends one line
to the supervisor's local audit log; the operator scripts call it for every
start and stop, the emergency stop included. `alert` judges health the same
read-only way and hands news to the configured transport (`alerts.py`);
`heartbeat` prints one signed line for the dead-man receiver (`deadman.py`).

Exit codes
----------
``tick``          ``0`` exit job ready (or another exit run held the lock),
                  ``1`` the exit job failed, ``2`` the configuration was refused.
``health``        ``0`` HEALTHY, ``1`` DEGRADED, ``2`` CRITICAL, ``3`` STOPPED.
``*-check``       ``0`` allowed, ``2`` refused (the refusals say why).
``audit``         ``0`` recorded, ``2`` invalid argument.
``alert``         ``0`` nothing to send or delivered, ``1`` delivery failed,
                  ``2`` no usable transport configured.
``heartbeat``     ``0`` printed, ``2`` no usable key.

Output is one JSON object of codes, counts and instants — never a secret, a
connection string or a provider message.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.data.database import connect
from src.orchestration.entryhalt.models import REASON_PATTERN
from src.orchestration.jobruns.models import ExitLivenessGate
from src.supervisor.alerts import (
    HOST_ALIAS_ENV,
    AlertPassBusy,
    Observation,
    TransportUnavailable,
    observe,
    run_alert_pass,
    transport_from_environment,
)
from src.supervisor.deadman import key_path, load_key, sign
from src.supervisor.models import (
    LABEL,
    TICK_ABANDONED_SECONDS,
    Category,
    DatabaseFacts,
    HealthExit,
    HealthReport,
    ServiceStatus,
    SupervisorState,
    deploy_refusals,
    evaluate_health,
    stop_refusals,
)
from src.supervisor.probe import database_facts, service_status
from src.supervisor.state import StateUnreadable, append_audit, load_state, state_dir
from src.supervisor.tick import run_tick

REFUSED = 2
ALERT_STATE_DIR_ENV = "RH_AGENTS_ALERT_STATE_DIR"
DATABASE_SECONDS = 15.0
AUDIT_ACTIONS = ("start", "stop", "emergency-stop", "alerts-stop")


def _settings() -> Settings:
    return Settings()


def _iso(value: Any) -> Any:
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


async def _read_database(settings: Settings, now: datetime) -> DatabaseFacts:
    engine, sessions = connect(settings.database_url)
    try:
        return await asyncio.wait_for(
            database_facts(
                sessions,
                now=now,
                exit_run_max_seconds=settings.paper_exit_run_max_seconds,
                entry_run_max_seconds=settings.paper_runner_max_seconds,
            ),
            timeout=DATABASE_SECONDS,
        )
    finally:
        await engine.dispose()


def gather(
    now: datetime,
) -> tuple[ServiceStatus, SupervisorState | None, bool, DatabaseFacts | None, ExitLivenessGate]:
    """Everything the read-only actions judge. Whatever cannot be read is `None`."""
    service = service_status()
    try:
        state = load_state(state_dir())
        readable = True
    except StateUnreadable:
        state, readable = None, False
    database: DatabaseFacts | None = None
    try:
        settings = _settings()
    except ValidationError:
        gate = ExitLivenessGate(max_age_seconds=180, run_max_seconds=60)
    else:
        gate = ExitLivenessGate(
            max_age_seconds=settings.paper_exit_liveness_max_age_seconds,
            run_max_seconds=settings.paper_exit_run_max_seconds,
        )
        try:
            database = asyncio.run(_read_database(settings, now))
        except (SQLAlchemyError, OSError, ValueError, TimeoutError):
            database = None
    return service, state, readable, database, gate


def health_document(report: HealthReport) -> dict[str, Any]:
    database = report.database
    state = report.state
    return {
        "kind": "exit_supervisor_health",
        "status": report.status.value,
        "reasons": list(report.reasons),
        "checked_at": _iso(report.checked_at),
        "service": {
            "label": LABEL,
            "loaded": report.service.loaded,
            "running": report.service.running,
            "last_exit_status": report.service.last_exit_status,
        },
        "open_positions": report.open_positions,
        "positions": None
        if database is None
        else {
            "normal": database.coverage.normal,
            "early": database.coverage.early,
            "unknown": database.coverage.unknown,
        },
        "exit_liveness": None
        if database is None
        else ("READY" if report.liveness is None else report.liveness),
        "last_ready_exit_run_finished_at": None
        if database is None
        else _iso(database.last_ready_finished_at),
        "exit_liveness_age_seconds": report.liveness_age_seconds,
        "last_exit_run": None
        if database is None or database.last_exit_run is None
        else {key: _iso(value) for key, value in database.last_exit_run.items()},
        "entry_blocked": None
        if database is None
        else {
            "manual_halt": database.entry_halt_manual,
            "auto_halt": database.entry_halt_auto,
            "paused": database.paused,
        },
        "supervisor": None
        if state is None
        else {
            "last_tick_started_at": _iso(state.last_tick_started_at),
            "last_tick_finished_at": _iso(state.last_tick_finished_at),
            "last_success_at": _iso(state.last_success_at),
            "consecutive_failures": state.consecutive_failures,
            "last_result": state.last_result,
            "last_failure": state.last_failure,
            "in_progress_since": _iso(state.in_progress_since),
            "last_gap_seconds": state.last_gap_seconds,
            "warnings": list(state.warnings),
            "host_restarted_at": _iso(state.host_restarted_at),
        },
    }


def _tick_in_progress(state: SupervisorState | None, service: ServiceStatus, now: datetime) -> bool:
    if service.running:
        return True
    if state is None or state.in_progress_since is None:
        return False
    return (now - state.in_progress_since).total_seconds() <= TICK_ABANDONED_SECONDS


def _print(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, default=str))


def _health() -> int:
    """Never anything but one of the four codes: a crash here reads as CRITICAL."""
    try:
        return _evaluated_health()
    except Exception:
        _print(
            {
                "kind": "exit_supervisor_health",
                "status": "CRITICAL",
                "reasons": ["HEALTH_CHECK_FAILED"],
            }
        )
        return int(HealthExit.CRITICAL)


def _evaluated_health() -> int:
    now = datetime.now(UTC)
    service, state, readable, database, gate = gather(now)
    report = evaluate_health(
        now=now,
        service=service,
        state=state,
        state_readable=readable,
        database=database,
        gate=gate,
    )
    _print(health_document(report))
    return int(report.exit_code)


def _check(kind: str) -> int:
    now = datetime.now(UTC)
    service, state, _, database, _ = gather(now)
    if kind == "stop":
        refusals = stop_refusals(database, tick_in_progress=_tick_in_progress(state, service, now))
    else:
        refusals = deploy_refusals(database, service)
    _print(
        {
            "kind": f"exit_supervisor_{kind}_check",
            "allowed": not refusals,
            "refusals": refusals,
            "open_positions": None if database is None else database.coverage.total,
            "service_loaded": service.loaded,
            "checked_at": _iso(now),
        }
    )
    return 0 if not refusals else REFUSED


def _audit(action: str, reason: str | None, actor: str | None) -> int:
    if reason is None or not REASON_PATTERN.match(reason):
        _print({"kind": "exit_supervisor_audit", "refusal": "REASON_REQUIRED"})
        return REFUSED
    if actor is not None and not REASON_PATTERN.match(actor.upper().replace("-", "_")):
        _print({"kind": "exit_supervisor_audit", "refusal": "INVALID_ACTOR"})
        return REFUSED
    now = datetime.now(UTC)
    service, state, readable, database, gate = gather(now)
    report = evaluate_health(
        now=now,
        service=service,
        state=state,
        state_readable=readable,
        database=database,
        gate=gate,
    )
    entry = {
        "at": _iso(now),
        "action": action,
        "reason": reason,
        "actor": actor or os.environ.get("USER", "UNKNOWN"),
        "status": report.status.value,
        "reasons": list(report.reasons),
        "open_positions": report.open_positions,
        "service_loaded": service.loaded,
    }
    append_audit(state_dir(), entry)
    _print({"kind": "exit_supervisor_audit", "recorded": True, **entry})
    return 0


def _health_observation(now: datetime) -> Observation:
    """The current verdict as an alert sees it. A crash of the check is CRITICAL."""
    try:
        service, state, readable, database, gate = gather(now)
        report = evaluate_health(
            now=now,
            service=service,
            state=state,
            state_readable=readable,
            database=database,
            gate=gate,
        )
        return observe(health_document(report), now=now)
    except Exception:
        return Observation("CRITICAL", ("HEALTH_CHECK_FAILED",), None, now)


def alert_state_dir() -> Path:
    configured = os.environ.get(ALERT_STATE_DIR_ENV, "").strip()
    return Path(configured) if configured else state_dir().parent / "alerts"


def _alert(dry_run: bool) -> int:
    now = datetime.now(UTC)
    observation = _health_observation(now)
    refusal: str | None = None
    try:
        transport = None if dry_run else transport_from_environment()
    except TransportUnavailable as unavailable:
        transport, refusal = None, unavailable.code
    try:
        outcome = run_alert_pass(
            alert_state_dir(),
            observation,
            transport=transport,
            host=os.environ.get(HOST_ALIAS_ENV, ""),
            dry_run=dry_run,
        )
    except AlertPassBusy:
        _print({"kind": "exit_supervisor_alert", "skipped": "ALERT_PASS_ALREADY_RUNNING"})
        return 0
    _print({"kind": "exit_supervisor_alert", "transport_refusal": refusal, **outcome.as_dict()})
    if refusal is not None and not dry_run:
        return REFUSED
    return 1 if outcome.failure is not None else 0


def _heartbeat() -> int:
    path = key_path()
    try:
        if path is None:
            raise TransportUnavailable("HEARTBEAT_KEY_NOT_CONFIGURED")
        key = load_key(path)
    except TransportUnavailable as unavailable:
        # stdout carries the heartbeat alone; a refusal goes to stderr.
        print(
            json.dumps({"kind": "exit_supervisor_heartbeat", "refusal": unavailable.code}),
            file=sys.stderr,
        )
        return REFUSED
    now = datetime.now(UTC)
    observation = _health_observation(now)
    print(sign(key, host=os.environ.get(HOST_ALIAS_ENV, ""), status=observation.status, now=now))
    return 0


def _terminated(signum: int, frame: Any) -> None:
    # launchd stops a job with SIGTERM. Raised as an exit, so the tick's child —
    # in its own process group — is killed on the way out, not orphaned.
    raise SystemExit(128 + signum)


def _tick() -> int:
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        result = run_tick(state_dir(), load_settings=_settings)
    finally:
        signal.signal(signal.SIGTERM, previous)
    _print({"kind": "exit_supervisor_tick", **result.as_dict()})
    if result.ok or result.skipped:
        return 0
    return REFUSED if result.category is Category.CONFIGURATION else 1


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(description="The exit-only PAPER supervisor.")
    parse.add_argument(
        "action",
        choices=("tick", "health", "stop-check", "deploy-check", "audit", "alert", "heartbeat"),
    )
    parse.add_argument("--action", dest="audited", choices=AUDIT_ACTIONS)
    parse.add_argument("--reason", help="UPPER_SNAKE_CASE code (audit only).")
    parse.add_argument("--actor", help="Who is asking, as recorded. Never verified.")
    parse.add_argument(
        "--dry-run", action="store_true", help="alert: show what would be sent, record nothing."
    )
    return parse


def main() -> int:
    arguments = parser().parse_args()
    if arguments.action == "tick":
        return _tick()
    if arguments.action == "health":
        return _health()
    if arguments.action == "stop-check":
        return _check("stop")
    if arguments.action == "deploy-check":
        return _check("deploy")
    if arguments.action == "alert":
        return _alert(arguments.dry_run)
    if arguments.action == "heartbeat":
        return _heartbeat()
    if arguments.audited is None:
        _print({"kind": "exit_supervisor_audit", "refusal": "ACTION_REQUIRED"})
        return REFUSED
    return _audit(arguments.audited, arguments.reason, arguments.actor)


if __name__ == "__main__":
    raise SystemExit(main())
