"""The dead-man's switch: signed heartbeats out, and a watcher somewhere else.

An alert pass on the trading host falls silent with the host. So the host
also sends heartbeats, and a receiver on another machine raises the alarm
when they stop. The two halves share one secret key and nothing else: the
receiver holds no trading secret, reads no trading database and writes
nowhere but its own small state directory.

Heartbeat (one JSON line, at most `MAX_LINE_BYTES`)::

    {"payload": {"v": 1, "host": "<alias>", "sent_at": "<ISO, UTC, ms>",
                 "nonce": "<32 hex>", "status": "<HEALTHY|...>"},
     "mac": "<hex HMAC-SHA256 of the canonical payload>"}

The receiver accepts a heartbeat only when its MAC verifies under the shared
key, it names the expected host, its `sent_at` lies within `MAX_SKEW_SECONDS`
of the receiver's clock and is later than the last accepted one. A copied
heartbeat therefore cannot be replayed — not now, not after a receiver
restart (the last accepted instant is persisted) — and an old one cannot be
reordered in. The receiver keeps one record per host: bounded by design.

Transport is deliberately not here. The intended one is SSH to the receiver
with a forced command (`restrict,command="... deadman receive ..."`), which
needs no listening service; anything that delivers one line on stdin works.

    python -m src.supervisor.deadman receive --store DIR --host ALIAS
    python -m src.supervisor.deadman check   --store DIR --host ALIAS [--dry-run]

`receive` reads one heartbeat on stdin and records it (exit 0) or rejects it
(exit 2, a code on stdout, nothing stored). `check` judges the last accepted
heartbeat — missing or older than `--max-age` is CRITICAL — and runs one
alert pass on that verdict (`alerts.py`). The sending half is
`python -m src.supervisor.main heartbeat` on the trading host.

Like `alerts.py`, nothing here imports settings or database code.
"""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.supervisor.alerts import (
    STATUSES,
    AlertPassBusy,
    Observation,
    TransportUnavailable,
    host_alias,
    read_secret_file,
    run_alert_pass,
    transport_from_environment,
)

HEARTBEAT_VERSION = 1
MAX_LINE_BYTES = 1024
MAX_SKEW_SECONDS = 120
DEFAULT_MAX_AGE_SECONDS = 300
MIN_KEY_BYTES = 32
RECEIVER_STATE_FILE = "heartbeat-state.json"
RECEIVER_LOCK = "heartbeat.lock"
KEY_FILE_ENV = "RH_AGENTS_HEARTBEAT_KEY_FILE"
KEY_CREDENTIAL = "heartbeat-key"
MAC = re.compile(r"^[0-9a-f]{64}$")


class HeartbeatRejected(Exception):
    """A heartbeat the receiver does not accept. `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_key(path: Path) -> bytes:
    """The shared key: an owner-only file of at least `MIN_KEY_BYTES` (hex or raw)."""
    value = read_secret_file(path)
    try:
        key = bytes.fromhex(value)
    except ValueError:
        key = value.encode()
    if len(key) < MIN_KEY_BYTES:
        raise TransportUnavailable("HEARTBEAT_KEY_TOO_SHORT")
    return key


def key_path(env: Mapping[str, str] | None = None) -> Path | None:
    environment = os.environ if env is None else env
    configured = environment.get(KEY_FILE_ENV, "").strip()
    if configured:
        return Path(configured)
    credentials = environment.get("CREDENTIALS_DIRECTORY", "").strip()
    return Path(credentials) / KEY_CREDENTIAL if credentials else None


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _mac(key: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def _instant_ms(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds")


def sign(key: bytes, *, host: str, status: str, now: datetime, nonce: str | None = None) -> str:
    """One heartbeat line. An unknown status is sent as CRITICAL."""
    payload = {
        "v": HEARTBEAT_VERSION,
        "host": host_alias(host),
        "sent_at": _instant_ms(now),
        "nonce": nonce or secrets.token_hex(16),
        "status": status if status in STATUSES else "CRITICAL",
    }
    return json.dumps({"payload": payload, "mac": _mac(key, payload)}, sort_keys=True)


@dataclass(frozen=True)
class Heartbeat:
    host: str
    sent_at: datetime
    status: str


def verify(
    line: str | bytes,
    key: bytes,
    *,
    expected_host: str,
    now: datetime,
    last_sent_at: datetime | None,
) -> Heartbeat:
    """The heartbeat in `line`, or `HeartbeatRejected` with the reason's code."""
    raw = line.encode() if isinstance(line, str) else line
    if len(raw) > MAX_LINE_BYTES:
        raise HeartbeatRejected("HEARTBEAT_TOO_LARGE")
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise HeartbeatRejected("HEARTBEAT_MALFORMED") from error
    if not isinstance(document, dict):
        raise HeartbeatRejected("HEARTBEAT_MALFORMED")
    payload = document.get("payload")
    mac = document.get("mac")
    if not isinstance(payload, dict) or not isinstance(mac, str) or not MAC.match(mac):
        raise HeartbeatRejected("HEARTBEAT_MALFORMED")
    if not hmac.compare_digest(_mac(key, payload).encode(), mac.encode()):
        raise HeartbeatRejected("HEARTBEAT_BAD_SIGNATURE")
    if payload.get("v") != HEARTBEAT_VERSION:
        raise HeartbeatRejected("HEARTBEAT_UNKNOWN_VERSION")
    if payload.get("host") != host_alias(expected_host):
        raise HeartbeatRejected("HEARTBEAT_WRONG_HOST")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 64:
        raise HeartbeatRejected("HEARTBEAT_MALFORMED")
    try:
        sent_at = datetime.fromisoformat(str(payload.get("sent_at")))
    except ValueError as error:
        raise HeartbeatRejected("HEARTBEAT_MALFORMED") from error
    if sent_at.tzinfo is None:
        raise HeartbeatRejected("HEARTBEAT_MALFORMED")
    if abs((now - sent_at).total_seconds()) > MAX_SKEW_SECONDS:
        raise HeartbeatRejected("HEARTBEAT_OUTSIDE_WINDOW")
    if last_sent_at is not None and sent_at <= last_sent_at:
        raise HeartbeatRejected("HEARTBEAT_REPLAYED")
    status = payload.get("status")
    return Heartbeat(
        host=str(payload["host"]),
        sent_at=sent_at,
        status=status if status in STATUSES else "CRITICAL",
    )


# Receiver state -------------------------------------------------------------


@dataclass(frozen=True)
class ReceiverState:
    host: str | None = None
    last_sent_at: datetime | None = None
    last_received_at: datetime | None = None
    last_status: str | None = None
    rejected: int = 0
    last_rejection: str | None = None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("An instant must carry its offset")
    return parsed


def load_receiver_state(directory: Path) -> ReceiverState:
    """The receiver's record. A missing or unreadable one is the empty record.

    Empty means "no heartbeat yet", which `judge` reads as CRITICAL — never as
    alive — and a replay after such a reset still has to fall inside the
    `MAX_SKEW_SECONDS` window to be accepted.
    """
    try:
        document = json.loads((directory / RECEIVER_STATE_FILE).read_text())
        if not isinstance(document, dict):
            raise ValueError("Not a record")
        status = document.get("last_status")
        rejected = document.get("rejected", 0)
        if not isinstance(rejected, int) or isinstance(rejected, bool) or rejected < 0:
            raise ValueError("A count must be a non-negative integer")
        return ReceiverState(
            host=document.get("host") if isinstance(document.get("host"), str) else None,
            last_sent_at=_instant(document.get("last_sent_at")),
            last_received_at=_instant(document.get("last_received_at")),
            last_status=status if status in STATUSES else None,
            rejected=rejected,
            last_rejection=document.get("last_rejection")
            if isinstance(document.get("last_rejection"), str)
            else None,
        )
    except (OSError, ValueError, TypeError):
        return ReceiverState()


def save_receiver_state(directory: Path, state: ReceiverState) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = {
        "host": state.host,
        "last_sent_at": _iso(state.last_sent_at),
        "last_received_at": _iso(state.last_received_at),
        "last_status": state.last_status,
        "rejected": state.rejected,
        "last_rejection": state.last_rejection,
    }
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".heartbeat.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(document, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / RECEIVER_STATE_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def receive(
    directory: Path, line: str | bytes, key: bytes, *, expected_host: str, now: datetime
) -> Heartbeat:
    """Verify one heartbeat and record it; a rejection is counted, nothing else."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / RECEIVER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = load_receiver_state(directory)
        try:
            heartbeat = verify(
                line, key, expected_host=expected_host, now=now, last_sent_at=state.last_sent_at
            )
        except HeartbeatRejected as rejected:
            save_receiver_state(
                directory,
                ReceiverState(
                    host=state.host,
                    last_sent_at=state.last_sent_at,
                    last_received_at=state.last_received_at,
                    last_status=state.last_status,
                    rejected=state.rejected + 1,
                    last_rejection=rejected.code,
                ),
            )
            raise
        save_receiver_state(
            directory,
            ReceiverState(
                host=heartbeat.host,
                last_sent_at=heartbeat.sent_at,
                last_received_at=now,
                last_status=heartbeat.status,
                rejected=state.rejected,
                last_rejection=state.last_rejection,
            ),
        )
        return heartbeat
    finally:
        os.close(descriptor)


def judge(
    state: ReceiverState, *, now: datetime, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> Observation:
    """The trading host as seen from outside it.

    No heartbeat yet, or the last one older than `max_age_seconds`: CRITICAL —
    the host, its network or its sender is down. A fresh heartbeat reporting
    CRITICAL is passed on as CRITICAL, so an alarm still gets out when the
    host's own alert path is what broke. Fresh and anything else is HEALTHY
    from out here; the host's own alert pass reports the rest.
    """
    if state.last_received_at is None:
        return Observation("CRITICAL", ("HOST_HEARTBEAT_MISSING",), None, now)
    age = (now - state.last_received_at).total_seconds()
    if age > max_age_seconds or age < -MAX_SKEW_SECONDS:
        return Observation("CRITICAL", ("HOST_HEARTBEAT_STALE",), None, now)
    if state.last_status == "CRITICAL":
        return Observation("CRITICAL", ("HOST_REPORTS_CRITICAL",), None, now)
    if state.last_status is None:
        return Observation("CRITICAL", ("HOST_HEARTBEAT_STATUS_UNKNOWN",), None, now)
    return Observation("HEALTHY", (), None, now)


# Command line ---------------------------------------------------------------


def _print(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, default=str))


def _receive(arguments: argparse.Namespace) -> int:
    path = key_path()
    if path is None:
        _print({"kind": "heartbeat_receive", "refusal": "HEARTBEAT_KEY_NOT_CONFIGURED"})
        return 2
    try:
        key = load_key(path)
    except TransportUnavailable as unavailable:
        _print({"kind": "heartbeat_receive", "refusal": unavailable.code})
        return 2
    line = sys.stdin.buffer.read(MAX_LINE_BYTES + 1).strip()
    try:
        heartbeat = receive(
            Path(arguments.store), line, key, expected_host=arguments.host, now=datetime.now(UTC)
        )
    except HeartbeatRejected as rejected:
        _print({"kind": "heartbeat_receive", "accepted": False, "refusal": rejected.code})
        return 2
    _print({"kind": "heartbeat_receive", "accepted": True, "status": heartbeat.status})
    return 0


def _check(arguments: argparse.Namespace) -> int:
    store = Path(arguments.store)
    now = datetime.now(UTC)
    observation = judge(load_receiver_state(store), now=now, max_age_seconds=arguments.max_age)
    refusal: str | None = None
    try:
        transport = None if arguments.dry_run else transport_from_environment()
    except TransportUnavailable as unavailable:
        transport, refusal = None, unavailable.code
    try:
        outcome = run_alert_pass(
            store / "alerts",
            observation,
            transport=transport,
            host=f"{host_alias(arguments.host)}-deadman",
            dry_run=arguments.dry_run,
        )
    except AlertPassBusy:
        _print({"kind": "deadman_check", "skipped": "ALERT_PASS_ALREADY_RUNNING"})
        return 0
    _print({"kind": "deadman_check", "transport_refusal": refusal, **outcome.as_dict()})
    if refusal is not None and not arguments.dry_run:
        return 2
    return 1 if outcome.failure is not None else 0


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(description="The dead-man's switch receiver.")
    parse.add_argument("action", choices=("receive", "check"))
    parse.add_argument("--store", required=True, help="The receiver's own state directory.")
    parse.add_argument("--host", required=True, help="The trading host's alias.")
    parse.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parse.add_argument("--dry-run", action="store_true")
    return parse


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.max_age < 60:
        _print({"kind": "deadman", "refusal": "MAX_AGE_TOO_SHORT"})
        return 2
    if arguments.action == "receive":
        return _receive(arguments)
    return _check(arguments)


__all__ = [
    "Heartbeat",
    "HeartbeatRejected",
    "ReceiverState",
    "judge",
    "key_path",
    "load_key",
    "load_receiver_state",
    "receive",
    "save_receiver_state",
    "sign",
    "verify",
]


if __name__ == "__main__":
    raise SystemExit(main())
